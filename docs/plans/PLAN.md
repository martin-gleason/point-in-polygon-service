# PLAN — F8 (layer tool) + F7 (batch lookups) + F5b (`arcpy` plugin) + C5–C7

> The v1 plan is archived at `docs/plans/v1.0-archive/PLAN.md`; v1.0.0 shipped
> 2026-07-17. This is the mutable plan for work since.
>
> **F7 is BUILT** (branch `F7/batch-lookups`, awaiting PR review).
> **C5 and C6 are DONE.**
> **F8 is DRAFTED and blocked on Δ5 — not authorized to build.**

**F8 — the layer-installation tool** is next. Adding a polygon layer today means
editing `scripts/build_data.py` and hand-writing TOML: fine for a developer,
impossible for the campaign volunteer who is the actual user. F8 makes it drag a
file in, look at the shape, confirm. **Blocked on Δ5.**

The brief, paraphrased back (2026-08-02): a non-technical person adds a layer by
dragging a file in *or* picking it through a normal OS dialog; the file is
checked; **the shape is drawn on screen before anything is committed**; a failure
gives a specific code they can look up and act on. Two worked examples given —
a CPD district file in the wrong CRS should say so, and a stale Cook County
export should be visibly wrong on screen. Answers on 2026-08-02: build the local
tool now; write `config.toml` with a timestamped backup; accept zipped
shapefiles, loose shapefile parts, GeoJSON, and an ArcGIS REST/export URL; and
draw the candidate **over the layers already configured** so a misaligned
boundary is obvious.

*A fixture for this arrived with the brief:* `shapefiles/ward25_precincts.geojson`
— 17 valid polygons, EPSG:4326, columns `ward` / `precinct` / `ward_precinct`,
bounding box inside Chicago. A good F8-T2 happy-path case. **It is currently
untracked and not gitignored**, so decide whether it belongs in the repo before
the F8 branch is opened.

**F7 — batch point-in-polygon** shipped for review on 2026-08-01 (Δ3 ratified).
See §8 for its adversarial-review record.

**F5b — the optional `arcpy` locator plugin** stays last: it touches proprietary
software, cannot be tested in CI, and is gated on a spike needing Esri hardware
nobody on this project has.

---

## 0. Proposed spec deltas (maintainer ratifies; the agent never edits the spec)

- **Δ3 (REQUIRED — F7 is blocked until this is ratified).** SPEC §3 currently
  reads: *"**Batch geocoding of large address lists.** v1 is single-address;
  batch may be a later feature."* F7 is exactly that later feature. Propose:
  move the bullet out of §3 (out of scope) and into §2 (in scope) as *"Batch
  point-in-polygon over an address or coordinate list supplied as CSV, XLSX, or
  a published Google Sheet — processed in memory and never persisted (§9)"*;
  and add to the §8 feature list: **F7** batch lookups (CLI), **F7b** batch API
  endpoint. **The agent must not start F7 before the maintainer ratifies this.**
  Rule 7: the spec is immutable once ratified; this is a proposal, not an edit.
- **Δ4 (deferred to F7b).** §4's API contract gains a batch endpoint. Not
  proposed yet — draft it when F7b is planned, once the engine F7 builds has
  proven its shape. No point contracting an interface we haven't run.
- **Δ5 (REQUIRED — F8 is blocked until this is ratified).** Adding a polygon
  layer is currently a developer task: edit `scripts/build_data.py`, then
  hand-write a `[[layers]]` block. F8 makes it something a campaign volunteer
  can do. Propose adding to §2: *"A local, offline layer-installation tool that
  validates a candidate polygon file, shows the operator the shape before
  anything is committed, and writes the layer into `config.toml` — run on the
  operator's own machine, never exposed by the deployed service."*
  Add to §8: **F8** — layer installation tool.

  **This delta must also be explicit about the boundary it does not cross.**
  §3 defers authentication out of v1, so F8 introduces **no authenticated
  surface and no write path on the public service.** The tool is a separate
  ASGI application, bound to loopback, launched by hand, and never reachable
  from a deployment. Propose recording that in §3 as: *"Remote layer
  administration. F8 is local-only; an authenticated admin surface on a
  deployed instance is a separate feature requiring its own threat model."*
- **Δ1 — none required for F5b.** §5.4 and §8 already describe the feature and
  §9 already fixes its constraints. This plan implements the spec as written.
- **Δ2 (proposed, minor) — §7 success criteria.** F5b cannot be proven by CI
  (no `arcpy` on any runner). Propose adding one criterion: *"the optional
  `arcpy` adapter is validated once on a real ArcGIS Pro workstation, with the
  transcript recorded in the PR; CI proves only the translation layer against a
  fake `arcpy`."* This makes the verification limit part of the contract
  instead of an unwritten exception to "no feature is done on assertion."

---

## 1. Architecture

### F7 — batch

One new package, deliberately I/O-free at its core so the CLI (F7) and the
later endpoint (F7b) are two thin shells over the same engine:

```
app/batch/
  __init__.py
  sources.py    # CSV | XLSX | Google Sheet  ->  (headers, iterator of row dicts)
  runner.py     # rows -> geocode (or not) -> PolygonLookup.locate -> result rows
  writer.py     # original columns + result columns -> CSV | XLSX, injection-safe
scripts/batch_locate.py   # argparse shell over the above — the F7 deliverable
```

`runner.py` holds no file handling and no argument parsing: it takes an iterable
of rows plus a `PolygonLookup` and a `Geocoder`, and yields result rows. That is
what makes F7b a small feature later instead of a rewrite, and it is what lets
the whole engine be tested with a stub geocoder and no files at all.

**Nothing new is needed in `app/`.** `PolygonLookup.locate()` already answers
one point at a time and holds its spatial index in memory across calls; batch is
a loop over it, not a new engine. The geocoders already implement one interface.
F7 adds a caller, not a capability.

### F8 — the layer-installation tool

**Yes, it uses FastAPI — but it is a *separate application*, not routes on the
service.** That distinction is the whole security design, so it is worth being
exact about why.

Production runs `uvicorn app.main:app` (Dockerfile line 40). Anything reachable
from `app.main` is served publicly by the Render instance. If the upload and
config-write endpoints were routes on that app — even guarded by an environment
variable — they would be one misconfiguration away from letting anyone on the
internet upload a file and rewrite the layer config of a live service. A guard
you can turn off by accident is not a boundary.

So F8 builds a second ASGI app that the deployment never imports:

```
app/admin/                  # separate FastAPI app; app.main NEVER imports this
  main.py                   #   create_admin_app() — loopback only
  inspect.py                #   candidate file/URL -> GeoDataFrame + facts
  validate.py               #   the check registry -> error codes
  codes.py                  #   error-code registry: cause + plain-language fix
  preview.py                #   reproject, simplify, emit GeoJSON for the browser
  commit.py                 #   write the GeoPackage + append to config.toml
static/admin/               # vanilla HTML/JS/CSS, zero dependencies, offline
scripts/add_layer.py        # what the operator runs; binds 127.0.0.1, opens a browser
```

The operator runs one command; a browser opens on a loopback URL. The page gives
**both** interactions asked for from one implementation and with no new
dependency: HTML5 drag-and-drop, and an `<input type="file">` that opens the
native OS file dialog. It reuses the service's existing
`{"error": {"code", "message"}}` envelope (`app/errors.py`), so error codes are
not a new concept in this codebase — just a new registry of them.

Reused rather than rebuilt: `app/config.py`'s loader and `app/lookup.py`'s
`PolygonLookup` are what validate a candidate (see D26), and F7's
decompression-bomb guard is the model for the archive checks.

### F5b — the `arcpy` plugin

The core changes in exactly one place. Everything else lives outside the core
distribution.

