# PLAN — Point-in-Polygon Service

**Status:** DRAFT — awaiting maintainer ratification (Gate 1).
**Contract:** `docs/specs/SPEC.md` (ratified 2026-07-06, frozen). This plan is
mutable; the spec is not.
**Learning budget:** 5% (ship mode) per `CLAUDE.md` — agent authors, maintainer
reviews every PR line-by-line and logs it.
**Deep spec required:** none — every v1 decision this plan takes is recorded
below (D1–D9) for maintainer adjudication at Gate 1; no gate currently needs a
chat-authored deep spec.

---

## 0. Proposed spec deltas (maintainer ratifies; the agent never edits the spec)

- **Δ1 — §6 scaffold paths.** The spec lists `docs/SPEC.md` among the init
  files. Per the maintainer's ecosystem-wide documentation convention
  (2026-05-25: specs and plans live in separate **plural** directories), C0
  placed the spec at `docs/specs/SPEC.md` and this plan at `docs/plans/PLAN.md`,
  and added `scripts/` (data-pipeline home) to the tree. Proposed delta: §6
  reads `docs/specs/SPEC.md`, and the tree list gains `docs/specs/`,
  `docs/plans/`, `scripts/`. The mutable `CLAUDE.md` pointers were already
  updated to match; the spec text itself is untouched pending ratification.

No other deltas proposed. The API contract (§4), the geocoding ladder (§5), and
the non-negotiables (§9) are implemented as written.

---

## 1. Architecture (module map)

```
app/
  main.py             FastAPI app factory; routes for §4; serves static/ (F4, F6)
  models.py           Pydantic models = the §4 shapes; OpenAPI generates from these
  errors.py           {"error": {"code", "message"}} envelope; 400/404/502 handlers
  config.py           Startup config: layers + geocoder chain from config.toml
  lookup.py           PolygonLookup — the generic engine (F2)
  geocoding/
    base.py           Geocoder protocol + GeocodeResult (F3)
    arcgis.py         ArcGISRestGeocoder — §5 modes 1 AND 2 (same class, config differs)
    census.py         CensusGeocoder — free public fallback (F5)
    nominatim.py      NominatimGeocoder — opt-in fallback (F5)
    local_points.py   LocalAddressPointGeocoder — §5 mode 3, air-gapped (F5)
    chain.py          GeocoderChain — ordered fallback per config (F5)
scripts/
  build_data.py       F1 pipeline: open data → GeoPackage, validates, documents
config.toml           Layers + provider chain; env-var NAMES for creds, never values
data/
  layers.gpkg         Shipped: police_districts + municipalities layers (F1)
static/
  index.html, app.js, style.css   Decoupled frontend — talks JSON only (F6)
tests/                pytest; synthetic-fixture unit tests + marked integration tests
docs/
  data-provenance.md  Source URLs, retrieval dates, licenses, CRS, field mapping (F1)
```

