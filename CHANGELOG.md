# Changelog

All notable changes to the Point-in-Polygon Service. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project uses the F/C/T work
grammar from `docs/conventions.md`.

## v1.0.0 — 2026-07-17

First stable release. A FOSS, key-free point-in-polygon web service
(FastAPI + GeoPandas): given an address or a point, which polygon of a layer
contains it? The engine is generic and layer-agnostic; the shipped dataset is
Chicago / Cook County police districts and municipalities.

### Features

- **F1 — Data pipeline → GeoPackage.** `scripts/build_data.py` fetches Chicago
  police districts + Cook County municipalities from public open-data portals
  and writes `data/layers.gpkg` in native CRS with normalized attributes;
  provenance (URLs, dates, licenses, CRS, counts) recorded in
  `docs/data-provenance.md`.
- **F2 — Generic `PolygonLookup` engine.** Config-driven, no HTTP: loads layers
  once, builds a shapely STRtree per layer, reprojects the query point to each
  layer's CRS (D3), and answers with `covers` / stable-first-match (D4).
- **F3 — `Geocoder` interface + ArcGIS REST adapter.** One `Geocoder` protocol
  behind every mode; `ArcGISRestGeocoder` serves §5 modes 1 (public) and 2
  (private/internal) — the difference is config, not code. Credentials by
  env-var reference only (D2); address and token never leak into URLs, logs, or
  exceptions (§9).
- **F4 — FastAPI service (the §4 contract).** `GET /health`, `GET /layers`,
  `GET /geocode`, `GET /locate`, `POST /locate`; the §4 error model
  (400/404/502); OpenAPI generated from code and asserted to equal §4;
  `--no-access-log` no-PII enforcement (D5).
- **F5 — Fallback / alternate geocoders + the chain (§5 mode 3).** `Census`
  (free/no key) and `Nominatim` (opt-in / self-hostable) adapters; USPS Pub. 28
  address normalization; `LocalAddressPointGeocoder` for fully-offline /
  air-gapped matching; `GeocoderChain` with fall-through-on-transport-failure
  only (D7). Default chain: `cook_county_arcgis → census`.
- **F6 — Static frontend, Dockerfile, deploy docs.** Vanilla, air-gapped
  (zero external requests, D9) test UI polished to WCAG 2.1 AA; a non-root
  `python:3.12-slim` Dockerfile running a single uvicorn process with
  `--no-access-log`; `docs/deployment.md` covering shoestring hosts, the
  air-gapped pip-wheelhouse install path, and the reverse-proxy access-log
  warning.

### Non-negotiables held

- **No proprietary runtime deps** — no `arcpy`, no ArcGIS license, no required
  API keys. Runs on `pip install` + `uvicorn`.
- **No PII persistence** — the queried address is never written to disk or logs,
  and never appears in an exception message.
- **OpenAPI ≡ implementation ≡ SPEC §4** — enforced by a contract test.
- **Offline-capable** — an air-gapped install geocodes and locates with no
  internet, proven by a socket-blocked acceptance test (§7).
- **AGPL-3.0** — the license notice is intact; running it as a hosted service
  carries the source-availability obligation.

### Verification

Full test suite: **123 passing**. Every feature shipped under adversarial
review with the findings fixed. (Outstanding maintainer check: a real
`docker build` / `docker run` — the dev environment had no Docker daemon.)

### Not in v1 (gated separately)

- **F5b** — optional isolated `arcpy` locator plugin (§5.4).
- **F2b / F2c / F5c** — retrofits: boundary-ambiguity surfacing, a slim
  `shapely`-only engine, and fuzzy offline matching.