```
app/geocoding/registry.py     # + lazy entry-point discovery (the ONLY core change)

plugins/arcpy-locator/        # separate installable distribution, never a core dep
  pyproject.toml              #   name: point-in-polygon-arcpy-locator
  README.md                   #   install into ArcGIS Pro's conda env
  LICENSE                     #   AGPLv3 + the arcpy note
  src/pip_arcpy_locator/
    geocoder.py               #   ArcpyLocatorGeocoder implements Geocoder
  tests/test_arcpy_locator.py #   runs against a FAKE arcpy module
```

`registry.py` already documents `_BUILDERS` as "the seam every provider plugs
into." F5b generalizes that seam rather than replacing it: built-in types stay a
literal dict; external types resolve through the `point_in_polygon.geocoders`
entry-point group (the group name the archived plan already proposed).

**Discovery must be lazy.** Iterating entry points at startup and calling
`.load()` on each would import `arcpy` into every deployment that merely has the
plugin installed — including one that never configures it. Instead, discovery
resolves an entry point only when a `[[geocoders]]` entry actually names that
`type`. A deployment with the plugin installed but unconfigured never imports
`arcpy`, never checks out a license seat, and pays nothing.

**Built-ins win.** An installed package must not be able to shadow
`arcgis_rest` or `census` by claiming that entry-point name. A collision is a
`ConfigError` at startup naming the offending distribution, not a silent
override.

## 2. Decisions

Numbering continues the archived plan's D1–D9.

### F7 decisions (D15–D21)

| ID | Decision | Why |
|---|---|---|
| **D15** | **Synchronous, streaming, in-memory. No job queue, no server-side state.** | A queue means storing a file of addresses — the exact thing §9 forbids. It would also drag in a broker and a state store, against golden rule 1. Rows stream through; nothing intermediate is written. |
| **D16** | **Google Sheets = published-CSV export only. No API key, no OAuth.** | A sheet shared "anyone with the link" exports over plain HTTP at `/export?format=csv&gid=…`. Zero dependencies, zero keys — the same reasoning that makes consuming a *public* ArcGIS REST endpoint acceptable (golden rule 2). Private sheets need OAuth credentials and are **out of scope**; the reader detects the login redirect and says so plainly. |
| **D17** | **XLSX via `openpyxl`, declared as an optional extra `[batch]`.** | It is the only genuinely new runtime dependency (confirmed absent from the venv). CSV and Google Sheets need nothing new — `httpx` is already a core dep. Gating XLSX behind an extra keeps the base install exactly as lean as it is today (golden rule 1); the reader raises a one-line "install `[batch]`" message. |
| **D18** | **Column mapping is explicit. Never guessed.** | `--address-column`, or `--lat-column`/`--lon-column`. When omitted, the error *suggests* likely headers it spotted but refuses to proceed. Silently guessing wrong on a 2,000-row file of real addresses produces plausible, wrong, unnoticed output — the worst failure mode this feature has. |
| **D19** | **Per-row isolation.** | A bad row gets `status` + `reason` columns and the run continues. Exit code is non-zero if any row failed, so it stays scriptable. One malformed address must never cost someone a 30-minute run. |
| **D20** | **Rate limiting on by default, and Nominatim refused outright.** | See R9 — this is the first caller in the project's history that can hammer a provider, and `nominatim.py`'s own docstring says the caller must throttle. Sequential with a configurable delay; `--provider nominatim` is rejected unless an explicit self-hosted override flag is passed. |
| **D21** | **Escape formula-injection on output.** | Any output cell starting with `= + - @`, tab, or CR is prefixed with `'`. `matched_address` is attacker-influenced text flowing into a CSV that someone will open in Excel. Cheap to prevent, invisible when forgotten. |

**On PII and the output file (D15 corollary).** §9 binds *the service*: it must
not persist queried addresses. The CLI's output file obviously contains the
user's addresses — that is the user's own data, on the user's own machine, at
their explicit request. No conflict. The distinction matters when F7b is
planned: the *endpoint* may hold rows in memory for the life of the request and
must write nothing.

### F8 decisions (D22–D28)

| ID | Decision | Why |
|---|---|---|
| **D22** | **Separate ASGI app, never a router on `app.main`.** | See §1. Enforced, not promised: a CI test asserts `app.main`'s route table contains no admin path and that importing `app.main` does not import `app.admin` (H10). |
| **D23** | **Loopback bind + one-time token + `Host` header check.** | Binding 127.0.0.1 stops the internet but not other accounts on a shared machine, and it does **not** stop DNS rebinding — a malicious web page the operator visits can resolve its own hostname to 127.0.0.1 and POST to the tool. The launcher mints a random token, opens the browser with it, and the app rejects any request without it or with an unexpected `Host`. Cheap, and the operator never sees it. |
| **D24** | **Nothing is written until the operator confirms.** | Inspection and preview are read-only. The candidate lives in a temp directory that is removed when the tool exits; only a confirmed commit copies data into `data/` and touches `config.toml`. |
| **D25** | **Commit = normalize to GeoPackage + append TOML + timestamped backup.** | Matches how every existing layer is stored. `config.toml` is **appended to as text**, not parsed and re-serialized: the stdlib has no TOML writer (`tomllib` is read-only), so appending a rendered block avoids a new dependency *and* preserves the operator's comments. Backup first, to `config.toml.bak-<timestamp>`. |
| **D26** | **Validate by actually loading it the way the service does.** | Before writing anything, build a real `LayerConfig` and load it through `PolygonLookup`, then run a point-in-polygon query against the candidate's own centroid. "The service will still start after this" becomes a tested fact rather than a hope. This is the check that matters most and is the cheapest to get right. |
| **D27** | **Two severities: blocking errors and acknowledgeable warnings.** | No CRS blocks. Invalid geometry only warns — the pipeline already repairs self-intersections with `make_valid`, so the tool offers the same repair and shows what changed. Treating a repairable condition as fatal would send a non-technical operator to QGIS for no reason. |
| **D28** | **Simplify geometry for display only.** | A boundary with 100k vertices will hang the browser. The preview simplifies with a tolerance derived from the bounding box and says it has done so. The **committed data is never simplified** — display fidelity and stored fidelity are separate concerns, and conflating them would quietly degrade the layer. |
| **D29** | **Harvest every vintage signal the format offers, and name the ones it doesn't.** | See the table below. Shapefiles carry no data version at all, so the tool must show what it *could* find (and say plainly when that is nothing) rather than leaving the operator to assume no news is good news. Fetch time and source URL are recorded at commit, so the *next* person has provenance this one didn't. |

#### What each format can actually tell us about vintage (measured 2026-08-02)

The maintainer asked whether a shapefile carries versioning. **It does not.**
Verified rather than recalled:

| Format | Vintage signal | Verdict |
|---|---|---|
| **Shapefile** | The `.shp` header's version field reads **1000** — that is the 1998 *format* version and is constant for every shapefile ever written. The `.dbf` header carries a last-update date (ours read `2026-08-02`, i.e. the moment it was written). An optional `.shp.xml` sidecar may carry real FGDC/ISO publication metadata *if the publisher shipped one*. | **No data versioning.** The `.dbf` date is a write timestamp, not a vintage, and says nothing about whether the boundaries are current. |
| **GeoJSON** | No standard field. RFC 7946 discourages foreign members, so publishers rarely add one. | **Nothing reliable.** |
| **GeoPackage** | `gpkg_contents.last_change` is **standard and populated** — our own `data/layers.gpkg` reports `2026-07-09T02:58:06.717Z` for `police_districts`. Optional `gpkg_metadata` tables can hold full ISO 19115 lineage (absent in ours). | **Yes, a real timestamp.** |
| **ArcGIS REST** | `?f=json` on the layer can return `editingInfo.lastEditDate`, the strongest signal available. **But it is optional**: probed live, Cook County's `politicalBoundary/MapServer/2` returns `serviceItemId` and `currentVersion` but **no `editingInfo`**. | **Sometimes the best, often absent.** Harvest when present; do not depend on it. |