Request flow, headline endpoint: `GET /locate?address=…&layer=…` →
`GeocoderChain` (address → WGS84 point + score) → `PolygonLookup` (reproject
point to layer's native CRS, spatial-index query) → §4 response. `POST /locate`
skips the geocoder entirely — the pure generic use.

## 2. Decisions taken (D1–D9)

- **D1 — Config is TOML, read with stdlib `tomllib`.** One `config.toml`
  (path overridable via `PIP_CONFIG` env var) holding `[[layers]]` and
  `[[geocoders]]` tables. No YAML dependency; earns nothing (rule 1).
- **D2 — Credentials by reference.** A provider entry names the env var that
  holds its token (`token_env = "COOK_GEOCODER_TOKEN"`); the value comes from
  the environment at startup. Nothing secret can be committed by construction
  (§9). Mode 1 vs mode 2 (§5) is literally the same adapter class with a
  different `base_url`/`token_env` — proven by a config-only test.
- **D3 — Reproject the point, not the layer.** Layers load and stay in their
  native CRS (EPSG:3435 for Cook County); each query point is transformed
  WGS84 → layer CRS via pyproj. One-point transforms are cheap, and the
  authoritative geometry is never resampled. *(ArcGIS equivalent: letting
  `SelectLayerByLocation` honor the layer's spatial reference rather than
  running `Project_management` on the whole feature class.)*
- **D4 — Spatial predicate is `covers`, first-match wins.** `covers` (unlike
  `contains`) returns true for a point exactly on the boundary. A point on a
  shared edge can legitimately hit two polygons; v1 returns the first by a
  stable sort on the layer's first configured attribute, documented in the
  endpoint docstring. Ambiguity surfacing is a candidate retrofit (F2b), not v1.
- **D5 — No-PII is enforced, not promised.** The invariant (§9) has three
  concrete teeth: (a) app loggers never log query params; (b) shipped run
  commands and the Dockerfile pass `--no-access-log` to uvicorn — the access log
  is where the address in a GET query string would otherwise be persisted;
  (c) a pytest captures all log output during a request and asserts the queried
  address string appears nowhere. The docs state plainly that a reverse proxy
  an agency adds in front (nginx etc.) has its own access log they must config.
- **D6 — Score normalized to 0–100 per adapter.** ArcGIS already 0–100; Census
  has no score (exact match → 100); Nominatim `importance` × 100, rounded.
  Each adapter documents its mapping in its docstring.
- **D7 — Chain falls through on transport failure only.** A provider that
  *errors* (timeout, 5xx, connect) → next provider; a provider that *answers*
  "no candidates" is authoritative → `matched: false`. Rationale: silently
  mixing providers on no-match makes scores incomparable and answers
  non-reproducible. Flippable by the maintainer at Gate 3 review if recall
  matters more.
- **D8 — Offline matching is exact-after-normalization in v1.** Uppercase,
  strip punctuation, standardize directionals and street suffixes (the USPS
  Publication 28 abbreviation table, embedded as a small dict). Exact match on
  normalized house number + street (+ optional city/zip filter) → score 100;
  otherwise no match. Fuzzy matching is a candidate retrofit (F5c), not v1
  (rule 1). The repo ships a small address-point *fixture*; the full Cook
  County address-point GeoPackage (~2M points) is built locally by the agency
  via the pipeline — documented, not committed.
- **D9 — Frontend is plain HTML/CSS/JS, zero external requests.** `fetch()`
  against the JSON API, no framework, no CDN, so it works air-gapped. A Leaflet
  map is explicitly deferred (vendoring a map stack is not earned by v1).

**Dependency budget (rule 1).** Runtime: `fastapi`, `uvicorn`, `geopandas`
(brings `shapely`, `pyproj`, `pyogrio`, `pandas`), `httpx`. Dev: `pytest`,
`respx`. Anything else must be argued into this list in a PR.

## 3. Features → tasks

### F1 — Data pipeline → GeoPackage
*Deliverable: `data/layers.gpkg` (police_districts + municipalities), rebuilt
by one documented script; provenance recorded.*

- **F1-T1** — `scripts/build_data.py`: fetch Chicago police district boundaries
  (Chicago Data Portal GeoJSON export) and Cook County municipalities (Cook
  County GIS open data); pin exact dataset URLs in the script and provenance
  doc. *(ArcGIS equivalent noted in docstrings: replaces `.gdb` feature classes
  + `FeatureClassToFeatureClass`.)*
- **F1-T2** — Write both layers into `data/layers.gpkg` in their native CRS
  with normalized attribute names (`dist_num`, `dist_name`, …); validate
  expected columns and feature counts against the source (spec §4 example
  expects 22 districts — verify against the authoritative source at build
  time and record the count in provenance).
- **F1-T3** — `docs/data-provenance.md`: source URLs, retrieval date, license
  terms, CRS, field mapping, feature counts.
- **F1-T4** — Tests: pipeline output schema test (layers, columns, CRS,
  non-empty); the shipped `.gpkg` is committed and the test runs against it.
- **F1-T5** — CI: GitHub Actions workflow running `pytest` on every PR (the
  enforcement surface for H2/H3 below).

**Verify:** `python scripts/build_data.py && pytest tests/test_data.py` —
output shown in PR.

### F2 — Generic `PolygonLookup` engine
*Deliverable: config-driven engine, no HTTP, no geocoding.*

- **F2-T1** — `app/config.py`: load `config.toml`; `[[layers]]` entries
  (id, name, path, layer, attributes, source); fail fast at startup on a
  missing file/layer.
- **F2-T2** — `app/lookup.py` `PolygonLookup`: load layers once at startup,
  build the shapely STRtree spatial index, `locate(lon, lat, layer_id)` →
  reproject per D3, query per D4, return attribute dict or
  `point_outside_all_polygons`. *(ArcGIS equivalents in docstrings:
  `SelectLayerByLocation` / Identify.)*
- **F2-T3** — Edge cases: unknown layer id (typed error → 404 at F4), point on
  a shared boundary (D4), point far outside, antimeridian/garbage coordinates
  rejected by validation.
- **F2-T4** — Unit tests against a tiny synthetic GeoPackage built by a fixture
  (two touching squares in EPSG:3435) — fast, deterministic, independent of
  real data. Plus one test against the shipped `layers.gpkg`.

**Verify:** `pytest tests/test_lookup.py`.

### F3 — `Geocoder` interface + ArcGIS adapter (§5 modes 1–2)

- **F3-T1** *(named in the spec)* — Pin the live public Cook County
  `AddressLocator` `GeocodeServer` endpoint: confirm the
  `findAddressCandidates` URL, record it in `config.toml` and
  `docs/data-provenance.md` with a captured sample response for tests.
- **F3-T2** — `app/geocoding/base.py`: `Geocoder` protocol
  (`geocode(address) -> GeocodeResult`), `GeocodeResult` model
  (`matched`, `point`, `score`, `matched_address`, `provider`).
- **F3-T3** — `ArcGISRestGeocoder` (httpx): `base_url`, optional `token_env`
  (D2), timeout, score passthrough (D6), transport errors raised as
  `GeocoderUnavailable` (→ 502 at F4). One class serves §5 mode 1 *and* mode 2;
  a test constructs it from a "private server" config to prove config-only
  switching.
- **F3-T4** — Tests with `respx`-mocked HTTP using the F3-T1 captured
  responses: match, no-match, timeout, HTTP 500, token attachment.

**Verify:** `pytest tests/test_geocoding_arcgis.py` (all-mocked; suite passes
with no network).

### F4 — FastAPI service (the §4 contract)

- **F4-T1** — App factory; `GET /health` (version single-sourced from package
  metadata); wire config/lookup/geocoder as startup state.
- **F4-T2** — `GET /layers` from config + loaded layer stats.
- **F4-T3** — `GET /geocode` with optional `provider=` override.
- **F4-T4** — `GET /locate` (geocode → lookup, all three §4 response shapes)
  and `POST /locate` (body `{lat, lon, layer}`, no geocoding).
- **F4-T5** — Error envelope + handlers: 400 (validation), 404 (unknown layer),
  502 (`GeocoderUnavailable`) — exactly the §4 error model.
- **F4-T6** — No-PII enforcement per D5: logging config, `--no-access-log` in
  all shipped run commands, and the log-capture test.
- **F4-T7** — OpenAPI contract test: the path+method set in `/openapi.json`
  equals §4's set exactly, and `/docs` serves. (Generated-from-code is the §9
  guarantee; this test catches an endpoint added or renamed outside the spec.)
