/*
 * F8-T5 — node tests for the pure half of the installer page.
 *
 * Run from pytest (`tests/test_admin_page.py::test_the_preview_model_passes_its_node_tests`),
 * which copies `static/admin/preview_model.js` next to this file as
 * `preview_model.mjs` and runs `node preview_model.test.mjs`. The rename is the
 * whole trick: this repository is a Python project with no `package.json`, so
 * node would read a `.js` file as CommonJS and refuse its `export` statements.
 *
 * What is actually being pinned here is the transform. The candidate is drawn
 * over the installed layers so that an operator can see that a superseded file
 * no longer lines up — the one failure no mechanical check in this project can
 * ever catch. If the two were projected even slightly differently the page
 * would invent a misalignment or hide one, and either way the drawing the
 * operator is trusting would be a lie. So: known input, known output, the same
 * transform for both roles, no stretching, and no division by zero.
 *
 * Offline: node's own assert module and nothing else.
 */
import assert from "node:assert/strict";

import {
  MINIMUM_SPAN,
  availableColumns,
  brokenPositions,
  buildTransform,
  describesRefusal,
  displacementSentence,
  findingParagraphs,
  formatDisplacement,
  formatDistance,
  geometryToPath,
  separationSentence,
  severityLabel,
  FINDING_PART_ORDER,
} from "./preview_model.mjs";

const tests = [];
function test(name, body) {
  tests.push([name, body]);
}

// --------------------------------------------------------------------------
// the transform
// --------------------------------------------------------------------------

const SQUARE_VIEWPORT = {
  min_x: 0,
  min_y: 0,
  max_x: 2,
  max_y: 1,
  width_px: 200,
  height_px: 100,
  longitude_scale: 1,
  units: "degrees",
};

test("a known viewport maps known lon/lat onto known SVG coordinates", () => {
  const transform = buildTransform(SQUARE_VIEWPORT, {});
  // 2 degrees across 200 px and 1 degree down 100 px: 100 px per degree, and
  // y counts up the globe and down the screen.
  assert.deepEqual(transform.project(0, 0), [0, 100]);
  assert.deepEqual(transform.project(2, 1), [200, 0]);
  assert.deepEqual(transform.project(1, 0.5), [100, 50]);
});

test("longitude is squeezed by the viewport's own cos(latitude)", () => {
  // A degree of longitude is 0.74 of a degree of latitude in Cook County. A
  // renderer that ignores that draws the county a third too wide.
  const transform = buildTransform(
    { min_x: 0, min_y: 0, max_x: 2, max_y: 1, longitude_scale: 0.5, units: "degrees" },
    { width: 200, height: 200 }
  );
  assert.deepEqual(transform.project(0, 0), [0, 200]);
  assert.deepEqual(transform.project(2, 1), [200, 0]);
});

test("the candidate and an installed layer get one identical transform", () => {
  const preview = {
    viewport: SQUARE_VIEWPORT,
    candidate: {
      geojson: {
        features: [
          { geometry: { type: "Polygon", coordinates: [[[0, 0], [2, 0], [2, 1], [0, 0]]] } },
        ],
      },
    },
    installed: [
      {
        geojson: {
          features: [
            { geometry: { type: "Polygon", coordinates: [[[0, 0], [2, 0], [2, 1], [0, 0]]] } },
          ],
        },
      },
    ],
  };
  // The page builds this once and hands the same object to both roles; the
  // point of the test is that the same ground gives the same ink.
  const transform = buildTransform(preview.viewport, {});
  const candidatePath = geometryToPath(
    preview.candidate.geojson.features[0].geometry,
    transform
  );
  const installedPath = geometryToPath(
    preview.installed[0].geojson.features[0].geometry,
    transform
  );
  assert.equal(candidatePath, installedPath);
  assert.ok(candidatePath.includes("M0 100"));

  // And building it twice from the same viewport is the same transform too —
  // so a refactor that rebuilt it per layer would still line up.
  const again = buildTransform(preview.viewport, {});
  assert.deepEqual(again.project(1, 0.5), transform.project(1, 0.5));
});