**Consequence for F8.** Fetching a URL is the *only* input where the tool might
learn the data's age, and even there the specific Cook County layer in the
maintainer's example does not publish it. This is why R17 stands: the preview
overlay is not a convenience, it is the primary control for currency.

A second finding from the same probe, worth its own warning code: writing to
shapefile **silently truncated `ward_precinct` to `ward_preci`** — the format
caps field names at 10 characters. An operator who brings a shapefile will get
attribute names that differ from what they see in QGIS, and a `[[layers]]`
`attributes` entry written from the untruncated name would fail at startup.

### F5b decisions (D10–D14)

| ID | Decision | Recommendation |
|---|---|---|
| **D10** | In-process plugin, or out-of-process shim? | **Decide at Gate 8a, after the F5b-T2 spike** — see §8 R1/R2. In-process is simpler and is what the archived plan assumed; it is only possible if the target Esri Python is ≥3.11. |
| **D11** | Entry-point group name | `point_in_polygon.geocoders` — already proposed in the archived plan; keep it. |
| **D12** | Where the plugin lives | In-repo under `plugins/arcpy-locator/`, published as its own distribution. Keeps it reviewable under the same AGPL repo while remaining separately installed (§5.4). |
| **D13** | Concurrency | Serialize `geocode()` behind a module-level lock and document **single-worker uvicorn only**. `arcpy` is not thread-safe and each process holds a license seat. |
| **D14** | Scoring | Map Esri `Score` (already 0–100) straight through, consistent with D6. Honor a configurable `min_score`; below it is a *no-match*, not an error. |

**The D10 alternative worth taking seriously.** If the shim route is chosen, the
shim can emulate a `GeocodeServer`'s `findAddressCandidates` response shape —
in which case the **existing `arcgis_rest` adapter already talks to it** and F5b
needs *no new core code and no new plugin package at all*. It becomes a
documented deployment recipe plus a ~100-line script the agency runs under its
own Esri Python. That is strictly less code in the AGPL core, strictly better
isolation, and it sidesteps R1, R3, R4, and R5 simultaneously. The cost is an
extra local process. **I recommend the spike explicitly evaluate this against
the in-process design rather than treating in-process as the default.**

## 3. F7 → tasks

**Δ3 ratified 2026-08-01. F7-T1…T4 and T6 are BUILT** on branch `F7/batch-lookups`
(commit `ec573f0`, pushed). 325 tests pass, of which the 123 v1 tests were
re-verified in isolation. F7-T5 (this runbook set) is written at
`docs/runbooks/batch-lookups.md` — local only, since `docs/` is untracked.

**Adversarial review found 9 defects; all 9 are fixed and independently
re-verified** (see §9 below). Awaiting maintainer PR review.

**Output is CSV only — decided 2026-08-02** (commit `947c45e`). The `.xlsx`
write path staged rows, addresses in cleartext, in `$TMPDIR/openpyxl.*`;
openpyxl cleans up on a normal exit but a kill or power loss leaves the copy.
SPEC §9 makes no-persistence a non-negotiable, so the write path was **deleted**
rather than flagged off — dead code that stages PII is worse than no code, and
F7b must not be able to re-enable it. Reading `.xlsx` is untouched.

The maintainer's framing was "safe for folks who know less about IT security
than me," which drove two further changes: the output file is created **0600**
via `os.open` (not a chmod afterwards, which would leave a readable window),
since it holds addresses by construction and a default umask would otherwise
expose it on a shared machine; and a finished run states in plain words what the
file contains and that it should be kept private and deleted when done. The
`.xlsx` refusal fires before the source is read and before any geocoding, and
its wording carries no jargon — verified by a test that greps for it.

### F7-T1 — `app/batch/sources.py`: read CSV, XLSX, Google Sheets
One entry point — `read_source(spec) -> (headers, Iterator[dict])` — with three
backends:

- **CSV** — stdlib `csv`. Handle the `utf-8-sig` BOM Excel writes, and read
  **every cell as a string**: coercing types silently destroys leading zeros in
  ZIP codes and house numbers (see R14).
- **XLSX** — `openpyxl` in `read_only=True` streaming mode so a large workbook
  doesn't land in memory whole. Import guarded, with the `[batch]` install hint.
- **Google Sheet** — accept a normal browser URL, extract the document id and
  `gid`, build the CSV export URL, fetch with the existing `httpx`. If Google
  answers with an HTML login page instead of CSV, that means the sheet isn't
  link-shared: raise a clear error saying exactly that and how to fix it.

Tests: fixture CSV (with BOM, with leading-zero ZIPs) and XLSX; `respx`-mocked
Sheets fetch covering the happy path, the not-shared login redirect, and a
malformed URL. All offline, consistent with the existing suite.

### F7-T2 — `app/batch/runner.py`: the engine
Takes rows, a `PolygonLookup`, an optional `Geocoder`, and a column mapping;
yields result rows. Per row: lat/lon columns present → skip geocoding entirely
and go straight to `locate()`; address column → geocode, then locate. Adds
`status`, `reason`, `matched_address`, `score`, and the layer's attributes.

Carries the D20 rate limiter and emits progress to **stderr** (so stdout stays
pipeable). Per-row exceptions are caught and become `status=error` rows (D19).

Tests with a stub geocoder and no files: match, no-match, outside-all-polygons,
unparseable coordinates, geocoder raising `GeocoderUnavailable`, and a rate
limiter test proving the configured delay is actually respected.

### F7-T3 — `app/batch/writer.py`: results out
Writes CSV or XLSX preserving **every original column in its original order**,
appending the result columns. Implements D21 formula-injection escaping, with a
test that feeds `=cmd|'/c calc'!A1` through as a matched address and asserts the
written cell is neutralized.

### F7-T4 — `scripts/batch_locate.py`: the CLI
`argparse` over the above. Flags: `--address-column` | `--lat-column`/
`--lon-column`, `--layer`, `--provider`, `--rate-limit`, `--max-rows`, `--out`,
`--config`. Refuses Nominatim (D20). Non-zero exit if any row failed (D19).
Docstring carries the ArcGIS/ArcPy equivalent per golden rule 3 — this is
essentially `arcpy.geocoding.geocodeAddresses` followed by a Spatial Join,
which is exactly the workflow the original prototype's users know.

### F7-T5 — documentation
`docs/runbooks/batch-lookups.md`: the end-to-end workflow, the **exact Google
Sheet sharing step** (the thing that will generate every support question), the
runtime math from R8, why a few thousand rows wants the offline geocoder, and
the plain statement that the CLI runs on your machine and sends nothing but
individual addresses to whichever geocoder you configured.

### F7-T6 — verification
Real end-to-end run against a fixture CSV and a genuinely published test sheet,
with the transcript and timings in the PR. Plus `pytest -q` green.

**Verify (whole feature):** full suite green; a real batch run transcript; and a
demonstration that a lat/lon-only file completes with the network severed
(reusing the `test_offline.py` socket-blocking fixture) — proving the
no-geocoder path is genuinely offline.

## 3a. F8 → tasks

**Δ5 ratified 2026-08-02. F8-T1 is BUILT** on branch `F8/layer-tool` (commit
`757a5bd`, based on `main` so it reviews independently of the F7 PR). 218 tests
pass; the 123 v1 tests were re-verified in isolation.

**Review found 14 defects across two lenses; all fixed and re-verified.** Two
mattered:

- **PIP-L004 was blind to the mirror mistake.** It caught "a degrees label over
  huge numbers" but not "real latitude and longitude under a label claiming a
  local grid" — which is the more likely error, because it is what happens when
  a mapping program asks "which projection is this?" and the operator picks the
  one the other layers use. Reproduced on the real
  `shapefiles/ward25_precincts.geojson`: `set_crs(3435)` produced **no blocking
  finding**, would have installed clean, and then reprojected Ward 25 to
  **lon -91.69, lat 36.62 — southern Missouri**, where every lookup returns a
  confident miss forever with nothing reporting failure. The check is now
  symmetric and blocking, and pinned against the shipped EPSG:3435 layers
  (coordinates to 1.9M ft) so it cannot block the service's own data.
