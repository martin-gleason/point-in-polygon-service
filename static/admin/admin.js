"use strict";

/*
 * F8-T5 — the installer page.
 *
 * Talks only to the installer app on this same origin. No framework, no
 * bundler, no library, no font and no map tile: this page is opened on a
 * laptop that may have no network at all, and every one of those would be a
 * request that leaves the machine.
 *
 * THE TOKEN
 *     The launcher opens this page with the token in the URL *fragment*
 *     (`#token=…`). A fragment is the one part of a URL browsers never send to
 *     a server and never put in `Referer`. It is read once, kept in a closure
 *     variable, and the fragment is wiped with `history.replaceState` on the
 *     first line of work — so it is not sitting in the address bar, not in a
 *     screenshot of the address bar, and not in whatever the operator pastes
 *     into a support request. It goes out as the `X-Admin-Token` header and
 *     nowhere else: never in a URL, never rendered, never logged.
 *
 * EVERY STRING FROM THE API IS HOSTILE
 *     Filenames, layer names, column names, sample values and finding
 *     specifics all originate inside a file the operator may have been emailed
 *     by a stranger, and all of them flow to this page. So there is no
 *     `innerHTML` in this file, no HTML built by string concatenation, and no
 *     SVG built by string concatenation either: elements are made with
 *     `createElement` / `createElementNS`, text goes in through `textContent`,
 *     and path data goes in through `setAttribute`. That is not defence in
 *     depth, it is the only defence — a tool for a volunteer has no security
 *     team behind it.
 *
 * ArcGIS / ArcPy equivalent
 *     The Share > Publish dialog in ArcGIS Pro: the analysis pane listing what
 *     is wrong with the layer, the map view showing the candidate over what is
 *     already published, and the Publish button that stays greyed out until
 *     the errors are cleared. Pro's checks come from a licensed desktop
 *     application; these come from `app.admin.validate`, and the drawing is
 *     hand-rolled SVG because a map library would be a dependency this project
 *     will not take.
 */

import {
  availableColumns,
  brokenPositions,
  buildTransform,
  describesRefusal,
  displacementSentence,
  findingParagraphs,
  formatCount,
  formatDistance,
  geometryToPath,
  separationSentence,
  severityLabel,
} from "./preview_model.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const TOKEN_HEADER = "X-Admin-Token";

// How many features are added to the map before the browser is given the frame
// back. The realistic payload is about 1.3 MB of GeoJSON across tens of
// thousands of corner points; appending it in one go blocks the main thread for
// long enough that the page looks frozen — and a page that looks frozen is one
// an operator kills halfway through.
const FEATURES_PER_FRAME = 200;

// --------------------------------------------------------------------------
// the token, taken out of the address bar before anything else happens
// --------------------------------------------------------------------------

const adminToken = takeTokenFromFragment();