test("aspect ratio is preserved — shapes are letterboxed, never stretched", () => {
  // A 2:1 rectangle of ground drawn into a 1:1 pixel box.
  const transform = buildTransform(
    { min_x: 0, min_y: 0, max_x: 2, max_y: 1, longitude_scale: 1, units: "degrees" },
    { width: 200, height: 200 }
  );
  const origin = transform.project(0, 0);
  const eastOfIt = transform.project(1, 0);
  const northOfIt = transform.project(0, 1);
  const acrossPixels = eastOfIt[0] - origin[0];
  const upPixels = origin[1] - northOfIt[1];
  assert.equal(acrossPixels, upPixels, "one degree must be one length on both axes");
  // Letterboxed, not stretched: the drawing is centred in the taller box.
  assert.equal(transform.offsetY, 50);
  assert.equal(transform.offsetX, 0);
});

test("a degenerate zero-width viewport does not divide by zero", () => {
  const transform = buildTransform(
    {
      min_x: 5,
      min_y: 5,
      max_x: 5,
      max_y: 5,
      width_px: 900,
      height_px: 700,
      longitude_scale: 1,
      units: "degrees",
    },
    {}
  );
  const [x, y] = transform.project(5, 5);
  assert.ok(Number.isFinite(x) && Number.isFinite(y), "a collapsed layer still draws");
  assert.ok(Number.isFinite(transform.scale) && transform.scale > 0);
  assert.ok(MINIMUM_SPAN > 0);
  // And a viewport that is nonsense end to end still produces numbers for a
  // coordinate that is one.
  const rubbish = buildTransform(
    { min_x: NaN, min_y: undefined, max_x: null, max_y: "x", width_px: 0 },
    {}
  );
  const point = rubbish.project(0, 0);
  assert.ok(Number.isFinite(point[0]) && Number.isFinite(point[1]));
  // A corner that is not a number is never given one: NaN in, NaN out, and
  // `geometryToPath` drops the ring rather than inventing a place for it.
  assert.ok(Number.isNaN(transform.project(NaN, 5)[0]));
  assert.ok(Number.isNaN(transform.project(5, undefined)[1]));
});

// --------------------------------------------------------------------------
// paths
// --------------------------------------------------------------------------

test("a polygon with a hole becomes two closed subpaths", () => {
  const transform = buildTransform(SQUARE_VIEWPORT, {});
  const path = geometryToPath(
    {
      type: "Polygon",
      coordinates: [
        [[0, 0], [2, 0], [2, 1], [0, 1], [0, 0]],
        [[0.5, 0.25], [1, 0.25], [1, 0.5], [0.5, 0.25]],
      ],
    },
    transform
  );
  assert.equal((path.match(/M/g) || []).length, 2);
  assert.equal((path.match(/Z/g) || []).length, 2);
});

test("a ring holding a coordinate that is not a number is dropped whole", () => {
  const transform = buildTransform(SQUARE_VIEWPORT, {});
  const path = geometryToPath(
    { type: "Polygon", coordinates: [[[0, 0], [1, NaN], [2, 1], [0, 0]]] },
    transform
  );
  // Half a boundary is a different boundary, so none of it is drawn.
  assert.equal(path, "");
});

test("multipolygons, lines and collections all draw", () => {
  const transform = buildTransform(SQUARE_VIEWPORT, {});
  assert.ok(
    geometryToPath(
      { type: "MultiPolygon", coordinates: [[[[0, 0], [1, 0], [1, 1], [0, 0]]]] },
      transform
    ).includes("Z")
  );
  assert.ok(
    geometryToPath({ type: "LineString", coordinates: [[0, 0], [1, 1]] }, transform)
      .startsWith("M")
  );
  assert.ok(
    geometryToPath(
      {
        type: "GeometryCollection",
        geometries: [{ type: "Point", coordinates: [1, 0.5] }],
      },
      transform
    ).includes("Z")
  );
  assert.equal(geometryToPath(null, transform), "");
  assert.equal(geometryToPath({ type: "Polygon", coordinates: [] }, transform), "");
});

// --------------------------------------------------------------------------
// the honesty signals
// --------------------------------------------------------------------------

test("a measured displacement is stated to a tenth of a metre", () => {
  const said = displacementSentence({ max_displacement_metres: 0.54 });
  assert.equal(said.known, true);
  assert.ok(said.text.includes("0.5 m"));
  assert.equal(formatDisplacement(1234.56), "1,234.6 m");
  assert.equal(formatDisplacement(null), null);
});

