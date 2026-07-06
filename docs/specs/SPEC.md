# SPEC — Point-in-Polygon Service

> **STATUS: RATIFIED — frozen contract as of 2026-07-06.**
> This document is now immutable. The agent may *propose* deltas, but the
> maintainer ratifies them before they are real; the agent never silently edits
> the spec it is held to. Planning and implementation are carried out by Claude
> Code — the plan in **ultrathink**, the build in **ultracode** — working from
> this contract.

## 1. Purpose (why this exists)

A fully open-source web service that answers one question: **given an address
(or a raw point), which polygon of a given layer contains it?**

The engine is *layer-agnostic*. The first configured layer is
Chicago / Cook County police districts, so the flagship use is *"what police
district does this address fall in?"* — but a community group could point the
same service at voting precincts, aldermanic wards, service areas, or any other
polygon set with zero code changes.

It is built to run on **open-source tools** and on **shoestring infrastructure**,
so nonprofits, mutual-aid groups, and small government offices can self-host it
for near-$0.

This is a port of the Esri/`arcpy` prototype at `arcpy_point_in_polygon_app`.
The root goal is to **remove the Esri dependency chain** (`arcpy`, the `.loc`
composite locator, the proprietary `.gdb`) from the default path — not to wrap
it behind a nicer API.

**Deployment reality this spec must respect.** The prototype used `arcpy` for a
concrete reason: it was built on locked-down *Court equipment* with no access to
Court or county *servers*. Some agencies will be in the same position —
firewalled, air-gapped, or restricted to a single Esri workstation. The design
must serve those agencies too (see §5), without making anyone else pay the Esri
tax.

## 2. In scope

- A generic point-in-polygon lookup over a configurable set of polygon layers
  stored as **GeoPackage** (open format).
- Address geocoding through a **pluggable provider interface** whose endpoint,
  authentication, and network reachability are all **configuration, not code**
  (see §5) — with Cook County's public ArcGIS `AddressLocator` as the default
  provider and free / offline / private fallbacks.
- A JSON/REST API whose **OpenAPI contract is generated from code** (so it
  cannot drift), served at `/openapi.json` and `/docs`.
- A minimal, decoupled **static frontend** that consumes the JSON API.
- **Police districts as the first shipped dataset**, sourced from open data.
- A **scaffolding bootstrap** that stands the project up from a parent projects
  directory (see §6).

## 3. Out of scope (explicit)

- **Officer / SPO assignment logic** — the prototype's `unit_structure`,
  `/units`, and `/assign`. This is an internal probation function with very
  different stakes from a public "find your district" tool. It is intentionally
  excluded from this public engine; if it is ever needed it belongs in a
  separate, clearly bounded downstream service. *(A deliberate equity boundary,
  not an oversight.)*
- **Persistence of queried addresses or any PII.** The service answers a query
  and forgets it. No logging or retention of addresses beyond the request.
- **Batch geocoding of large address lists.** v1 is single-address; batch may be
  a later feature.
- **Authentication in v1 of the public API.** (Distinct from geocoder-provider
  auth in §5.) Deployment-level protection is a Chore, not a v1 feature.

## 4. API contract (v1)

JSON over HTTP. All coordinates are **WGS84 (EPSG:4326), `lon`/`lat`**; internal
reprojection to each layer's native CRS (Cook County data is EPSG:3435, Illinois
State Plane East) is handled server-side. OpenAPI generated from the Pydantic
models, served at `/openapi.json` and `/docs`.

**`GET /health`**
→ `200 {"status": "ok", "version": "<semver>"}`

**`GET /layers`** — list configured polygon layers.
→ `200 {"layers": [{"id": "police_districts", "name": "Chicago Police Districts",
"feature_count": 22, "attributes": ["dist_num", "dist_name"], "source": "<provenance>"}]}`

**`GET /geocode?address=<str>&provider=<optional>`** — geocode only.
→ `200 {"query": "<address>", "matched": true, "point": {"lon": .., "lat": ..},
"score": <0-100>, "matched_address": "<str>", "provider": "cook_county_arcgis"}`
→ `200 {"query": "...", "matched": false, "point": null, "provider": "..."}` when
no candidate is found.

**`GET /locate?address=<str>&layer=<id>&provider=<optional>`** — the headline
endpoint: geocode → point-in-polygon.
→ `200 {"query": "...", "geocode": { <geocode object above> }, "layer":
"police_districts", "match": {"found": true, "feature": { <layer attributes> }}}`
→ `"match": {"found": false, "reason": "point_outside_all_polygons"}` when the
address geocodes but lands in no polygon (e.g. in Cook County but outside
Chicago).
→ `"geocode": {"matched": false}` with no `match` when the address fails to
geocode.

**`POST /locate`** — point-in-polygon only, no geocoding (the pure generic use).
body: `{"lat": .., "lon": .., "layer": "police_districts"}`
→ same `match` shape as above.

**Error model.** `400` missing/invalid params (Pydantic validation); `404`
unknown `layer`; `502` upstream geocoder failure. Body:
`{"error": {"code": "<slug>", "message": "<human message>"}}`.