function takeTokenFromFragment() {
  const fragment = window.location.hash.replace(/^#/, "");
  let token = "";
  for (const pair of fragment.split("&")) {
    const [name, value] = pair.split("=");
    if (name === "token" && value) {
      try {
        token = decodeURIComponent(value);
      } catch (error) {
        token = value;
      }
    }
  }
  // Unconditionally: whatever was in the fragment, it is not staying there.
  window.history.replaceState(
    null,
    "",
    window.location.pathname + window.location.search
  );
  return token;
}

// --------------------------------------------------------------------------
// the page
// --------------------------------------------------------------------------

const sourceForm = byId("source-form");
const fileInput = byId("file-input");
const dropZone = byId("drop-zone");
const chosenFiles = byId("chosen-files");
const urlInput = byId("source-url");
const layerIdInput = byId("layer-id");
const displayNameInput = byId("display-name");
const attributesInput = byId("attributes");
const selectInput = byId("select-layer");
const inspectButton = byId("inspect-button");

const statusRegion = byId("status");

const findingsCard = byId("findings-card");
const findingsHeading = byId("findings-heading");
const findingsSummary = byId("findings-summary");
const findingsList = byId("findings");

const mapCard = byId("map-card");
const zoomToggle = byId("zoom-toggle");
const mapSvg = byId("preview-map");
const installedGroup = byId("installed-group");
const candidateGroup = byId("candidate-group");
const mapSummary = byId("map-summary");
const columnsList = byId("columns");
const markedRows = byId("marked-rows");
const previewNotes = byId("preview-notes");

const installCard = byId("install-card");
const ackCheckbox = byId("ack-warnings");
const ackLabel = byId("ack-label");
const installButton = byId("install-button");
const installHint = byId("install-hint");

// Files chosen by drag-and-drop live here; the file input owns its own. Both
// are peers, and the last one used wins.
let droppedFiles = [];

const state = {
  sessionId: null,
  blocking: true,
  hasWarnings: false,
  // True only once the shapes are actually drawn — see drawJobs' completion
  // callback. Not "the request finished": installing a layer nobody looked at
  // is the exact failure this tool exists to prevent.
  mapDrawn: false,
  installed: false,
};

// Bumped on every new preview so a render still in flight for the previous
// candidate stops instead of drawing itself over the current one.
let renderGeneration = 0;

// The preview being drawn, and which of its two rectangles is being used. Held
// so the operator can switch between them without asking the server again — a
// second request would be a second 1.3 MB payload for a picture of the same
// shapes.
let currentPreview = null;
let zoomedToCandidate = false;

// --------------------------------------------------------------------------
// small DOM helpers — the only places elements are made
// --------------------------------------------------------------------------

function byId(id) {
  return document.getElementById(id);
}

function clear(element) {
  while (element.firstChild) element.removeChild(element.firstChild);
}

function make(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function setStatus(kind, text) {
  statusRegion.className = "status " + kind;
  statusRegion.textContent = text;
}

/**
 * Turn a control off WITHOUT taking it out of the tab order.
 *
 * `disabled` removes a control from the accessibility tree and from the tab
 * order, which means the operator who most needs to know why a button will not
 * work is the one who can never reach it to be told — and when the condition
 * clears, the control reappears in the tab order with nothing announced.
 * `aria-disabled` keeps it reachable and keeps its description (the hint
 * paragraph) readable; the click handler does the actual refusing.
 *
 * ArcGIS / ArcPy equivalent
 *     ArcGIS Pro's Publish button greys out until the analysis pane is clear,
 *     and the reason lives only in the pane. Here the reason is attached to the
 *     button itself, because there is no second window to go and read.
 */
function setControlOff(control, off) {
  control.setAttribute("aria-disabled", off ? "true" : "false");
}

function isControlOff(control) {
  return control.getAttribute("aria-disabled") === "true";
}

// --------------------------------------------------------------------------
// talking to the installer
// --------------------------------------------------------------------------

async function call(path, options) {
  const settings = Object.assign({ method: "GET" }, options || {});
  settings.headers = Object.assign({}, settings.headers || {});
  settings.headers[TOKEN_HEADER] = adminToken;
  settings.cache = "no-store";
  const response = await fetch(path, settings);
  const payload = await response.json().catch(() => null);
  // Success is not "the status code was in the 200s". A body carrying this
  // project's `{"error": …}` envelope is a refusal whatever the status line
  // says, and the difference is not academic: the page reported "Installed."
  // for a refusal that arrived with a 200, which is the exact failure the
  // install step must never produce — an operator who believes a layer is
  // installed stops checking.
  return {
    ok: response.ok && !describesRefusal(payload),
    status: response.status,
    payload,
  };
}

function errorText(result, fallback) {
  const error = result.payload && result.payload.error;
  if (error && typeof error.message === "string") return error.message;
  return fallback + " (" + result.status + ")";
}

// --------------------------------------------------------------------------
// choosing a file
// --------------------------------------------------------------------------

function chosenFileList() {
  if (fileInput.files && fileInput.files.length) return Array.from(fileInput.files);
  return droppedFiles;
}

function describeChosen() {
  const files = chosenFileList();
  if (!files.length) {
    chosenFiles.textContent = "";
    return;
  }
  // Filenames come off somebody's disk. textContent, always.
  chosenFiles.textContent =
    files.length === 1
      ? "Chosen: " + files[0].name
      : "Chosen " + files.length + " files: " + files.map((file) => file.name).join(", ");
}

fileInput.addEventListener("change", () => {
  droppedFiles = [];
  describeChosen();
});

for (const eventName of ["dragenter", "dragover"]) {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.add("dragging");
  });
}
for (const eventName of ["dragleave", "drop"]) {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.remove("dragging");
  });
}
dropZone.addEventListener("drop", (event) => {
  const transferred = event.dataTransfer && event.dataTransfer.files;
  if (!transferred || !transferred.length) return;
  droppedFiles = Array.from(transferred);
  fileInput.value = "";
  describeChosen();
});

