# Adding a polygon layer — Point-in-Polygon Service

How to point this service at a new set of shapes: voting precincts, wards,
service areas, school attendance zones, council districts — any polygon set.

The engine is layer-agnostic by design (SPEC §1): **which polygons it serves is
configuration, not code.** Adding a layer is three required steps and takes
minutes. You do not touch `app/` at all.

1. Get the shapes into a GeoPackage layer.
2. Register that layer in `config.toml`.
3. Record where the data came from in `docs/data-provenance.md`.

Then verify (§4). The gotchas in §5 are the ones that actually bite.

> **ArcGIS / ArcPy equivalent.** This whole runbook replaces what you would do
> in ArcGIS by importing a feature class into a File Geodatabase
> (`arcpy.conversion.FeatureClassToFeatureClass`), reprojecting it
> (`arcpy.management.Project`), renaming fields
> (`arcpy.management.AlterField`), and then publishing a service definition so
> the layer is queryable. Here the `.gdb` is an OGC GeoPackage, the service
> definition is `config.toml`, and none of it needs an Esri license.

---

## 1. Get the shapes into a GeoPackage

There are two paths. Pick by where your data lives.

### Path A — the source is a public open-data URL (preferred)

Add a `LayerSource` to the `SOURCES` list in `scripts/build_data.py`. This is
how both shipped layers are defined, and it is preferred because the build is
then **reproducible**: anyone can rebuild the data from scratch with one
command, and the pipeline validates the source schema for you.

```python
LayerSource(
    layer_id="wards",                       # becomes the GeoPackage layer name
    name="Chicago Wards",
    url="https://data.cityofchicago.org/api/geospatial/<dataset-id>"
        "?method=export&format=GeoJSON",
    # Maps SOURCE column name -> the normalized name the service exposes.
    # Only these columns (plus geometry) survive into the GeoPackage.
    field_map={"ward": "ward_num", "ward_name": "ward_name"},
    source="City of Chicago Data Portal, dataset <id>. Public domain.",
),
```

Then rebuild:

```bash
python scripts/build_data.py            # uses cached downloads in data/raw/
python scripts/build_data.py --refresh  # re-download the sources first
```

The pipeline reprojects to `TARGET_CRS` (EPSG:3435), keeps only the mapped
columns, repairs invalid geometry, and writes the layer into
`data/layers.gpkg`. It prints the feature count, CRS, and attribute list for
each layer — copy those numbers into the provenance doc in §3.

If the portal renamed a field, the build **fails loudly** with the missing
field list rather than writing a half-correct layer. That is deliberate: fix
`field_map` and re-run.

### Path B — you have a local shapefile / GeoJSON / File Geodatabase

Write it into the GeoPackage yourself with GeoPandas. Read §5's first gotcha
before you do — a hand-added layer in `data/layers.gpkg` is **destroyed** the
next time anyone runs `build_data.py`, so for anything you intend to keep, use
a **separate GeoPackage file** the pipeline never touches:

```python
import geopandas as gpd

frame = gpd.read_file("~/Downloads/precincts.shp")   # or .geojson, or a .gdb layer

# Normalize: keep only the columns the API should expose, with clear,
# nominative names (golden rule 5 — `precinct_num`, not `PRECINCT_N`).
frame = frame[["PRECINCT_N", "WARD_NUM", "geometry"]].rename(
    columns={"PRECINCT_N": "precinct_num", "WARD_NUM": "ward_num"}
)

# The layer MUST carry a CRS (see §5). If the source declares one, keep it;
# only set_crs when the file genuinely has none and you know what it is.
if frame.crs is None:
    frame = frame.set_crs("EPSG:4326")

# Repair self-intersections etc. — an invalid polygon silently breaks the
# `covers` predicate and the spatial index.
invalid = ~frame.geometry.is_valid
if invalid.any():
    frame.geometry = frame.geometry.make_valid()

frame.to_file("data/precincts.gpkg", layer="precincts", driver="GPKG")
```

You do **not** have to reproject to EPSG:3435. Each layer keeps its own native
CRS; the engine builds a transformer per layer and reprojects the incoming
WGS84 query point onto it (`app/lookup.py`). Reprojecting the authoritative
polygons is the thing to avoid.

## 2. Register the layer in `config.toml`