- **Dragging in a CSV crashed with an `AttributeError`** instead of the friendly
  "this file has no shapes in it" — whose code path was unreachable dead code.
  The most likely wrong-file case in the feature.

Also fixed: PIP-L008 reported shape positions counted in a *filtered* subset, so
it named the wrong shape and would have made the preview highlight a correct one;
the bounding-box check silently collapsed across the antimeridian and now refuses
to make a claim rather than making a wrong one; PIP-L003 sent GeoPackage and
GeoJSON users hunting for a `.prj` file their format never had; and the registry
mixed "commit" and "install" for the same act, with "commit" undefined for a
reader to whom it means "promise". Verified: none of *commit, CRS, EPSG, WGS84,
geometry, parse, null* appears undefined in any of the 18 messages.

**Three UI promises the text now makes, which later tasks must honor** (pinned by
a `UI_PROMISES` test so a task cannot drop the feature and leave the words):
F8-T3 must highlight `detail["broken_positions"]` on the preview; F8-T3 must
render `detail["available_columns"]` beside it; F8-T5/T6 must actually repair
self-crossing outlines during install — PIP-L008 is a warning rather than a block
*because* it is repairable, so dropping the repair would make the severity wrong.
The page must also render findings in the order what → specifics → why → fix,
because two entries point across that boundary.

### F8-T1 — the error-code registry (`codes.py`) and validator (`validate.py`)
The heart of the feature, and pure: no I/O, no FastAPI. A table of checks, each
owning a stable code, a severity (D27), and three pieces of plain-language text —
**what happened, why it matters, how to fix it**. Codes are stable identifiers an
operator can quote in a support request or search the runbook for.

The set to cover, each with its own code:

| Situation | Severity |
|---|---|
| Unrecognized / unreadable format | blocking |
| Loose `.shp` dropped without its `.dbf` / `.shx` — names exactly which are missing | blocking |
| **No CRS defined** (a `.shp` with no `.prj`) — the maintainer's example | blocking |
| CRS defined but coordinates implausible for it (feet-sized numbers in a degrees CRS) | blocking |
| Zero features | blocking |
| Geometry is not polygonal (points/lines) | blocking |
| Mixed geometry types in one layer | blocking |
| Invalid geometry (self-intersection) — repair offered | warning |
| Proposed layer id collides with a configured layer | blocking |
| Chosen attribute column missing, or empty in every feature | blocking |
| Duplicate attribute names | blocking |
| Archive too large, or a decompression ratio that smells like a bomb | blocking |
| Archive member escaping the extraction directory (zip-slip) | blocking |
| Fetch failed, or a URL returned HTML instead of spatial data | blocking |
| Feature count or file size beyond what the service should load | warning |
| Bounding box nowhere near any configured layer | warning |

Text is written for a non-technical reader, per the standard set in F7: no
jargon a campaign volunteer would have to look up. Tested against synthetic
fixtures — one per code, asserting both the code and that its fix text is
actionable.

### F8-T2 — `inspect.py`: get a candidate in
Four input paths, all four requested: **zipped shapefile**, **loose shapefile
parts**, **GeoJSON**, and an **ArcGIS REST / export URL**. Each ends at the same
place — a GeoDataFrame plus a facts record (feature count, CRS name and code,
bounding box, attribute names with sample values, and where it came from).

Archive handling reuses F7's guard: read the zip *directory* first, enforce total
and per-member uncompressed caps and a ratio ceiling, refuse any member whose
path escapes the extraction root, and only ever extract a whitelist of shapefile
extensions. Nothing from an archive is executed or trusted.

The URL path reuses the pipeline's existing polite `User-Agent` and, like F7's
Sheets reader, tells "that URL returned a login page / HTML" apart from "the
network failed."

### F8-T2 — **BUILT 2026-08-02** (`ee957e3`)
`app/admin/inspect.py` reads all four requested inputs plus GeoPackage (nearly
free, and the only format carrying a real vintage). 304 tests pass; the 123 v1
tests re-verified in isolation.

**Review found 11 defects; all fixed and re-verified.** Four installed silently
wrong data — which matters more than a crash here, because the layer installs,
every downstream check passes, and the operator's only defence is looking at a
shape that appears correct:

- **A zip could smuggle a second layer past the preview.** Sets were identified
  by *basename* while extraction also used basename, so folders collapsed.
  Reproduced: `a_real/wards.{shp,shx,prj}` plus `z_forged/wards.dbf` merged into
  **real Chicago geometry carrying the attacker's attribute table** — a
  correct-looking map with the wrong labels, which defeats the one control this
  feature rests on. Sets are now identified by folder *and* stem, never
  assembled across folders, and two candidates are an explicit choice.
- **ArcGIS still truncated silently.** Paging ended on a short page even when the
  page size was the reader's own *guess*, so a service capping below it and
  setting no `exceededTransferLimit` installed 500 of 2,500 polygons with no
  finding. A transient metadata failure also flipped it into that regime. Now
  verified fetching 2,500 of 2,500, distinct and in order.
- **The reader fabricated a CRS.** The payload's `crs` member was dropped on
  reassembly and a requested `outSR` was never recorded, so `outSR=4269` (NAD83)
  installed labelled WGS84 with nothing firing — a metre-scale shift invisible
  on a preview and wrong forever at boundaries. Verified EPSG:3435 now survives.
- **Every browser upload was refused.** Dispatch read the on-disk suffix, but an
  upload arrives as `tmp0abcdef` with no extension. This blocked every path
  F8-T3/T5 will use.

Also fixed: unbounded response bodies (a 314 MB reply cost ~900 MB RSS before
being correctly refused), a feature cap unreachable on single-page answers,
corrupt archive members escaping as raw `zlib.error`, temp directories holding
the operator's data leaking on failure, and a pasted `token=` reaching
`facts["source_url"]`, which is documented as going to the browser.

Held up under review: zip-slip against all eight crafted path forms, the
extension whitelist, no off-host redirects, CRS passthrough on every local path,
and leading zeros surviving as strings.

### F8-T3 — `preview.py`: something the operator can actually judge
Reproject to WGS84, simplify for display only (D28), and emit GeoJSON plus a
bounding box. Also emit the **already-configured layers** so the UI can draw the
candidate on top of them.

That overlay is the answer to the stale-data problem. A superseded CPD district
file is perfectly valid data — no check can know it is out of date — but drawn
over the districts already in the service, a boundary that no longer lines up is
obvious at a glance. Automated validation cannot catch "wrong"; a human looking
at two outlines can.

### F8-T3 — **BUILT 2026-08-04** (`35a4da9`)
`app/admin/preview.py`. 357 tests pass; the 123 v1 tests re-verified in isolation.

**The fidelity requirement is met and measured, not asserted.** Verified against
an independent Hausdorff computation, and re-measured by hand:

| shift applied | drawn outlines differ by | vs the 0.538 m simplification error |
|---|---|---|
| 250 m | 250.02 m | 464× |
| 100 m | 100.08 m | 186× |
| 25 m | 25.05 m | 46× |
| 10 m | 10.02 m | 18.6× |
| 1 m | 1.00 m | 1.9× |

A finding worth keeping: **the obvious "one screen pixel" tolerance rule is
unsafe.** Cook County at 900 px is ~67 m/pixel, which would smooth a whole-block
boundary move into apparent agreement. A hard 0.5 m ceiling governs at county
scale; the pixel rule only binds when zoomed under ~450 m across.

**Review found 7 defects; all fixed and re-verified.** Two corrupted numbers the
operator is shown:

- **The measurement failed open.** Non-finite distances were filtered away while
  the worst-case stayed `0.0`, so a `GeometryCollection` claimed **0.0 m error on
  geometry that plain polygons reported as 428 m** — and the page would have
  rendered "boundaries may sit up to 0.0 m from where they really are." It now
  fails closed: unmeasurable is reported as unknown *with a reason*, never zero.