// --------------------------------------------------------------------------
// step 1 — inspect
// --------------------------------------------------------------------------

sourceForm.addEventListener("submit", (event) => {
  event.preventDefault();
  inspect();
});

async function inspect() {
  const files = chosenFileList();
  const url = urlInput.value.trim();
  urlInput.removeAttribute("aria-invalid");

  if (files.length && url) {
    urlInput.setAttribute("aria-invalid", "true");
    setStatus("error", "Choose a file or give a web address — not both.");
    urlInput.focus();
    return;
  }
  if (!files.length && !url) {
    setStatus(
      "error",
      "Choose a map file, or give the web address of a published map service."
    );
    fileInput.focus();
    return;
  }

  const body = new FormData();
  for (const file of files) body.append("files", file, file.name);
  if (url) body.append("url", url);
  body.append("layer_id", layerIdInput.value.trim());
  body.append("display_name", displayNameInput.value.trim());
  body.append("attributes", attributesInput.value.trim());
  body.append("select", selectInput.value.trim());

  resetResults();
  inspectButton.disabled = true;
  setStatus("pending", "Reading the layer and checking it. This can take a moment…");

  let result;
  try {
    result = await call("/api/inspect", { method: "POST", body });
  } catch (error) {
    inspectButton.disabled = false;
    setStatus("error", "The tool did not answer. Is it still running?");
    return;
  }
  inspectButton.disabled = false;

  if (!result.ok) {
    // A refusal of the *file* carries a finding written for the operator; a
    // refusal of the *request* carries only a sentence. Both are shown.
    const finding = result.payload && result.payload.error && result.payload.error.finding;
    if (finding) {
      showFindings([finding], true);
    }
    setStatus("error", errorText(result, "That could not be read"));
    if (finding) focusResults();
    return;
  }

  const payload = result.payload;
  state.sessionId = payload.session_id;
  state.blocking = Boolean(payload.blocking);
  const findings = Array.isArray(payload.findings) ? payload.findings : [];
  state.hasWarnings = findings.some((found) => found.severity === "warning");

  showFindings(findings, false);
  showColumns(findings, payload.facts);
  showMarkedRows(findings);
  installCard.hidden = false;
  updateInstallButton();

  setStatus(
    state.blocking ? "error" : "warn",
    state.blocking
      ? "This layer cannot be installed as it is — see what must be fixed below."
      : "Nothing here stops this layer being installed. Look at the map before you do."
  );

  focusResults();
  loadPreview(payload.session_id);
}

/**
 * Put the caret on the results the operator just asked for.
 *
 * The live region says "see what must be fixed below", and "below" was three
 * sections that had just stopped being `hidden`, containing nothing focusable —
 * so Tab stepped straight over the entire answer and the summary line was never
 * spoken at all. A submit the operator pressed is precisely the moment moving
 * focus is correct rather than rude. The heading is described by
 * #findings-summary, so landing on it reads the heading and then the count.
 */
