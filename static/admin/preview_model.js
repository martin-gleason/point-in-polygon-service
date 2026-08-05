"use strict";

/*
 * F8-T5 — the pure half of the installer page.
 *
 * Everything in this file is a function of its arguments. It touches no DOM,
 * no network and no globals, which is the whole reason it is a separate file:
 * the numbers it produces are the ones the operator's judgement rests on, and
 * a number that can only be checked by looking at a browser is a number nobody
 * checks. `tests/js/preview_model.test.mjs` drives every function here under
 * plain node, and `tests/test_admin_page.py` drives node from pytest.
 *
 * THE ONE TRANSFORM
 *     `buildTransform` is built once, from `preview.viewport`, and is used for
 *     the candidate and for every installed layer without exception. That is
 *     not a tidiness preference. The candidate is drawn over the installed
 *     layers because a superseded boundary file is valid in every mechanical
 *     sense — no check in `app.admin.validate` will ever flag it — and the
 *     operator noticing that the outlines no longer line up is the only
 *     control this feature has. Two layers projected even slightly differently
 *     would either invent a misalignment that is not there or hide one that
 *     is, and in both cases the drawing the operator is trusting would be
 *     lying. So there is one transform, it is pure, and it is tested.
 *
 * ArcGIS / ArcPy equivalent
 *     The map frame's coordinate system and extent in an ArcGIS Pro layout —
 *     `arcpy.mp.MapFrame.camera` plus the data frame's spatial reference,
 *     which every layer in that frame is drawn through whether it likes it or
 *     not. Pro guarantees the shared transform by construction; here it is
 *     guaranteed by there being one function and one call to it.
 */

// A span narrower than this is treated as this. A viewport of zero width is
// not a drawing problem to be reported, it is a division by zero — one
// degenerate shape collapsed to a point produces one, and the page must still
// draw something rather than fill itself with NaN.
export const MINIMUM_SPAN = 1e-12;

// How many decimal places of an SVG user unit survive into the path data. The
// page is 900 units wide, so a hundredth of a unit is far under a screen pixel
// and the saving over unrounded floats is roughly a third of the path text.
const PATH_DECIMALS = 2;

/**
 * The single projection every layer on the map is drawn through.
 *
 * `viewport` is the payload's own rectangle (`preview.viewport`): a box in
 * degrees, plus `longitude_scale`, which is cos(latitude) at the middle of the
 * box. A degree of longitude is shorter than a degree of latitude everywhere
 * but the equator — 0.74 of it in Cook County — so longitude is multiplied by
 * that factor before anything is scaled, or the county comes out a third too
 * wide and every shape in it is the wrong shape.
 *
 * One scale for both axes, chosen as the smaller of the two fits, so a shape
 * is never stretched. `app.admin.preview._fit` has already matched the box to
 * the pixel frame's aspect ratio, so in practice the two fits agree; taking
 * the minimum anyway means a hand-made or future viewport that does not agree
 * is letterboxed rather than distorted.
 */
export function buildTransform(viewport, size) {
  const frame = size || {};
  const width = positiveOr(frame.width, positiveOr(viewport && viewport.width_px, 900));
  const height = positiveOr(frame.height, positiveOr(viewport && viewport.height_px, 700));
  const box = viewport || {};

  const longitudeScale = positiveOr(box.longitude_scale, 1);
  const minX = finiteOr(box.min_x, 0);
  const minY = finiteOr(box.min_y, 0);
  const maxX = finiteOr(box.max_x, minX);
  const maxY = finiteOr(box.max_y, minY);

  const spanX = Math.max((maxX - minX) * longitudeScale, MINIMUM_SPAN);
  const spanY = Math.max(maxY - minY, MINIMUM_SPAN);
  const scale = Math.min(width / spanX, height / spanY);
  const offsetX = (width - spanX * scale) / 2;
  const offsetY = (height - spanY * scale) / 2;

  const transform = {
    width,
    height,
    scale,
    offsetX,
    offsetY,
    minX,
    minY,
    longitudeScale,
    units: typeof box.units === "string" ? box.units : "degrees",
    // y counts down the screen and up the globe, hence the subtraction.
    //
    // A coordinate that is not a number comes back as NaN and is never
    // substituted with anything, which is what `appendLine` needs in order to
    // drop the ring. Defaulting it to the viewport's own corner — as this did
    // once — draws a shape through a place the file never named, and the whole
    // point of this drawing is that the operator can believe it.
    project(x, y) {
      const longitude = Number(x);
      const latitude = Number(y);
      if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) {
        return [NaN, NaN];
      }
      return [
        offsetX + (longitude - minX) * longitudeScale * scale,
        height - offsetY - (latitude - minY) * scale,
      ];
    },
  };
  return Object.freeze(transform);
}

