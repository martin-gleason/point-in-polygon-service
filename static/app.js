"use strict";

// Minimal test UI for the Point-in-Polygon Service. Talks only to the JSON API
// on the same origin — no external requests, so it works offline / air-gapped.

const form = document.getElementById("locate-form");
const layerSelect = document.getElementById("layer");
const layerHint = document.getElementById("layer-hint");
const resultEl = document.getElementById("result");
const rawDetails = document.getElementById("raw-details");
const rawEl = document.getElementById("raw");

// Toggle aria-invalid so an invalid field is conveyed to assistive tech by more
// than the visual (red) border alone.
function setInvalid(el, invalid) {
  if (invalid) {
    el.setAttribute("aria-invalid", "true");
  } else {
    el.removeAttribute("aria-invalid");
  }
}

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
  // "found" | "outside" | "error" | "pending" — drives colour AND the message
  // text, so state never depends on colour alone.
  resultEl.className = `result ${status}`;
  resultEl.textContent = detail;
  if (payload) {
    rawEl.textContent = JSON.stringify(payload, null, 2);
    rawDetails.hidden = false;
  } else {
    rawEl.textContent = "";
    rawDetails.hidden = true;
  }
  resultEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
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
  const latEl = document.getElementById("lat");
  const lonEl = document.getElementById("lon");
  const lat = parseFloat(latEl.value);
  const lon = parseFloat(lonEl.value);
  const layer = layerSelect.value;
  const latBad = Number.isNaN(lat);
  const lonBad = Number.isNaN(lon);
  setInvalid(latEl, latBad);
  setInvalid(lonEl, lonBad);
  if (latBad || lonBad) {
    render("error", "Enter a numeric latitude and longitude.", null);
    (latBad ? latEl : lonEl).focus();
    return;
  }
  locate(lat, lon, layer);
});

// Address search: geocode + point-in-polygon via GET /locate.
const addressForm = document.getElementById("address-form");

async function locateAddress(address, layer) {
  render("pending", "Geocoding…", null);
  let response;
  try {
    const query = `address=${encodeURIComponent(address)}&layer=${encodeURIComponent(layer)}`;
    response = await fetch(`/locate?${query}`);
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

  if (!payload.geocode.matched) {
    render("outside", `Could not geocode “${payload.query}”.`, payload);
    return;
  }

  const where = payload.geocode.matched_address || payload.query;
  const match = payload.match;
  if (match && match.found) {
    const fields = Object.entries(match.feature)
      .map(([key, value]) => `${key} = ${value === null ? "—" : value}`)
      .join(", ");
    render("found", `${where} → ${payload.layer}: ${fields}`, payload);
  } else {
    render("outside", `${where} → not found: ${match ? match.reason : "no match"}`, payload);
  }
}

addressForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const addressEl = document.getElementById("address");
  const address = addressEl.value.trim();
  if (!address) {
    setInvalid(addressEl, true);
    render("error", "Enter an address.", null);
    addressEl.focus();
    return;
  }
  setInvalid(addressEl, false);
  locateAddress(address, layerSelect.value);
});

for (const button of document.querySelectorAll(".address-preset")) {
  button.addEventListener("click", () => {
    document.getElementById("address").value = button.dataset.address;
    locateAddress(button.dataset.address, layerSelect.value);
  });
}

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