## 5. Geocoding deployment modes (the pluggable ladder)

Every mode sits behind one `Geocoder` interface (address in → point + score +
matched address out). The concrete provider, its endpoint URL, its
authentication (token / API key / basic auth — supplied via environment or
config, **never hardcoded, never committed**), and whether it needs the public
internet at all are **configuration**. The service picks a provider (or an
ordered fallback chain) from config at startup. Modes, preferred first:

1. **Public agency geocoder** — a public ArcGIS `GeocodeServer` (Cook County's
   `AddressLocator`) or any public HTTP geocoder. The default.
2. **Private / internal geocoder** — the *same* `ArcGISRestGeocoder` pointed at a
   firewalled internal ArcGIS Server (private base URL + token), or a
   self-hosted open-source geocoder (Nominatim / Pelias) on the agency's own
   hardware. No internet egress required; only config differs from mode 1.
3. **Fully offline / air-gapped** — `LocalAddressPointGeocoder` matches the
   address against a **local address-point GeoPackage** in pure FOSS. No server,
   no internet. This is the primary answer for a locked-down workstation: the
   authoritative address points many Esri locators are built from are published
   as open data (Cook County's are), so they can be loaded onto the machine once
   and used forever.
4. **Last resort — optional `arcpy` locator adapter** — for an environment that
   truly has nothing but a locked ArcGIS Desktop workstation and an existing
   `.loc` locator. Provided as a **separately-installed, isolated plugin** behind
   the same interface. It is **never a core dependency, never the default, and
   never required** to run the service; installing it is an explicit opt-in that
   only the opting agency pays for.

## 6. Project scaffolding (maintainer Chore)

Standing up the project skeleton is a **Chore** (`C0`) the maintainer performs
before feature work begins — **not** an agent-built feature. Run from the parent
directory that holds all the maintainer's projects, the scaffold produces a new
`<project_name>/` subdirectory containing:

- the full tree: `app/`, `app/geocoding/`, `data/`, `static/`, `tests/`,
  `docs/`, `.claude/agents/`;
- the init files: `CLAUDE.md`, `docs/SPEC.md`, `docs/conventions.md`,
  `.claude/agents/adversarial-reviewer.md`, `pyproject.toml`, `.gitignore`,
  `README.md`, and a `LICENSE` carrying the AGPLv3 text.

Requirements on however the Chore is carried out: **idempotent and
non-clobbering** (re-running only fills in what is missing), and
**cross-platform** — the target environments skew locked-down/Windows, so a
Python-stdlib bootstrap is preferred over a shell script. It should be
documented so a maintainer coming from ArcGIS or from QGIS can follow it without
prior context (rule 2).

## 7. Success criteria

- A known Chicago address returns the **correct** police district end-to-end,
  verified against authoritative Chicago PD district boundaries.
- The default install runs with `pip install` + `uvicorn` on a machine with **no
  Esri software and no API keys**.
- The **offline mode (§5.3)** geocodes and locates a district on a machine with
  **no internet and no Esri** — the locked-workstation acceptance test.
- `/openapi.json` matches the implemented endpoints exactly (generated, never
  hand-maintained).
- A point known to be outside Chicago returns `found: false` with a clear
  reason — not a 500.
- Scaffolding (C0) run in an empty parent directory produces a runnable project
  skeleton and does not clobber existing work when re-run.

## 8. Feature list (structural IDs; implementation detail lives in `PLAN.md`)

- **F1** — Data pipeline → GeoPackage (police districts + municipalities),
  provenance documented.
- **F2** — Generic `PolygonLookup` engine (config-driven layers).
- **F3** — `Geocoder` interface + ArcGIS adapter, endpoint/auth/network all
  configurable per §5 modes 1–2 (**F3-T1** pins the live public `GeocodeServer`
  under Cook County's `AddressLocator`).
- **F4** — FastAPI service exposing the contract in §4.
- **F5** — Fallback / alternate geocoders: US Census, Nominatim, and the offline
  `LocalAddressPointGeocoder` (§5.3) + the provider chain.
  - **F5b** — Optional isolated `arcpy` locator adapter (§5.4), shipped as a
    separately-installed plugin.
- **F6** — Static frontend + Dockerfile + deploy docs.
- **Chores (human):** **C0** scaffold the project skeleton (§6) from the projects
  directory before feature work; **C1** choose the shoestring deploy host;
  **C2** register any accounts/keys a chosen provider needs.

## 9. Non-negotiables

- **No proprietary dependency in the core or default install.** The engine, the
  API, and every default-installed geocoder adapter run on open-source software
  with no ArcGIS license and no mandatory paid key. An agency whose environment
  forces proprietary tooling MAY add an optional adapter (§5.4) that depends on
  it — but only as a separately-installed, isolated plugin behind the standard
  `Geocoder` interface, never core, never default, never required.
- **No persistence of queried addresses / PII.**
- **Provider credentials never hardcoded or committed** — environment/config
  only.
- **OpenAPI generated from code** — the spec cannot drift from the
  implementation.
- **AGPLv3** — copyleft that holds even when the service is run over a network.

-----
July 6, 2026

#AI/Claude