/** One coordinate pair through the transform. */
export function projectPoint(transform, x, y) {
  return transform.project(x, y);
}

/**
 * A GeoJSON geometry as SVG path data, or "" when it draws no mark.
 *
 * Returned as a string to be set with `setAttribute`. It is never concatenated
 * into markup: every coordinate here came out of a file the operator may have
 * been emailed, so it reaches the document through an attribute value and an
 * element built with `createElementNS`, and through nothing else.
 *
 * A ring with a coordinate that is not a number is dropped whole rather than
 * drawn as far as it got — half a boundary is a different boundary.
 */
export function geometryToPath(geometry, transform) {
  if (!geometry || typeof geometry !== "object") return "";
  const parts = [];
  appendGeometry(geometry, transform, parts);
  return parts.join("");
}

function appendGeometry(geometry, transform, parts) {
  const type = geometry.type;
  const coordinates = geometry.coordinates;
  if (type === "Polygon") {
    appendRings(coordinates, transform, parts);
  } else if (type === "MultiPolygon") {
    for (const polygon of arrayOr(coordinates)) appendRings(polygon, transform, parts);
  } else if (type === "LineString") {
    appendLine(coordinates, transform, parts, false);
  } else if (type === "MultiLineString") {
    for (const line of arrayOr(coordinates)) appendLine(line, transform, parts, false);
  } else if (type === "Point") {
    appendMarker(coordinates, transform, parts);
  } else if (type === "MultiPoint") {
    for (const point of arrayOr(coordinates)) appendMarker(point, transform, parts);
  } else if (type === "GeometryCollection") {
    for (const part of arrayOr(geometry.geometries)) appendGeometry(part, transform, parts);
  }
}

function appendRings(rings, transform, parts) {
  for (const ring of arrayOr(rings)) appendLine(ring, transform, parts, true);
}

function appendLine(coordinates, transform, parts, close) {
  const points = arrayOr(coordinates);
  if (points.length === 0) return;
  const drawn = [];
  for (let index = 0; index < points.length; index += 1) {
    const pair = points[index];
    if (!Array.isArray(pair)) return;
    const [x, y] = transform.project(pair[0], pair[1]);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return; // drop the ring whole
    drawn.push((index === 0 ? "M" : "L") + round(x) + " " + round(y));
  }
  if (close) drawn.push("Z");
  parts.push(drawn.join(" ") + " ");
}

function appendMarker(coordinates, transform, parts) {
  if (!Array.isArray(coordinates)) return;
  const [x, y] = transform.project(coordinates[0], coordinates[1]);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return;
  const arm = 3;
  parts.push(
    "M" + round(x - arm) + " " + round(y) +
    " L" + round(x) + " " + round(y - arm) +
    " L" + round(x + arm) + " " + round(y) +
    " L" + round(x) + " " + round(y + arm) + " Z "
  );
}

// --------------------------------------------------------------------------
// numbers, said the way somebody would say them out loud
// --------------------------------------------------------------------------