function focusResults() {
  findingsHeading.focus();
}

function resetResults() {
  renderGeneration += 1;
  currentPreview = null;
  zoomedToCandidate = false;
  zoomToggle.disabled = true;
  state.sessionId = null;
  state.blocking = true;
  state.hasWarnings = false;
  state.mapDrawn = false;
  state.installed = false;
  clear(findingsList);
  clear(columnsList);
  clear(previewNotes);
  clear(installedGroup);
  clear(candidateGroup);
  findingsCard.hidden = true;
  mapCard.hidden = true;
  installCard.hidden = true;
  ackCheckbox.checked = false;
  setControlOff(installButton, true);
  updateInstallButton();
  setMapText("The map has not been drawn yet.");
  markedRows.textContent = "";
  findingsSummary.textContent = "";
}

// --------------------------------------------------------------------------
// the findings
// --------------------------------------------------------------------------

/**
 * Every finding, in the order the registry wrote them.
 *
 * The four parts go out in the one order they mean anything in — what, the
 * specifics, why, the fix — because two registry entries point across that
 * boundary in words ("the sentence that follows", "the sentence above"). The
 * order lives in `preview_model.FINDING_PART_ORDER`, which node tests, rather
 * than in the shape of the loop below.
 */
function showFindings(findings, isRefusal) {
  clear(findingsList);
  findingsCard.hidden = false;

  const blocking = findings.filter((found) => found.severity === "blocking").length;
  const warnings = findings.length - blocking;
  findingsSummary.textContent = isRefusal
    ? "This file was refused before it could be checked any further."
    : findings.length === 0
    ? "Nothing to report — no problems were found in this file."
    : blocking + " must be fixed, " + warnings + " worth checking.";

  for (const finding of findings) {
    const severity = finding.severity === "blocking" ? "blocking" : "warning";
    const item = make("li", "finding " + severity);

    const head = make("div", "finding-head");
    // The code, prominently: an operator quotes it in a support request and
    // looks it up in the docs, so it is text they can select, not decoration.
    head.appendChild(make("span", "finding-code", finding.code || ""));
    head.appendChild(make("span", "severity", severityLabel(finding.severity)));
    item.appendChild(head);
    item.appendChild(make("p", "finding-title", finding.title || ""));

    for (const paragraph of findingParagraphs(finding)) {
      item.appendChild(make("p", "part part-" + paragraph.part, paragraph.text));
    }

    // PIP-L008 promises "the preview map marks each one". The rows are named
    // here too, because a mark on a map is not something you can look up in a
    // spreadsheet.
    const rows = brokenPositions([finding]);
    if (rows.length) {
      item.appendChild(
        make(
          "p",
          "rows",
          "Marked on the map, at " +
            (rows.length === 1 ? "row " : "rows ") +
            rows.join(", ") +
            " of the file, counting every row from the top starting at 0."
        )
      );
    }
    findingsList.appendChild(item);
  }
}

/** PIP-L010: "The columns this file actually has are listed beside the preview." */
function showColumns(findings, facts) {
  clear(columnsList);
  const samplesByName = new Map();
  const columnFacts = facts && Array.isArray(facts.columns) ? facts.columns : [];
  for (const column of columnFacts) {
    if (column && typeof column === "object") samplesByName.set(String(column.name), column);
  }

  const names = availableColumns(findings, facts);
  if (!names.length) {
    columnsList.appendChild(make("li", "hint", "This file has no columns of names or numbers in it."));
    return;
  }
  for (const name of names) {
    const item = make("li");
    item.appendChild(make("span", "column-name", name));
    const detail = samplesByName.get(name);
    const samples = detail && Array.isArray(detail.samples) ? detail.samples : [];
    const shown = samples.slice(0, 3).map((value) => (value === null ? "—" : String(value)));
    item.appendChild(
      make(
        "span",
        "column-samples",
        shown.length ? shown.join(", ") : "nothing in this column"
      )
    );
    columnsList.appendChild(item);
  }
}