Add a `[[layers]]` block. All six keys are **required** — `app/config.py`
validates them at startup and refuses to boot on a bad entry, so a
configuration mistake surfaces immediately instead of as a 500 on the first
query.

```toml
[[layers]]
id = "precincts"                    # the API's layer id: ?layer=precincts
name = "Chicago Voting Precincts"   # human label, shown by GET /layers
path = "data/precincts.gpkg"        # resolved RELATIVE TO config.toml
layer = "precincts"                 # the layer name INSIDE the GeoPackage
attributes = ["precinct_num", "ward_num"]   # columns returned on a hit
source = "Chicago Data Portal, dataset <id>, retrieved 2026-08-01."
```

| Key | Must satisfy |
|---|---|
| `id` | Unique across all layers; this is the `?layer=` value callers pass. |
| `path` | File must exist at startup (relative paths resolve against `config.toml`, not the working directory). |
| `layer` | Must name a layer that actually exists in that GeoPackage. |
| `attributes` | Non-empty, and every name must be a real column — startup fails otherwise. Order matters: the first attribute breaks ties when a point sits on a shared boundary. |
| `source` | Free text, but treat it as required provenance, not decoration. |

Restart the service to pick it up — layers load once at startup.

## 3. Record provenance

Add a section to `docs/data-provenance.md` following the existing ones:
the source URL/dataset id, the retrieval date, the license, the feature count
and CRS from the build output, and the field mapping you applied. This is what
lets the next maintainer tell a stale layer from a current one.

## 4. Verify

Never call it done on assertion — run the checks:

```bash
pytest -q                              # the suite must stay green
python scripts/check_no_secrets.py     # part of "passing" in CI

uvicorn app.main:app --no-access-log   # then, in another shell:
curl -s http://127.0.0.1:8000/layers   # your layer, with its feature_count

# A point you KNOW falls inside a specific polygon:
curl -s -X POST http://127.0.0.1:8000/locate \
  -H "Content-Type: application/json" \
  -d '{"lat": 41.8838, "lon": -87.6319, "layer": "precincts"}'

# A point you know is outside every polygon — must be found:false with a
# reason, NOT a 500:
curl -s -X POST http://127.0.0.1:8000/locate \
  -H "Content-Type: application/json" \
  -d '{"lat": 0.0, "lon": 0.0, "layer": "precincts"}'
```

Check the returned attributes against the authoritative source for at least one
known address — a layer that loads cleanly can still be the wrong vintage or
the wrong geography.

See `docs/runbooks/testing.md` for the full testing runbook and the
confusing-but-likely edge cases.

## 5. Gotchas

**`build_data.py` rewrites `data/layers.gpkg` from scratch.** `build()` deletes
the file before writing, so a rebuild is deterministic and never leaves a stale
layer behind. The corollary: **any layer you hand-added to `layers.gpkg` is
silently gone after the next build.** Either define it in `SOURCES` (Path A) or
keep it in its own `.gpkg` (Path B).

**A layer with no CRS fails at startup.** `_LoadedLayer` raises
`layer '<id>' has no CRS`. Shapefiles missing their `.prj` are the usual
culprit. Set the CRS deliberately — do not guess.

**Configured attributes must exist as columns.** A typo in `attributes` fails
at startup with the missing list, not at query time. Same for `layer` naming a
GeoPackage layer that isn't there.

**Boundary points can match more than one polygon.** The engine uses
`covered_by`, so a point exactly on a shared edge is covered by both neighbors.
It returns the first by a stable sort on your **first configured attribute**, so
order `attributes` with the most identifying column first.

**Null attribute values are fine** and serialize as JSON `null` — the shipped
municipalities layer has features with no `muni_name`. Don't filter those rows
out to make the output tidy; they are real polygons.

**Committing the data.** Shipped GeoPackages **are** committed (that is what
makes a default install work with no network), but `data/raw/` is gitignored and
large derived files are not committed — the ~2M-point address file is the
standing example. If your layer is big, ship the build recipe rather than the
bytes.

**No proprietary dependencies.** Getting shapes out of a `.gdb` or a locked
Esri environment is fine as a one-time local conversion, but nothing in the
runtime path may require `arcpy` or an ArcGIS license (golden rule 2, SPEC §9).

---

July 2026

#AI/Claude