/** A distance between layers, matching `app.admin.preview._distance_phrase`. */
export function formatDistance(metres) {
  if (!Number.isFinite(metres)) return "an unknown distance";
  if (metres >= 1000) return group(Math.round(metres / 1000)) + " km";
  return group(Math.round(metres)) + " m";
}

/** The fidelity headline, to a tenth of a metre — never invented. */
export function formatDisplacement(metres) {
  if (!Number.isFinite(metres)) return null;
  return group(Math.round(metres * 10) / 10, 1) + " m";
}

export function formatCount(value) {
  return Number.isFinite(value) ? group(Math.round(value)) : "—";
}

function group(value, decimals) {
  const fixed = typeof decimals === "number" ? value.toFixed(decimals) : String(value);
  const [whole, fraction] = fixed.split(".");
  const spaced = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return fraction === undefined ? spaced : spaced + "." + fraction;
}

// --------------------------------------------------------------------------
// the honesty signals
// --------------------------------------------------------------------------

// Why a displacement could not be measured, in words. The keys are
// `app.admin.preview`'s own constants. An unknown that does not say why is
// read as "fine", which is the one reading this page must never produce.
const DISPLACEMENT_UNKNOWN_TEXT = {
  no_crs:
    "This file says nothing about where on Earth its shapes sit, so how far " +
    "the outlines drawn here sit from the real ones cannot be given in metres.",
  extent_too_large:
    "The ground this map covers is too wide for this tool to measure across " +
    "accurately, so it cannot say how far the outlines drawn here sit from " +
    "the real ones.",
  unmeasurable_geometry:
    "This tool could not measure how far the outlines drawn here sit from the " +
    "real ones — some of the shapes in this file are not of a kind it can " +
    "measure.",
  not_drawn:
    "Nothing from this file is drawn on the map, so this tool cannot say how " +
    "faithfully it was drawn.",
};

const NOT_SAYING_IT_IS_GOOD =
  "It is not saying the drawing is accurate; it is saying it does not know.";

/**
 * What the page says about how faithfully the outlines were drawn.
 *
 * The single most dangerous sentence this page could produce is "every
 * boundary here may sit up to 0.0 m from where it really is" written from a
 * measurement that was never taken. So an absent number is stated as absent,
 * with the payload's own reason, and the sentence that follows says in plain
 * words that unknown is not the same as accurate.
 */
export function displacementSentence(simplification) {
  const detail = simplification || {};
  const metres = detail.max_displacement_metres;
  if (Number.isFinite(metres)) {
    return {
      known: true,
      text:
        "Every boundary drawn here may sit up to " +
        formatDisplacement(metres) +
        " from where it really is.",
    };
  }
  const reason = detail.displacement_unknown_reason;
  const known = DISPLACEMENT_UNKNOWN_TEXT[reason];
  let text = known
    ? known + " " + NOT_SAYING_IT_IS_GOOD
    : "This tool could not measure how far the outlines drawn here sit from " +
      "the real ones. " + NOT_SAYING_IT_IS_GOOD;
  if (
    reason === "no_crs" &&
    Number.isFinite(detail.max_displacement_units)
  ) {
    text +=
      " Measured in the file's own numbers it is up to " +
      String(detail.max_displacement_units) +
      " of them.";
  }
  return { known: false, text };
}

/**
 * Is this response body a refusal?
 *
 * Not "was the status code in the 200s". This project answers every refusal in
 * one envelope — `{"error": {"code", "message"}}` — and a page that reads the
 * status line alone reported "Installed." for a refusal that arrived with a
 * 200. That is the single worst thing the install step can do: an operator who
 * believes a layer is installed stops checking, and nothing downstream will
 * ever tell them otherwise. So the body is asked as well as the status.
 */
export function describesRefusal(payload) {
  return Boolean(payload && typeof payload === "object" && payload.error);
}