function showMarkedRows(findings) {
  const rows = brokenPositions(findings);
  markedRows.textContent = rows.length
    ? "Rows marked on the map: " + rows.join(", ") + "."
    : "";
}

// --------------------------------------------------------------------------
// the map
// --------------------------------------------------------------------------

/**
 * One sentence, in two places: visible under the map, and as the map's own
 * accessible name.
 *
 * The `<svg role="img">` used to be named by the section heading — "3. Look at
 * the shape" — which told a screen-reader user to do the one thing they cannot
 * and carried none of the content. Everything the drawing says now goes through
 * here, so the two paths cannot drift apart.
 */
function setMapText(text) {
  mapSummary.textContent = text;
  mapSvg.setAttribute("aria-label", text);
}

async function loadPreview(sessionId) {
  mapCard.hidden = false;
  setMapText("Drawing the map…");
  let result;
  try {
    result = await call("/api/preview/" + encodeURIComponent(sessionId));
  } catch (error) {
    // `mapDrawn` deliberately stays false. The gate means "the operator has
    // seen the shape", not "the request finished" — and a request that failed
    // is the clearest case of not having seen it.
    updateInstallButton();
    setMapText("The map could not be drawn: the tool did not answer.");
    return;
  }
  if (!result.ok) {
    updateInstallButton();
    setMapText("The map could not be drawn. " + errorText(result, "The tool refused"));
    return;
  }
  renderPreview(result.payload.preview);
}

function renderPreview(preview) {
  renderGeneration += 1;
  const generation = renderGeneration;
  currentPreview = preview;
  clear(installedGroup);
  clear(candidateGroup);

  const wide = preview.viewport || preview.candidate_viewport;
  const close = preview.candidate_viewport || preview.viewport;
  zoomToggle.disabled = !preview.candidate_viewport || !preview.viewport;
  // The label names the action and nothing else. No aria-pressed: a control
  // that both swaps its label and reports a pressed state announces the
  // opposite of the truth ("Show everything installed as well … pressed"
  // means "showing everything is ON", which is exactly when it is off). Which
  // view is drawn is said in words by `viewSentence`, under the map and in the
  // map's own accessible name.
  zoomToggle.textContent = zoomedToCandidate
    ? "Show everything installed as well"
    : "Zoom in on this layer";

  const viewport = zoomedToCandidate ? close : wide;
  if (!viewport || preview.undrawable_reason) {
    mapSvg.setAttribute("viewBox", "0 0 900 700");
    describeMap(preview, null);
    return;
  }

  // ONE transform, built once, used for the candidate and for every installed
  // layer. Two transforms would either invent a misalignment or hide one, and
  // this drawing is the only check there is against a superseded file.
  const transform = buildTransform(viewport, {
    width: viewport.width_px,
    height: viewport.height_px,
  });
  mapSvg.setAttribute("viewBox", "0 0 " + transform.width + " " + transform.height);

  const jobs = [];
  for (const layer of preview.installed || []) {
    jobs.push({ group: installedGroup, layer, className: "installed-shape" });
  }
  // The candidate goes on last so it is drawn over the installed layers.
  jobs.push({ group: candidateGroup, layer: preview.candidate, className: "candidate-shape" });

  drawJobs(jobs, transform, generation, () => {
    describeMap(preview, transform);
    // Only here — with the ink actually on the page — does the operator count
    // as having seen the layer. Setting this when the request returned would
    // unlock Install while the map was still blank, and in a backgrounded tab
    // (where requestAnimationFrame never fires) it would never be drawn at all.
    state.mapDrawn = true;
    updateInstallButton();
  });
}

zoomToggle.addEventListener("click", () => {
  if (!currentPreview) return;
  zoomedToCandidate = !zoomedToCandidate;
  // Re-drawn from the payload already in hand, through one transform rebuilt
  // from the other rectangle. Both layers still share it, which is the whole
  // invariant — a second view must not become a second projection.
  renderPreview(currentPreview);
});