- **Distances were projected, not geodesic.** One AEQD centred on the viewport
  put two layers whose true gap is 22.3 km at 2,489 km, and the payload then
  accused a correct file of being the wrong one — a false positive in the exact
  sentence the operator acts on. Now solved on the ellipsoid: 22,260.5 m against
  a 22,263.9 m truth. An extent too large to measure honestly now says so.

Also fixed: a `NaN` coordinate crashed with a raw `GEOSException` while being
accepted by both the reader and the validator; the measurement ran twice and was
O(n²), so 100k vertices took 40 s under no bound (now 5.8 s); the escalation loop
doubled its tolerance to 524 km chasing a feature-count problem a distance lever
cannot solve; and sub-grid features rendered as invalid zero-area polygons while
the payload claimed they were drawn.

Held up: the candidate frame is byte-identical after a preview (F8-T6 commits the
original geometry, so display simplification must never leak into it); the
viewport never hides separation at 5/50/500 km; holes survive; and EPSG:3435 in
feet is reprojected before measuring, so metres are honest.

### F8-T4 — `app/admin/main.py`: the local app
A separate FastAPI app (D22) with three endpoints: inspect a candidate, fetch its
preview, commit it. Loopback bind, token, and `Host` check per D23. Reuses
`app/errors.py`'s envelope so an error code reaches the browser in the shape the
rest of the service already speaks.

### F8-T4 — **BUILT 2026-08-04** (`6ba4422`)
`app/admin/main.py`, two read-only endpoints; commit returns a clean 501 until
F8-T6. **419 tests pass**; the 123 v1 tests re-verified in isolation.

**H10 is real and can fail.** The route walk now inspects what a mounted app
*routes to*, not the type of the app object — the old check compared
`type(route.app).__module__`, which is always `fastapi.applications`, so it was a
hook that could never fire. A test now mounts the installer on a real public app
and asserts the walk catches it. Sabotage matrix from review (all four caught,
but note C was caught by a single text scan alone):

| sabotage | route walk | import probe | text scan |
|---|---|---|---|
| admin app mounted | now FAILS (was a miss) | fail | fail |
| bare top-level import | n/a | fail | fail |
| env-gated import in `create_app` | n/a | n/a | **fail (only)** |
| obfuscated `importlib` import | now FAILS (was a miss) | fail | fail |

**Review found 5 defects; all fixed and re-verified.**

- **The only pre-auth path that was not a clean refusal.** One non-ASCII byte in
  the token header made `secrets.compare_digest` raise `TypeError` *outside* the
  guard's try, bypassing the error envelope with a bare 500 and a logged
  traceback — reachable by any local process or other account on a shared
  machine, exactly the adversary D23 names. The comparison now runs on bytes;
  verified **all 256 byte values** give a clean 403. The audit also found
  `"²".isdigit()` is `True` while `int("²")` raises, in the size check.
- **Replacing an installed layer was impossible** — the most likely reason to
  reopen this tool. The exclusion of the layer being replaced was applied to the
  preview but not the validator, so `PIP-L009` always fired and the Install
  button could never be pressed. Now computed once for both; a replacement warns
  (`PIP-L020`) rather than blocking, since areas are about to be dropped but
  nothing is destroyed until T6.
- **One inspect froze the whole tool** — `async def` doing synchronous geopandas
  and network I/O on the event loop: 11.04 s against a 0.05 s baseline. Verified
  fixed against a real uvicorn: 0.00 s during a slow inspect.
- **A valid two-layer GeoPackage was reported as corrupt.** It fired `PIP-L001`
  ("never got as far as opening… check that it finished downloading"). New
  **PIP-L019** asks which layer you want; shared with the two-shapefile zip case.

**Token placement, recorded because it is easy to get wrong later:** the token
rides in `X-Admin-Token`, never the URL. A query-string token lands in browser
history and is handed to every external resource via `Referer`. A header also
cannot be set cross-origin without a preflight this app never answers, so the
browser stops a rebinding tab before the `Host` check runs.

`shapefiles/ward25_precincts.geojson` is now **tracked**: nine assertions across
four test modules rest on it, behind `skipif` guards that would otherwise pass
while testing nothing — the silent-skip footgun `docs/runbooks/testing.md` calls
out as edge case A.

### F8-T5 — `static/admin/`: the page
Vanilla HTML/JS/CSS, zero dependencies, no external fonts or tiles, works fully
offline. Drop zone plus a file picker plus a URL field. Renders the preview as
inline **SVG** — no mapping library — with the configured layers muted underneath
and the candidate outlined on top. A facts panel. Errors shown with their code
prominent and the fix text in plain words. One confirm button, disabled until the
candidate passes.

Accessible to the same standard F6 was held to (labels, contrast, keyboard
operation) — a drag-and-drop-only interface would exclude keyboard users, so the
file picker is a peer of the drop zone, not a fallback.

### F8-T6 — `commit.py`: write it safely
Normalize to `data/<layer_id>.gpkg`, back up `config.toml`, append the rendered
`[[layers]]` block (D25). Respect `PIP_CONFIG` — the operator's live config may
not be `./config.toml`. Refuse to overwrite an existing data file or layer id.

**Before writing, run D26's dry-run**: build the `LayerConfig`, load it with
`PolygonLookup`, query a point. If the service could not start with this layer,
say so and write nothing.

After a successful commit, state plainly that the running service must be
restarted to pick the layer up — layers load once at startup (`app/lookup.py`),
so silence here would leave the operator wondering why their layer is missing.

### F8-T7 — the isolation guarantee
`scripts/add_layer.py`: mint the token, bind 127.0.0.1, open the browser, clean
up the temp directory on exit. Refuse any attempt to bind a non-loopback address.

Plus the H10 test that makes D22 real: assert `app.main`'s routes contain nothing
from `app/admin`, and that importing `app.main` does not import `app.admin`.

### F8-T8 — documentation
`docs/runbooks/adding-a-layer.md` gains the tool as its **first** path — the
existing `build_data.py` route becomes the advanced option for a reproducible
pipeline. Plus a complete error-code table: code, what it means, how to fix it.
That table is the thing the maintainer's example depends on — an operator hitting
a CRS error should find their code and a fix without asking anyone.

**Verify (whole feature):** full suite green; H10 isolation test; a real
end-to-end run installing a genuine layer from each of the four input types, with
screenshots of the preview and of at least one error dialog in the PR.

## 3b. F5b → tasks

### F5b-T1 — core: lazy entry-point discovery
`registry.py` grows resolution of an unknown `type` through the
`point_in_polygon.geocoders` group, loaded on demand. Built-in names are not
shadowable. Unknown-and-undiscoverable types keep today's error, extended to
list discoverable plugin types so the diagnostic stays useful.

Tests use a **dummy** plugin (a fake distribution registered in the test, no
`arcpy` anywhere): discovery works, laziness holds (assert the module is *not*
imported until configured), shadowing is refused, a plugin whose import raises
surfaces as `ConfigError` at startup naming the distribution.

*No `arcpy` involved. This task is fully CI-verifiable and can ship alone.*

### F5b-T2 — spike on real Esri hardware **(gate; timeboxed)**
The maintainer runs this; the agent cannot. Answers, recorded in a findings note:

1. The exact Python version in the target ArcGIS Pro conda env. **If < 3.11 the
   in-process design is dead** and D10 goes to the shim.
2. The single-address geocode call and its real latency (`arcpy.geocoding.*`
   against a `.loc`). Batch-oriented APIs that require an input table are a
   problem — see R3.
3. Whether any part of that call writes the queried address to disk (scratch
   geodatabase, project workspace, temp table).
4. Whether the workstation can install the core package at all (network,
   permissions, conda env cloning).

**No implementation starts before this returns.** Everything downstream is
conditional on it.

