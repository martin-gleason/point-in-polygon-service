# Data provenance — `data/layers.gpkg`

This file records where each shipped polygon layer comes from, how it was
transformed, and what it contained when last built. The GeoPackage is rebuilt by
`scripts/build_data.py`; this document is updated to match after a rebuild.

- **Built with:** `python scripts/build_data.py`
- **Last retrieved / built:** 2026-07-08
- **Stored CRS:** EPSG:3435 (NAD83 / Illinois State Plane East, ftUS) — every
  layer is reprojected to this single planar CRS at build time. Query points
  arrive as WGS84 (EPSG:4326) lon/lat and are reprojected onto it server-side.
- **Format:** OGC GeoPackage (open, single-file) — the FOSS stand-in for a
  proprietary Esri `.gdb`.

---

## Layer `police_districts` — Chicago Police Districts

| Field | Value |
|---|---|
| Source | City of Chicago Data Portal, dataset **24zt-jpfn** — *Boundaries - Police Districts (current)* |
| Access URL | `https://data.cityofchicago.org/api/geospatial/24zt-jpfn?method=export&format=GeoJSON` |
| License | Public domain (City of Chicago open data) |
| Source CRS | WGS84 / EPSG:4326 (GeoJSON export, no CRS member) |
| Polygon features | **25** |
| Distinct districts | **23** — `dist_num` ∈ {1–12, 14–20, 22, 24, 25, 31}. Districts 13, 21, 23 do not exist (historically merged). |
| Multi-feature districts | District **31** (O'Hare) is **3 separate polygons** — this is why the feature count (25) exceeds the district count (23). A point in any of its polygons returns `dist_num` 31. |

**Note on the spec §4 example.** SPEC.md §4 illustrates `/layers` with
`"feature_count": 22`. Twenty-two is the commonly cited number of Chicago
*patrol* districts; the authoritative current dataset ships **25 polygon
features / 23 distinct district numbers** (the extra count is District 31 /
O'Hare and its multi-polygon geometry). The spec's number is illustrative; the
API reports the real loaded feature count, and PLAN.md F1-T2 directed this
build-time verification. `/layers` will report `feature_count: 25` for this
layer.

**Attribute mapping** (source → normalized):

| Source field | Normalized | Example | Notes |
|---|---|---|---|
| `dist_num` | `dist_num` | `"17"` | District number, kept as the source string. |
| `dist_label` | `dist_name` | `"17TH"` | The open dataset carries no long district name; `dist_name` derives from the portal's ordinal label. |

---

## Layer `municipalities` — Cook County Municipalities

| Field | Value |
|---|---|
| Source | Cook County GIS, `politicalBoundary/MapServer` layer **2** — *Municipality* |
| Access URL | `https://gis.cookcountyil.gov/traditional/rest/services/politicalBoundary/MapServer/2/query?where=1=1&outFields=MUNICIPALITY,AGENCY_DESC&f=geojson` |
| License | Cook County open data |
| Source CRS | Native EPSG:3435 (wkid 102671); the `f=geojson` export is delivered in EPSG:4326 and reprojected back to 3435 at build time. |
| Polygon features | **173** |
| Geometry repairs | 10 features had invalid geometry (self-intersections) at last build and were repaired with `make_valid()` before writing. |

The municipalities layer is what lets the service distinguish *"outside Chicago"*
from *"outside Cook County entirely"* — a suburban Cook County address geocodes,
falls in no police district, but does fall in a municipality (e.g. Evanston).

**Attribute mapping** (source → normalized):

| Source field | Normalized | Example |
|---|---|---|
| `MUNICIPALITY` | `muni_name` | `"Evanston"` |
| `AGENCY_DESC` | `agency` | `"VILLAGE OF HAZELCREST"` |

---

## Geocoder — Cook County AddressLocator (SPEC §5 mode 1, F3-T1)

| Field | Value |
|---|---|
| Provider id | `cook_county_arcgis` |
| Service | Cook County GIS, `AddressLocator/CookAddressComposite` **GeocodeServer** |
| Operation | `findAddressCandidates` |
| Base URL | `https://gis.cookcountyil.gov/traditional/rest/services/AddressLocator/CookAddressComposite/GeocodeServer` |
| Auth | none (public) — a private/internal server (mode 2) adds a token by env-var name |
| Service native SR | EPSG:3435 (wkid 102671); the adapter requests `outSR=4326` so results come back as WGS84 lon/lat |
| Score | 0–100, passed through unchanged (D6) |

**Captured sample (retrieved 2026-07-09), used to back the F3-T4 tests:**

`findAddressCandidates?SingleLine=121 N LaSalle St, Chicago, IL 60602&outSR=4326`
→ 1 candidate: `Match_addr = "121 N LA SALLE ST, CHICAGO, IL"`,
`location = {x: -87.63231, y: 41.88366}`, `score = 97.15`.
An unmatchable query returns `{"candidates": []}`. The tests use `respx`-mocked
HTTP shaped from this capture, so CI never depends on the live endpoint.

The adapter sends the request as **POST with the parameters in the body**, not a
GET query string, so the queried address (PII, §9) and any auth token (§9) never
appear in a request URL that could reach an access log or an exception message.

## Reproducibility

`scripts/build_data.py` caches raw downloads in `data/raw/` (gitignored) and
rebuilds `data/layers.gpkg` deterministically. Re-run with `--refresh` to pull
fresh source data. If a portal changes its schema, the pipeline fails loudly
with the missing-field list rather than shipping a malformed layer — update the
`field_map` in `build_data.py` and this document together.

-----
2026-07-08

#AI/Claude