/** Draw layer by layer, a few hundred features to a frame, so the page stays alive. */
function drawJobs(jobs, transform, generation, whenDone) {
  let jobIndex = 0;
  let featureIndex = 0;

  function step() {
    if (generation !== renderGeneration) return; // a newer candidate arrived
    let drawn = 0;
    while (jobIndex < jobs.length && drawn < FEATURES_PER_FRAME) {
      const job = jobs[jobIndex];
      const features =
        (job.layer && job.layer.geojson && job.layer.geojson.features) || [];
      if (featureIndex >= features.length) {
        jobIndex += 1;
        featureIndex = 0;
        continue;
      }
      appendFeature(job.group, features[featureIndex], transform, job.className);
      featureIndex += 1;
      drawn += 1;
    }
    if (jobIndex < jobs.length) {
      window.requestAnimationFrame(step);
      return;
    }
    whenDone();
  }

  window.requestAnimationFrame(step);
}

function appendFeature(group, feature, transform, className) {
  if (!feature || !feature.geometry) return;
  const data = geometryToPath(feature.geometry, transform);
  if (!data) return;
  const marked = Boolean(feature.properties && feature.properties.highlighted);

  const path = document.createElementNS(SVG_NS, "path");
  // Path data is set as an attribute value, never concatenated into markup.
  path.setAttribute("d", data);
  path.setAttribute("class", className + (marked ? " marked" : ""));
  group.appendChild(path);

  if (marked) {
    // A ring as well as a dotted stroke: the mark has to survive being looked
    // at by somebody who does not see the colour difference.
    const centre = projectedCentre(feature.geometry, transform);
    if (centre) {
      const ring = document.createElementNS(SVG_NS, "circle");
      ring.setAttribute("cx", String(centre[0]));
      ring.setAttribute("cy", String(centre[1]));
      ring.setAttribute("r", "9");
      ring.setAttribute("class", "marked-ring");
      group.appendChild(ring);
    }
  }
}

/** The middle of a geometry's box, in SVG units — for placing the ring. */
function projectedCentre(geometry, transform) {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  const visit = (value) => {
    if (!Array.isArray(value)) return;
    if (typeof value[0] === "number" && typeof value[1] === "number") {
      const [x, y] = transform.project(value[0], value[1]);
      if (!Number.isFinite(x) || !Number.isFinite(y)) return;
      minX = Math.min(minX, x);
      maxX = Math.max(maxX, x);
      minY = Math.min(minY, y);
      maxY = Math.max(maxY, y);
      return;
    }
    for (const item of value) visit(item);
  };
  if (geometry.type === "GeometryCollection") {
    for (const part of geometry.geometries || []) {
      const centre = projectedCentre(part, transform);
      if (centre) return centre;
    }
    return null;
  }
  visit(geometry.coordinates);
  if (!Number.isFinite(minX) || !Number.isFinite(minY)) return null;
  return [(minX + maxX) / 2, (minY + maxY) / 2];
}

/**
 * What the drawing shows, and the payload's own honesty signals, in words.
 *
 * Every sentence is appended to the visible summary AND collected into the
 * map's accessible name, so the non-visual path carries the same content the
 * visual one does: what is drawn, how it sits against the layers already
 * installed, which of the two views is showing, and how faithful the outlines
 * are. Nothing is invented here — `displacementSentence` still decides whether
 * a displacement number is known, and a number that did not arrive is not
 * printed.
 */