### F5b-T3 — plugin package skeleton
`plugins/arcpy-locator/` with its own `pyproject.toml`, the entry-point
declaration, a README covering installation into ArcGIS Pro's Python (clone the
conda env; `propy`), and a LICENSE carrying the AGPLv3 notice plus a plain
statement that `arcpy` is proprietary, is not bundled, and is the installing
agency's own licensed software.

### F5b-T4 — `ArcpyLocatorGeocoder`
Implements the `Geocoder` protocol. Config: `id`, `type = "arcpy_locator"`,
`locator` (path to the `.loc`), optional `min_score`. Behavior:

- Import `arcpy` lazily inside the builder, and raise `ConfigError` with a
  human message if absent ("`arcpy` not importable — this plugin runs only
  inside a licensed ArcGIS Python environment").
- Match → `GeocodeResult` with `point` reprojected to **WGS84 lon/lat** (the
  adapter's job; the locator's native output may be anything).
- No candidate, or best candidate below `min_score` → `GeocodeResult.no_match`.
- Locator error / license failure → `GeocoderUnavailable`, so it fallthroughs in
  a chain exactly like every other provider (D7).
- **Memory workspace only.** Nothing touches disk. See R3.
- Serialized behind a lock (D13).

### F5b-T5 — tests against a fake `arcpy`
A `sys.modules["arcpy"]` stand-in, injected per test, proving the translation
layer: match, no-match, below-`min_score`, missing locator, arcpy raising →
`GeocoderUnavailable`, and the §9 test that the queried address appears in no
log record and no exception message. Plus one test asserting the adapter fails
cleanly when `arcpy` is absent — the state every CI runner and every FOSS
install is in.

### F5b-T6 — documentation
A new `docs/runbooks/arcpy-plugin.md`: when you would ever want this (you have
nothing but a locked Esri box), why it is the *last* resort behind §5.3, how to
install it into Esri's Python, the commented `config.toml` block, and the
single-worker constraint. Plus the commented opt-in stanza in `config.toml`
itself, matching the existing `nominatim` / `local_points` pattern.

**Verify (whole feature):** `pytest -q` green with no `arcpy` present (the
normal case), plus a transcript from a real ArcGIS Pro workstation showing a
known address geocoded through the plugin and located to the correct district —
pasted into the PR per Δ2.

## 4. Gates & sequence

| Gate | Crossing | Opens |
|---|---|---|
| — | C5 (branch reconcile) | **DONE 2026-08-01** |
| Gate 8 | **Maintainer ratifies Δ3** (batch moves in-scope) | F7-T1…T6 |
| Gate 8b | C6 run while the tree is quiet | F7 build starts on rewritten history |
| Gate 9 | F7 shipped, adversarially reviewed, PR logged | F7b planning (with Δ4) |
| Gate 9a | **Maintainer ratifies Δ5** (local layer tool in scope) | F8-T1…T8 |
| Gate 10 | Maintainer ratifies Δ2 | F5b-T1 |
| Gate 10a | **F5b-T2 spike returns; D10 decided** | F5b-T3…T6 (or the shim redesign) |

**Recommended order: C6 ✓ → F7 ✓ → F8 → F7b → F5b.**

**F8 goes next, ahead of F7b.** F7b puts the batch engine behind a network
endpoint; F8 removes the need to edit Python to add a layer. The second is worth
more to the people this tool exists for — a campaign that cannot install a layer
has nothing to run batches against — and F8's validation work (the error-code
registry, the archive guards) is reusable by any later upload surface, including
F7b's. Building F8 first means F7b inherits a vetted intake path instead of
inventing a second one.

C6 (the history purge) should go **now, before F7 starts**, not last. It takes
minutes, the tree is currently quiet and single-branch, and every SHA in the
repo changes when it runs — doing it after F7 opens a branch means rewriting
history under an in-flight feature, which is the painful version. This reverses
the sequencing in the previous draft, and the reason is simply that C5 is done
and nothing is in flight right now. That window closes the moment F7-T1 starts.

F5b goes last: it is blocked on hardware nobody has, cannot be proven in CI, and
serves a hypothetical agency, while F7 serves the actual use case the original
`arcpy` prototype existed for.

## 5. Hooks

Continues H1–H5 from the archived plan.

| ID | Invariant | Mechanism | Surface |
|---|---|---|---|
| H6 | No proprietary dep in the core or default install (§9) | CI job that imports the whole `app` package and asserts `"arcpy" not in sys.modules`, and that `pip install .` pulls no Esri anything | CI |
| H7 | Plugin laziness | Test asserting a configured-but-unused plugin type is never imported | test suite |
| H8 | Batch never persists (§9, D15) | Test asserting a batch run creates no file other than the one `--out` names — no temp files, no scratch | test suite (F7-T2) |
| H9 | Output is injection-safe (D21) | The `=cmd|…` round-trip test in F7-T3 | test suite |
| H10 | **The admin tool is never publicly served (D22)** | Test asserting `app.main`'s route table contains no `app/admin` path, and that importing `app.main` does not import `app.admin` | test suite (F8-T7), CI-enforced via H2 |

H10 is the one that matters for F8. "The upload endpoint isn't exposed" would
otherwise be a claim resting on nobody ever wiring the two apps together; this
makes it a property the suite checks on every commit.

H6 is the one that matters for F5b: it makes "never core, never default, never
required" a thing CI enforces rather than a thing we promise. H8 plays the same
role for F7 — "in memory only" stops being a claim and becomes a test.

## 6. Chores (human track)

### C5 — reconcile `main` and retire the stale branch — **DONE 2026-08-01**
`main` and `docs/testing-runbook` diverged: both carry content-identical commits
under different SHAs (merge base is `f8ffa5a`, the v1.0.0 release), so this is
**not** a fast-forward and a merge would create a merge commit — against the
rebase-only convention. Cherry-pick instead:

```bash
git checkout main && git pull
git cherry-pick 68a84ab 2a3f3f9    # the README fix + the docstring fix
git push origin main
git push origin --delete docs/testing-runbook
git branch -D docs/testing-runbook
```

### C6 — purge the local-only paths from history — **DONE 2026-08-01**

**Executed and verified.** `git filter-repo --path docs --path CLAUDE.md --path
bootstrap.py --path .claude --invert-paths --force`, then a force-push of `main`
and the rewritten `v1.0.0` tag.

| Check | Result |
|---|---|
| Backup taken before anything | `../pip-service-backup.bundle` (2.6 MB, all refs) |
| Commits touching the four paths, after | **0** |
| Path scan across every commit tree | **0 matches** in 36 commits |
| Blobs containing the ratified-SPEC marker | **0** |
| HEAD tree hash | `22d966…` — **unchanged**, so working content is byte-identical |
| Commits | 39 → 36 (three docs-only commits became empty and were pruned) |
| `v1.0.0` tag | rewritten `c950f73` → `54ef6eb`, force-pushed; GitHub release intact |
| Test suite | 123 passed |
| Fresh clone from GitHub | none of the four paths present at HEAD or in history |

Dropped commits (docs-only, so they emptied out): `adding pr log`,
`docs: add H-label guardrail to plan hooks section`,
`docs(F6-T4): air-gapped setup must make the offline geocoder the default`.

`main` had no branch protection, so nothing needed lifting. `filter-repo`
removed the `origin` remote as it always does; it was re-added and upstream
tracking restored.

**Original procedure, for the record:**

**Purge all four paths, not just the docs:**

```
docs/   CLAUDE.md   bootstrap.py   .claude/
```

`bootstrap.py` is the one that matters. It embedded `CLAUDE.md` (2,578 chars),
`docs/specs/SPEC.md` (10,892 chars — a *superset* of the live 10,680-char copy,
since it holds the pre-edit original), and `docs/conventions.md` (1,476 chars)
verbatim as string literals. Purging `docs/` while leaving it would have moved
the same material from one tracked file to another and achieved nothing. All
four are now untracked at HEAD (`bbfbecb`); C6 removes them from history too.