- **F4-T8** — End-to-end success-criteria tests: a known Chicago address →
  its correct police district (geocoder mocked with the F3-T1 captured real
  response; district asserted against the authoritative boundary data); a
  suburban Cook County address → `found: false, reason:
  point_outside_all_polygons`; an unmatchable address → `geocode.matched:
  false` and no `match` key.

**Verify:** `pytest` (full suite) + manual `uvicorn app.main:app` smoke shown
in the PR (`curl /health`, `/locate`, `/openapi.json`).

### F5 — Fallback / alternate geocoders + the chain (§5 mode 3)

- **F5-T1** — `CensusGeocoder` (US Census `onelineaddress` API — free, no key);
  score mapping per D6.
- **F5-T2** — `NominatimGeocoder`: honors the public-instance usage policy
  (identifying User-Agent, ≤1 req/s note); **not** in the default chain —
  documented opt-in (a public shared instance is not for service traffic);
  fully supported when self-hosted (§5 mode 2).
- **F5-T3** — Address normalization module for D8 (pure functions, embedded
  USPS Pub. 28 tables) — shared by `local_points.py` and reusable elsewhere.
- **F5-T4** — `LocalAddressPointGeocoder`: match against an address-point
  GeoPackage per D8. Repo ships a small fixture gpkg; `scripts/build_data.py`
  grows a `--address-points` mode that builds the full county file locally
  (documented for the locked-workstation agency; the big file is never
  committed).