function describeMap(preview, transform) {
  clear(previewNotes);
  clear(mapSummary);

  const simplification = preview.simplification || {};
  const displacement = displacementSentence(simplification);

  const spoken = [];
  const say = (text, className) => {
    if (!text) return;
    mapSummary.appendChild(make("span", className || null, text));
    mapSummary.appendChild(document.createTextNode(" "));
    spoken.push(text.trim());
  };

  say(drawnSentence(preview, transform));
  say(viewSentence(preview));
  say(displacement.text, displacement.known ? "known" : "unknown");

  if (simplification.escalated) {
    say(
      "This layer holds more detail than one page can draw, so the outlines " +
        "were smoothed further than usual to fit — " +
        formatCount(simplification.vertices_before) +
        " corner points came down to " +
        formatCount(simplification.vertices_after) +
        "."
    );
  }

  say(separationSentence(preview));

  // Only when there is a gap. "0 m away" is a true number and a misleading
  // sentence: layers that touch are covered by `separationSentence` saying
  // they cover the same ground.
  if (
    transform &&
    Number.isFinite(preview.separation_metres) &&
    preview.separation_metres > 0
  ) {
    say(
      "The nearest installed layer is " +
        formatDistance(preview.separation_metres) +
        " away."
    );
  }

  mapSvg.setAttribute(
    "aria-label",
    spoken.join(" ") || "There is nothing to show on this map."
  );

  if (preview.undrawable_reason) {
    previewNotes.appendChild(
      make(
        "li",
        null,
        "Nothing is drawn on this map. Check this file another way before you " +
          "install it — you have not seen it."
      )
    );
  }

  if (Array.isArray(preview.highlight_not_drawn) && preview.highlight_not_drawn.length) {
    previewNotes.appendChild(
      make(
        "li",
        null,
        "These marked rows are not on the map, so there is nothing to look at " +
          "for them: " +
          preview.highlight_not_drawn.join(", ") +
          "."
      )
    );
  }

  // The payload's own notes say a dropped shape is "listed row by row". Those
  // rows are in dropped_positions, and until they are printed the operator is
  // told a list exists, shown none, and never learns which shape vanished off
  // the map. Same row numbering the marked rows use, so the two agree.
  for (const layer of [preview.candidate, ...(preview.installed || [])]) {
    const dropped = layer && layer.dropped_positions;
    if (!Array.isArray(dropped) || !dropped.length) continue;
    const whose =
      layer === preview.candidate
        ? "this file"
        : "the installed layer " + String(layer.name || layer.id || "");
    previewNotes.appendChild(
      make(
        "li",
        null,
        "Not drawn from " +
          whose +
          ", by row: " +
          dropped.join(", ") +
          ". Counting every row from the top of the file, including any row " +
          "with nothing drawn in it."
      )
    );
  }

  for (const note of preview.notes || []) {
    previewNotes.appendChild(make("li", null, String(note)));
  }
}

/** What is on the canvas, named by the same line styles the legend names. */
function drawnSentence(preview, transform) {
  // The note below already says nothing was drawn, and says what to do about
  // it. Saying it twice in one breath helps nobody.
  if (preview.undrawable_reason) return "";
  if (!transform) return "Nothing is drawn on this map.";

  const count = preview.candidate && preview.candidate.feature_count;
  const areas = Number.isFinite(count)
    ? formatCount(count) + " areas"
    : "The areas";
  const installed = (preview.installed || []).length;
  return (
    areas +
    " from this file are drawn as a heavy dashed outline, over " +
    (installed
      ? formatCount(installed) +
        (installed === 1 ? " layer" : " layers") +
        " already installed, drawn in thin solid grey."
      : "an empty map — no other layer is installed on this computer yet.")
  );
}

/** Which of the payload's two rectangles is showing, said rather than shown. */
function viewSentence(preview) {
  if (!preview.candidate_viewport || !preview.viewport) return "";
  return zoomedToCandidate
    ? "The view is zoomed in on this layer alone, so anything installed " +
        "outside it is off the edge."
    : "The view takes in this layer and everything already installed.";
}

// --------------------------------------------------------------------------
// step 4 — install
// --------------------------------------------------------------------------