Facts established 2026-08-01: 41 commits total, 16 touch the doc paths, and the
**first commit already contains them** — so every SHA in the repository changes,
including the `v1.0.0` tag. `git-filter-repo` is installed locally.

1. **Back up first**: `git bundle create ../pip-service-backup.bundle --all`
   and keep it off the machine's normal backup rotation. This is the only way
   back.
2. Complete C5 first — one branch, nothing in flight.
3. `git filter-repo --path docs --path CLAUDE.md --path bootstrap.py --path .claude --invert-paths`
4. Verify: `git log --all --oneline -- docs CLAUDE.md bootstrap.py .claude`
   returns **nothing**, the tree at the new tip is byte-identical to the old
   (only history differs), and `pytest -q` is still green.
5. Re-create the `v1.0.0` tag on the rewritten commit and re-point the release.
6. Force-push all refs and tags. Branch protection must be lifted for the push
   and restored after.
7. Re-add the local `docs/` and `CLAUDE.md` afterwards — `filter-repo` also
   strips the remote, so re-add `origin` by hand.

### C7 — the GitHub PR diffs: leave them
All 8 merged PRs contain the doc files in their diffs, and **a history rewrite
does not touch PR diffs** — GitHub serves them from its own storage. Removing
them would mean deleting and re-creating the repository, which also destroys
the 8 PR conversations that `docs/runbooks/testing.md` names as the project's
adversarial-review record.

**Decision (2026-08-01): leave them.** Against a friction goal (R6), deleting
the repo trades a real audit trail for a marginal gain — someone would have to
know the PRs exist and page through eight merged diffs to reconstruct what a
`git clone` used to hand them for free. That is exactly the effort bar C6 is
meant to raise. Optionally export the conversations for the local record:

```bash
for n in $(seq 1 8); do gh pr view $n --comments; done > docs/pr-review-export.md
```

## 7. Success-criteria map

| Criterion | Proven by |
|---|---|
| Batch reads CSV, XLSX, and a published Sheet | F7-T1 tests (offline fixtures + mocked fetch) |
| Batch persists nothing but the requested output | H8 test |
| Output is safe to open in Excel | H9 injection test |
| lat/lon batch runs with no network at all | F7-T6 socket-blocked run |
| A public geocoder is never hammered | D20 rate-limiter test (F7-T2) |
| Plugin never imported unless configured | H7 test + F5b-T1 |
| Core installs and runs with no Esri anything | H6 CI job |
| Adapter translates match/no-match/error correctly | F5b-T5 (fake `arcpy`) |
| Real locator geocodes a known address end-to-end | F5b-T2 spike + PR transcript (Δ2) |
| No queried address persisted or logged | F5b-T5 PII test + R3 disk check in the spike |

## 8. F7 adversarial review — findings and resolutions (2026-08-01)

Three reviewers with distinct lenses (spec/PII, correctness, security) found 8
defects; verification of the fixes turned up a 9th. All were reproduced before
being fixed, and re-reproduced afterwards. None were style opinions.

| # | Lens | Defect | Resolution |
|---|---|---|---|
| W1 | security | **Header row never escaped** — a total bypass of the D21 formula-injection defense. An input column *named* `=cmd\|'/c calc'!A0` produced an XLSX whose A1 had `data_type: f`, a live formula firing on open. | Escaping moved from the row-rendering layer to the **write boundary**, so no cell can bypass it. `data_type` now `s`. |
| S5 | security | **XLSX decompression bomb** — a 320 KB workbook cost 445 MB RSS inside `load_workbook`, before any row limit could intervene. 10 MB ≈ 15 GB. | Zip *directory* inspected before opening: total 256 MB, per-member 128 MB, 200:1 ratio caps. 616 KB bomb now refused in 0.000 s, 0 bytes RSS. |
| S1 | correctness | **Interior blank XLSX rows silently dropped** — 4 data rows returned 3, exit 0. CSV returned 4. Output row *N* stopped matching input row *N*, shifting every district onto the wrong case. | Blank rows buffered and flushed when a non-empty row follows, so genuine trailing padding still drops. CSV/XLSX parity now asserted cell-for-cell. |
| S2 | correctness | **`csv.Error` escaped as a traceback**, exit 1 — indistinguishable from a normal partial run. A 4,000-row file wrote 2 rows and looked fine. | Caught and raised as `BatchError` naming the line (never the content). Exit 2, no traceback. |
| **NEW** | correctness | **Short unclosed quote silently loses rows** *below* `field_size_limit` — the common case. 3 rows in, 2 out, nothing reported. Found while verifying S2's fix. | Unfixable by detection alone (a runaway quote and a legitimate multi-line cell are identical to a parser), so it is **surfaced**: a per-record stderr warning plus a `read N` / `wrote M` count the operator can compare. Exit code unchanged — a multi-line cell is not an error. |
| W2 | correctness | **Duplicate `pip_*` columns** when re-running over prior output — the writer produced a file its own reader refuses. | Collision refused before any file is opened, in the reader's own vocabulary. No silent rename. |
| S4 | correctness | **"Publish to web" Sheets URLs** parsed the doc id as the literal `"e"`, 404ing with a message blaming a correct URL. | Both URL forms supported, each through its own export endpoint. |
| S3 | credential | **Full URL echoed in errors** — a presigned link's `token=` leaked to stderr and CI logs. | Query, params, fragment and userinfo stripped from every message; audited all call sites. |
| C1 | PII | **`.xlsx` staging warning gated on geocoding**, so a lat/lon run staged addresses from unmapped passthrough columns with no warning. Measured: a real address in `$TMPDIR/openpyxl.*`. | Warning keyed on output format, not mapping. |
| C2 | contract | `MemoryError`/unexpected exceptions escaped all handlers → traceback, exit 1. | Top-level boundary honors its documented contract: clean message, exit 2, no address echoed. |

