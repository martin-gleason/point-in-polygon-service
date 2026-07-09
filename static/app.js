"use strict";

// Minimal test UI for the Point-in-Polygon Service. Talks only to the JSON API
// on the same origin — no external requests, so it works offline / air-gapped.

const form = document.getElementById("locate-form");
const layerSelect = document.getElementById("layer");
const layerHint = document.getElementById("layer-hint");
const resultCard = document.getElementById("result-card");
const resultEl = document.getElementById("result");
const rawEl = document.getElementById("raw");

// Populate the layer dropdown from GET /layers, so adding a layer in config.toml
// shows up here with no code change.
async function loadLayers() {
  try {
    const response = await fetch("/layers");
    if (!response.ok) throw new Error(`/layers returned ${response.status}`);
    const data = await response.json();
    layerSelect.innerHTML = "";
    for (const layer of data.layers) {
      const option = document.createElement("option");
      option.value = layer.id;
      option.textContent = `${layer.name} (${layer.feature_count} features)`;
      option.dataset.attributes = layer.attributes.join(", ");
      layerSelect.appendChild(option);
    }
    updateLayerHint();
  } catch (error) {
    layerHint.textContent = `Could not load layers: ${error.message}`;
  }
}

function updateLayerHint() {
  const selected = layerSelect.selectedOptions[0];
  layerHint.textContent = selected
    ? `Returns: ${selected.dataset.attributes}`
    : "";
}

function render(status, detail, payload) {
  resultCard.hidden = false;
  resultEl.className = status; // "found" | "outside" | "error"
  resultEl.textContent = detail;
  rawEl.textContent = payload ? JSON.stringify(payload, null, 2) : "";
  resultCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function locate(lat, lon, layer) {
  render("pending", "Locating…", null);
  let response;
  try {
    response = await fetch("/locate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lat, lon, layer }),
    });
  } catch (error) {
    render("error", `Network error: ${error.message}`, null);
    return;
  }

  const payload = await response.json().catch(() => null);

  if (!response.ok) {
    const message =
      payload && payload.error
        ? `${payload.error.code}: ${payload.error.message}`
        : `Request failed (${response.status})`;
    render("error", message, payload);
    return;
  }

  const match = payload.match;
  if (match.found) {
    const fields = Object.entries(match.feature)
      .map(([key, value]) => `${key} = ${value === null ? "—" : value}`)
      .join(", ");
    render("found", `Found in ${payload.layer}: ${fields}`, payload);
  } else {
    render("outside", `Not found: ${match.reason}`, payload);
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const lat = parseFloat(document.getElementById("lat").value);
  const lon = parseFloat(document.getElementById("lon").value);
  const layer = layerSelect.value;
  if (Number.isNaN(lat) || Number.isNaN(lon)) {
    render("error", "Enter a numeric latitude and longitude.", null);
    return;
  }
  locate(lat, lon, layer);
});

layerSelect.addEventListener("change", updateLayerHint);

for (const button of document.querySelectorAll(".preset")) {
  button.addEventListener("click", () => {
    document.getElementById("lat").value = button.dataset.lat;
    document.getElementById("lon").value = button.dataset.lon;
    layerSelect.value = button.dataset.layer;
    updateLayerHint();
    locate(
      parseFloat(button.dataset.lat),
      parseFloat(button.dataset.lon),
      button.dataset.layer
    );
  });
}

loadLayers();