test("an unmeasured displacement is never spelled zero", () => {
  for (const reason of ["no_crs", "extent_too_large", "unmeasurable_geometry", "not_drawn"]) {
    const said = displacementSentence({
      max_displacement_metres: null,
      displacement_unknown_reason: reason,
    });
    assert.equal(said.known, false, reason);
    assert.ok(!said.text.includes("0.0 m"), reason);
    assert.ok(said.text.includes("does not know"), reason);
  }
  // And with no reason at all it still refuses to imply accuracy.
  const bare = displacementSentence({});
  assert.equal(bare.known, false);
  assert.ok(bare.text.includes("does not know"));
});

test("separation is said the way somebody would say it out loud", () => {
  assert.equal(formatDistance(950), "950 m");
  assert.equal(formatDistance(22264), "22 km");
  assert.ok(separationSentence({ separation_metres: 22264 }).includes("22 km"));
  assert.ok(separationSentence({ overlaps_installed: true }).includes("same ground"));
  assert.ok(separationSentence({ comparable: false }).includes("on its own"));
  assert.ok(separationSentence({}).includes("could not work out"));
});

test("a refusal is a refusal whatever the status line says", () => {
  // The page reported "Installed." for a body carrying an error envelope that
  // arrived with a 200. Success is the body agreeing, not the status alone.
  assert.equal(describesRefusal({ error: { code: "not_implemented" } }), true);
  assert.equal(describesRefusal({ session_id: "abc" }), false);
  assert.equal(describesRefusal(null), false);
  assert.equal(describesRefusal("error"), false);
});

// --------------------------------------------------------------------------
// the findings
// --------------------------------------------------------------------------

test("a finding renders what, specifics, why, fix — in that order", () => {
  assert.deepEqual(FINDING_PART_ORDER, ["what", "specifics", "why", "fix"]);
  const paragraphs = findingParagraphs({
    what: "W",
    why: "Y",
    fix: "F",
    specifics: "S",
  });
  assert.deepEqual(paragraphs.map((paragraph) => paragraph.part), FINDING_PART_ORDER);
  assert.deepEqual(paragraphs.map((paragraph) => paragraph.text), ["W", "S", "Y", "F"]);
  // An empty part is left out rather than rendered as a blank paragraph.
  assert.deepEqual(
    findingParagraphs({ what: "W", specifics: "  ", why: "", fix: "F" }).map((p) => p.part),
    ["what", "fix"]
  );
});

test("severity is carried by a word, not only by a colour", () => {
  assert.equal(severityLabel("blocking"), "Must be fixed");
  assert.equal(severityLabel("warning"), "Worth checking");
});

test("PIP-L008's marked rows come out sorted, deduplicated and whole", () => {
  const rows = brokenPositions([
    { code: "PIP-L008", detail: { broken_positions: [7, 2, "2", 2.9] } },
    { code: "PIP-L016", detail: { broken_positions: [99] } },
    null,
  ]);
  assert.deepEqual(rows, [2, 7]);
  assert.deepEqual(brokenPositions(undefined), []);
});

test("PIP-L010's available columns are listed, and so are the file's own", () => {
  const columns = availableColumns(
    [{ code: "PIP-L010", detail: { available_columns: ["ward", "dist_num"] } }],
    { columns: [{ name: "dist_num" }, { name: "shape_area" }] }
  );
  assert.deepEqual(columns, ["ward", "dist_num", "shape_area"]);
  // The promise holds when the finding never fired, too.
  assert.deepEqual(availableColumns([], { columns: [{ name: "ward" }] }), ["ward"]);
  assert.deepEqual(availableColumns([], {}), []);
});

// --------------------------------------------------------------------------

let failed = 0;
for (const [name, body] of tests) {
  try {
    body();
    console.log("ok   " + name);
  } catch (error) {
    failed += 1;
    console.log("FAIL " + name);
    console.log(String(error && error.stack ? error.stack : error));
  }
}
console.log(tests.length - failed + "/" + tests.length + " node checks passed");
process.exit(failed === 0 ? 0 : 1);