**Verified clean under review** (worth recording, so it isn't re-checked): no
logger anywhere in `app/`; every geocoder exception path opaque, so an httpx
error embedding the queried URL cannot reach output; `GeocodeResult.query` never
written; the lat/lon path proven network-free with sockets monkeypatched to
raise; `locate(lon, lat, …)` argument order correct at every call site.

## 9. Risks & open questions

### F8 risks

**R16 — "wrong CRS" is two different problems, and only one is detectable.**
A file with **no** CRS (a `.shp` missing its `.prj`) is caught with certainty —
that is the maintainer's example and it gets a blocking code. A file that
*declares* a CRS that is simply **wrong** is a different matter: the data is
internally consistent and nothing is malformed. The only automatic signal is
implausibility — coordinates in the hundreds of thousands under a degrees CRS,
or a bounding box nowhere near any configured layer — which is a heuristic and
gets a warning, not a block. **The preview overlay is the real detector**, and
the docs must not oversell the validator as catching more than it does.

**R17 — stale data cannot be detected at all.** A superseded CPD district file
is valid in every mechanical sense. No check will ever flag it. This is the
entire justification for D28's overlay and for making the preview a required
step rather than a nicety. Be honest about it in the runbook: the tool proves a
file is *loadable*, not that it is *current*.

**R18 — a local web UI is not automatically a safe one.** Loopback binding stops
the internet, but not other accounts on a shared machine, and it does not stop
**DNS rebinding** — a page the operator visits in another tab can resolve a
hostname to 127.0.0.1 and POST to the tool. D23's token and `Host` check are the
mitigations, and they are not optional extras. This is exactly the class of
mistake the maintainer's "safe for folks who know less about IT security"
framing is about: the operator cannot be expected to know this risk exists.

**R19 — the browser will choke on real boundary data.** A detailed municipal
boundary can carry six-figure vertex counts. D28's display-only simplification
handles it, but the tolerance needs care: too aggressive and a genuinely
misaligned boundary starts looking aligned, defeating R17's whole purpose.
Simplify enough to render, never enough to hide a discrepancy — and say on screen
that the drawing is simplified.

**R20 — a bad commit could stop the service from starting.** `config.toml` is
read at startup and a malformed or unloadable layer is fatal. D26's dry-run —
building the real `LayerConfig` and loading it through `PolygonLookup` before
writing anything — is what keeps a volunteer from bricking a running deployment.
The timestamped backup is the second line of defense.

**R21 — shapefile text encoding.** The `.dbf` format predates Unicode and
encoding is declared, if at all, in a `.cpg` sidecar. Attribute values with
accents or apostrophes can arrive mangled, and mangled text will be committed as
the layer's display name. Detect the sidecar, default sensibly, and show sample
attribute values in the facts panel so the operator sees mojibake before they
commit rather than after.

**R22 — `PIP_CONFIG` means "the config file" is not a fixed path.** A deployment
may run from a config elsewhere. Writing to `./config.toml` while the service
reads another would leave the operator staring at a layer that never appears.
Resolve the same way `app/config.py` does, and show the resolved path on screen
before committing.

### F7 risks

**R8 — the runtime will surprise people.** At a polite 1 request/second, 2,000
addresses is **~33 minutes**. That is the honest number and it belongs in the
runbook and in the CLI's own startup message ("2,000 rows at 1.0 req/s ≈ 33 min
— Ctrl-C is safe"), not discovered by a user at minute five. Two mitigations
that actually work: rows carrying lat/lon skip geocoding entirely and run in
seconds, and the offline `local_points` geocoder has no rate limit at all. **For
batches of a few thousand, the offline geocoder is the right answer** and the
docs should say so rather than implying the public chain scales.

**R9 — batch is the first thing that can abuse a public service.** There is no
caller-side throttle anywhere in the codebase today (verified 2026-08-01), and
`nominatim.py`'s docstring explicitly delegates rate limiting to the caller
because v1 never had one that mattered. Cook County's ArcGIS locator is a public
county service run for residents; hammering it with a few thousand sequential
requests is the kind of thing that gets an IP blocked and, worse, is rude to a
public agency. D20 makes throttling default-on and non-optional, and refuses
Nominatim's public instance outright. **This is the risk I would not compromise
on** — it is the only one in this plan where getting it wrong harms someone
outside the project.

**R10 — silently wrong output is the real failure mode.** A batch job that
returns 2,000 rows of confident, wrong districts is far more dangerous than one
that crashes — nobody re-checks a spreadsheet that looks fine. Hence D18's
refusal to guess column mappings, the per-row `status`/`reason` columns, and a
non-zero exit code. The runbook should tell people to spot-check a handful of
known addresses against the single-address `/locate` endpoint before trusting a
run.

**R11 — Google's CSV export is not a contracted API.** The `/export?format=csv`
URL is long-standing and widely used but undocumented as a stable interface; it
can change, and it will not warn us. Mitigation: one integration point, a
recorded-response test, and an error message that distinguishes "not shared"
from "we got something that isn't CSV." Accept that this may break someday.

**R12 — SSRF, but only for F7b.** The CLI fetching a URL on the user's own
machine is nothing. An *endpoint* that accepts a user-supplied sheet URL and
fetches it server-side is a textbook SSRF vector into whatever the service can
reach. When F7b is planned: allowlist `docs.google.com`, resolve and reject
private address ranges, and do not follow redirects off-host. Noting it now so
it is not discovered late.

**R13 — `openpyxl` may not install on a locked-down box.** The agencies this
serves are exactly the ones where `pip install` is restricted. CSV and Google
Sheets need nothing beyond the existing dependencies, so the runbook's answer is
"save it as CSV" — which is why XLSX sits behind an optional extra (D17) rather
than becoming a core dependency that could block an install that never needed it.

**R14 — Excel will mangle the data before we ever see it.** Leading-zero ZIP
codes become integers, long house numbers go scientific, dates get "helpfully"
reformatted. Reading every cell as a string (F7-T1) protects us on the way in,
but a ZIP already destroyed in the source file is unrecoverable. Worth a runbook
note: keep address columns formatted as Text.

**R15 — open question: what does a partial run leave behind?** If a 33-minute
job dies at row 1,400, the user wants to resume. A `--resume` flag implies
persisting progress, which brushes against D15's spirit even on the client side.
Cheapest honest answer: write output incrementally so a killed run leaves a
valid partial file, and offer `--skip-rows N` to continue. **Decide during
F7-T4; do not build a state file.**

### F5b risks

**R1 — Python version mismatch (highest risk to the whole design).** The core
requires ≥3.11. ArcGIS Pro's bundled conda Python has been 3.9 across several
Pro 3.x releases and moved to 3.11 only in a later one; ArcMap 10.x is Python
**2.7** and is categorically out of reach. If the target box is anything below
3.11, the in-process plugin cannot exist and D10 must go to the shim. *The
agent has not verified the Pro-version→Python-version mapping and should not be
trusted on it — this is exactly what F5b-T2 is for.*

**R2 — the shim may be the better design anyway.** See the D10 note. Do not let
"the archived plan said entry points" foreclose it.

**R3 — PII on disk (§9 non-negotiable).** `arcpy`'s geocoding surface is
table-oriented; a naive implementation writes the queried address into a scratch
geodatabase, persisting exactly what the service promises never to persist. The
`memory` workspace avoids this, but ArcGIS also has project-level scratch
defaults that can silently land on disk. **The spike must check this
explicitly.** If single-address geocoding cannot be done without a disk write,
that is a spec conflict the maintainer must resolve, not something to engineer
around quietly.

**R4 — licensing.** AGPLv3 core plus proprietary `arcpy` in one process. The
spec's isolation requirement (separate package, separate install) is the
mitigation, and the shim design isolates further by putting them in separate
processes. Whether the AGPL's terms are implicated is a legal question for the
maintainer; flagging it, not resolving it.

**R5 — cost and concurrency.** Each `arcpy` process holds a license seat, and
`arcpy` is not thread-safe. Multi-worker uvicorn multiplies seats and risks
corruption. Hence D13's single-worker constraint — which must be stated loudly
in the runbook, because it silently caps throughput.

**R6 — RESOLVED 2026-08-01: housekeeping, not security.** The maintainer's
goal is to make the work *a bit harder to reverse engineer* — friction, not
secrecy. That settles the proportionality question: rewrite history (C6), keep
the repository and its PR history (C7), and do not chase GitHub Support or a
repo re-creation. The content was public from the first commit and should be
treated as disclosed; the docs carry no credentials, no PII, and no private
endpoints, so nothing about this is urgent.

**What the purge will and won't remove.** Worth stating plainly so the result
isn't mistaken for more than it is. After C6, a cloner can no longer read the
spec, the plans, the runbooks, or the agent instructions from the repo at any
commit. What *remains*, and is not worth removing:

| Residue | Count | Why leave it |
|---|---|---|
| `SPEC §N` references in source | 44 | Load-bearing engineering comments |
| `F<N>`/`F<N>-T<M>` IDs in source | 56 | Same — they trace code to its reason |
| `D<N>` decision references | 53 | Same |
| "ArcGIS / ArcPy equivalent" docstrings | 12 files | Golden rule 3; the most valuable prose in the repo |
| Commit messages carrying `F`/`C` IDs | 30 | Rewriting them churns every SHA for nothing |
| `CHANGELOG.md` naming the doc files | 3 lines | It is a historical record; falsifying it is worse |

Someone determined can still infer the workflow from these. That is fine —
inferring the shape of a process from ID grammar is a different order of effort
from reading the ratified spec verbatim, which is what `bootstrap.py` was
handing out until `bbfbecb`.

**R7 — the plugin will rot.** Nobody on this project has an Esri box; CI cannot
touch it; it will be exercised approximately never. Ship it with an explicit
"last validated on <Pro version> on <date>" line in its README, and treat a
stale one as a known-unknown rather than a working feature.

---

August 1, 2026

#AI/Claude