- **F5-T5** — `GeocoderChain` per D7 + `provider=` override; default chain in
  `config.toml`: `cook_county_arcgis → census`.
- **F5-T6** — **Offline acceptance test** (the §7 locked-workstation
  criterion): a pytest fixture monkeypatches socket creation so any network
  attempt fails the test, then geocodes a fixture address via
  `local_points` and locates its district end-to-end.

**Verify:** `pytest tests/test_geocoding_fallbacks.py tests/test_offline.py`.
PR may split as F5-pr1 (T1–T2, T5) / F5-pr2 (T3–T4, T6) if review size warrants.

#### F5b — optional isolated `arcpy` adapter (§5.4) — **gated, not v1**
Separate installable package `plugins/arcpy-locator/` (own `pyproject.toml`,
own license note), discovered via a `point_in_polygon.geocoders` entry-point
group so the core never imports it. Core gains only the generic entry-point
discovery (a dozen lines, testable with a dummy plugin). Planned in detail only
when an agency actually needs it; it is never core, never default (§9).

### F6 — Static frontend + Dockerfile + deploy docs

- **F6-T1** — `static/index.html` + `app.js` + `style.css` per D9: address
  form → `GET /locate` → district card; the three failure shapes (no geocode,
  outside all polygons, service error) each get a clear human message.
  WCAG-conscious: labels, contrast, keyboard use.
- **F6-T2** — Serve `static/` from FastAPI (`StaticFiles`) so the default
  install is one process — while the frontend keeps consuming only the public
  JSON API (decoupled: it can be hosted anywhere else unchanged).
- **F6-T3** — `Dockerfile`: `python:3.12-slim`, non-root user, `--no-access-log`
  (D5), single `uvicorn` process.
- **F6-T4** — Deploy docs: shoestring options for C1 (free-tier PaaS,
  $5 VPS, on-prem Windows box), the offline/air-gapped install path (pip
  wheelhouse built on a connected machine, carried over — geopandas wheels
  cover Windows), and the reverse-proxy access-log warning from D5.

**Verify:** `docker build` + container smoke test; frontend exercised against
the running service (screenshot in PR).

## 4. Gates & sequence

| Gate | Crossing | Opens |
|---|---|---|
| Gate 0 | SPEC ratified — **CLEARED 2026-07-06** | Planning |
| Gate 1 | Maintainer ratifies this plan + Δ1 | F1 build |
| Gate 2 | F1 shipped, adversarially reviewed, PR logged | F2 |
| Gate 3 | F2 shipped … | F3 |
| Gate 4 | F3 shipped … | F4 |
| Gate 5 | F4 shipped … | F5 |
| Gate 6 | F5 shipped … | F6 |
| Gate 7 | F6 shipped … | v1 tag `v1.0.0`; F5b/retrofits by separate gate |

Strictly serial by default — the 5% practice means one PR in review at a time.
F3 has no code dependency on F2 (both sit on `app/config.py`), so the
maintainer MAY authorize them in parallel at Gate 3 if review bandwidth allows.

Every feature: build under **ultracode** → adversarial-reviewer pass →
verification evidence in the PR → maintainer review + log → gate.