/** How far the candidate sits from the nearest installed layer, in words. */
export function separationSentence(preview) {
  const payload = preview || {};
  if (payload.comparable === false) {
    return "This layer cannot be placed on Earth from what is inside it, so " +
      "it is drawn on its own and there is nothing to compare it against.";
  }
  if (payload.overlaps_installed === true) {
    return "This layer covers the same ground as a layer already installed.";
  }
  if (Number.isFinite(payload.separation_metres)) {
    return "The nearest layer already installed is " +
      formatDistance(payload.separation_metres) +
      " away from this one.";
  }
  return "This tool could not work out how far this layer sits from the " +
    "layers already installed, so it is not telling you whether they cover " +
    "the same ground. Look at the map.";
}

// --------------------------------------------------------------------------
// findings
// --------------------------------------------------------------------------

/**
 * The order the four parts of a finding are rendered in, and it is not a
 * choice the page gets to make.
 *
 * PIP-L004's `what` ends "the sentence that follows says which way round this
 * one is", which is the `specifics`. PIP-L008's `fix` says "the sentence above
 * gives its row number", which is also the `specifics`. Both sentences are
 * already shipping in `app/admin/codes.py`; rendered in any other order they
 * point at the wrong text and become nonsense. `app.admin.codes.Finding.message`
 * joins the same four in the same order, so the page and the API envelope
 * cannot drift apart.
 */
export const FINDING_PART_ORDER = ["what", "specifics", "why", "fix"];

export function findingParagraphs(finding) {
  const found = finding || {};
  const paragraphs = [];
  for (const part of FINDING_PART_ORDER) {
    const text = typeof found[part] === "string" ? found[part].trim() : "";
    if (text) paragraphs.push({ part, text });
  }
  return paragraphs;
}

/** "Must be fixed" / "Worth checking" — severity said in words, not in colour. */
export function severityLabel(severity) {
  return severity === "blocking" ? "Must be fixed" : "Worth checking";
}

/**
 * The rows PIP-L008 says the preview map marks — promise 1, kept.
 *
 * The preview payload marks the same rows on its features
 * (`properties.highlighted`); this is the list the page prints beside the map
 * so the operator can find them in the file's own table as well.
 */
export function brokenPositions(findings) {
  const rows = new Set();
  for (const finding of arrayOr(findings)) {
    if (!finding || finding.code !== "PIP-L008") continue;
    const detail = finding.detail || {};
    for (const position of arrayOr(detail.broken_positions)) {
      const row = Number(position);
      if (Number.isFinite(row)) rows.add(Math.trunc(row));
    }
  }
  return Array.from(rows).sort((left, right) => left - right);
}

/**
 * The columns this file actually has — promise 2, kept.
 *
 * PIP-L010's `fix` says "The columns this file actually has are listed beside
 * the preview", so they are, whether or not that finding fired: the finding's
 * own `detail.available_columns` first, because it is the list the message is
 * talking about, then everything the reader found in the file.
 */
export function availableColumns(findings, facts) {
  const names = [];
  const seen = new Set();
  const add = (value) => {
    const name = typeof value === "string" ? value : String(value == null ? "" : value);
    if (!name || seen.has(name)) return;
    seen.add(name);
    names.push(name);
  };
  for (const finding of arrayOr(findings)) {
    if (!finding || finding.code !== "PIP-L010") continue;
    for (const column of arrayOr((finding.detail || {}).available_columns)) add(column);
  }
  for (const column of arrayOr((facts || {}).columns)) {
    if (column && typeof column === "object") add(column.name);
    else add(column);
  }
  return names;
}

// --------------------------------------------------------------------------
// small helpers
// --------------------------------------------------------------------------

function arrayOr(value) {
  return Array.isArray(value) ? value : [];
}

function finiteOr(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function positiveOr(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : fallback;
}

function round(value) {
  return String(Number(value.toFixed(PATH_DECIMALS)));
}