/**
 * Install is off until the tool says nothing blocks it, the operator has
 * acknowledged anything worth checking, and a preview has come back — because
 * installing without having looked at the map is exactly the failure this
 * whole feature exists to prevent.
 */
function updateInstallButton() {
  // The confirmation is ALWAYS tickable. Its second clause — "the map is the
  // layer I meant to install" — is the one thing this feature exists to
  // collect, and on a clean file it used to be permanently greyed out, so the
  // happy path was the path where the operator could not say yes. What changes
  // instead is the wording: the warnings clause is dropped when there are none,
  // rather than left standing over a file that has no warnings to have read.
  ackLabel.textContent = state.hasWarnings
    ? "I have read the things worth checking above, and the map is the layer " +
      "I meant to install."
    : "The map above is the layer I meant to install.";

  const acknowledged = ackCheckbox.checked;
  const ready =
    Boolean(state.sessionId) &&
    !state.blocking &&
    acknowledged &&
    state.mapDrawn &&
    !state.installed;
  setControlOff(installButton, !ready);

  // Never empty. This paragraph is the button's `aria-describedby`, so it is
  // what a keyboard user hears the moment they land on the button — which is
  // the whole reason the button stayed in the tab order.
  if (state.installed) {
    installHint.textContent = "This layer is installed. There is nothing more to do here.";
  } else if (!state.sessionId) {
    installHint.textContent = "Choose a file and look at the map first.";
  } else if (state.blocking) {
    installHint.textContent =
      "Something above must be fixed before this layer can be installed. " +
      "Pressing this now will not do anything.";
  } else if (!state.mapDrawn) {
    installHint.textContent = "Waiting for the map, so you can look at it first.";
  } else if (!acknowledged) {
    installHint.textContent =
      "Tick the box above to confirm the map is the layer you meant, and this " +
      "button will install it.";
  } else {
    installHint.textContent = "Ready. This writes the layer to this computer.";
  }
}

ackCheckbox.addEventListener("change", () => {
  const wasOff = isControlOff(installButton);
  updateInstallButton();
  // Ticking the box is the moment the button becomes usable, and a control
  // quietly becoming usable is a control nobody is told about. This is a
  // change the operator just caused, so saying so is not chatter.
  if (wasOff && !isControlOff(installButton)) {
    setStatus("pending", "Ready: the Install button below will now install this layer.");
  }
});

installButton.addEventListener("click", async () => {
  // The refusal `disabled` used to do for free — done here, so the button can
  // stay reachable and keep explaining itself.
  if (isControlOff(installButton)) return;
  if (!state.sessionId) return;
  setControlOff(installButton, true);
  setStatus("pending", "Installing…");
  let result;
  try {
    result = await call("/api/commit/" + encodeURIComponent(state.sessionId), {
      method: "POST",
    });
  } catch (error) {
    setStatus("error", "The tool did not answer. Nothing was installed.");
    updateInstallButton();
    return;
  }

  if (result.ok) {
    state.installed = true;
    setStatus("ok", "Installed.");
    updateInstallButton();
    return;
  }

  // 501 is the honest answer from this build: the route exists, installing is
  // F8-T6, and nothing was written. Saying so plainly beats a green tick that
  // is not true — an operator who believes a layer is installed stops checking.
  const code = result.payload && result.payload.error && result.payload.error.code;
  if (result.status === 501 || code === "not_implemented") {
    setStatus(
      "warn",
      "Nothing was installed: this build of the tool inspects and previews " +
        "only. Installing is not built yet. Nothing on this computer was " +
        "changed."
    );
  } else {
    setStatus("error", "Nothing was installed. " + errorText(result, "The tool refused"));
  }
  updateInstallButton();
});

// --------------------------------------------------------------------------
// first paint
// --------------------------------------------------------------------------

if (!adminToken) {
  setStatus(
    "error",
    "This page was opened without the key this run of the tool printed when " +
      "it started. Close this tab and open the address the tool printed."
  );
}