## 5. Hooks (deterministic enforcement; surface stated honestly)

| ID | Invariant (spec §) | Mechanism | Surface |
|---|---|---|---|
| H1 | Spec immutability (§ preamble, rule 7) | Claude Code `PreToolUse` hook denying Edit/Write on `docs/specs/SPEC.md` | local hook — needs maintainer approval of `.claude/settings.json` (proposed as **C3**) |
| H2 | Tests pass before merge | `pytest` workflow (F1-T5) | CI — plus branch protection once the GitHub repo exists (**C4**) |
| H3 | No committed credentials (§9) | secret-pattern scan step in the same workflow; `.env*` gitignored | CI + `.gitignore` |
| H4 | No PII in logs (§9) | log-capture test (F4-T6) | test suite (CI-enforced via H2) |
| H5 | OpenAPI ≡ implementation ≡ §4 (§9) | generated-from-code by construction + contract test (F4-T7) | test suite |

CLAUDE.md prose covers the rest; anything the agent drifts past twice gets
promoted to a hook here.

**`H<N>` is a plan-local label, not a work-chunk ID.** The `H` numbers exist
only to trace *spec invariant → mechanism → owning task* in this table (the same
register-only role Milestone/Risk IDs play in the OCS grammar). An `H<N>` MUST
NOT appear in a commit, branch, or PR title — the building work always carries
the `F`/`C`/`T` ID of its owning task (e.g. the H4 log test ships under
`F4-T6`, not `hook(H4):`). This keeps the project's structural grammar exactly
three categories — Feature, Chore, Task. *(Ratified 2026-07-06; promoted to the
`cc-md` skill's Hooks guidance the same day.)*

## 6. Chores (human track)

- **C0** — scaffold the skeleton — **DONE 2026-07-06** (this session;
  `bootstrap.py` is the §6 idempotent, stdlib-only, re-runnable form).
- **C1** — choose the shoestring deploy host (inputs arrive with F6-T4).
- **C2** — register any accounts/keys a chosen provider needs (none needed for
  the default chain — that's the point).
- **C3** *(proposed)* — approve the H1 spec-protection hook in
  `.claude/settings.json`.
- **C4** *(proposed)* — create the GitHub remote; enable branch protection +
  rebase-and-merge-only.

## 7. Success-criteria map (spec §7 → where proven)

| §7 criterion | Proven by |
|---|---|
| Known Chicago address → correct district | F4-T8 e2e test + F3-T1 captured live response |
| `pip install` + `uvicorn`, no Esri, no keys | F4 verify step; dependency budget (D-list) |
| Offline mode geocodes + locates, no internet | F5-T6 socket-blocked acceptance test |
| `/openapi.json` matches implementation | F4-T7 contract test |
| Outside-Chicago point → clean `found: false` | F2-T3 + F4-T8 |
| C0 idempotent, non-clobbering scaffold | `bootstrap.py` re-run demo (C0 evidence, this session) |

## 8. Risks & open questions

- **Cook County endpoint drift/downtime** — pinned + sample-captured at F3-T1;
  chain falls back to Census (D7); a captured-response test suite means CI
  never depends on the live endpoint.
- **Nominatim public-instance policy** — mitigated by keeping it out of the
  default chain (F5-T2).
- **Source-portal schema changes** — pipeline validates expected columns
  (F1-T2); provenance pins retrieval dates (F1-T3).
- **geopandas footprint on locked-down Windows** — wheels exist for all deps;
  offline wheelhouse path documented at F6-T4. If an environment still can't
  take the stack, a slimmer `shapely`+`pyogrio`-only engine is a possible
  retrofit (F2c) — not v1.
- **Open:** exact Cook County municipalities dataset URL — resolved at F1-T1
  and recorded in provenance.
- **Open:** default `layer` when `GET /locate` omits it — v1 requires the
  param explicitly (400 if missing); revisit if the frontend wants a default.

-----
July 6, 2026 — plan drafted (ultrathink) against SPEC as ratified 2026-07-06.

#AI/Claude
