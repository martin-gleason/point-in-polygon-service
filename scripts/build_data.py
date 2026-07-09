#!/usr/bin/env python3
"""F1 — data pipeline: open data -> data/layers.gpkg.

Fetches the two v1 polygon layers from public open-data portals, reprojects
them to the region's native planar CRS (EPSG:3435, Illinois State Plane East),
normalizes their attribute names, validates them, and writes them as layers of
a single GeoPackage that the service ships and loads at startup.

    python scripts/build_data.py            # build both layers into data/layers.gpkg
    python scripts/build_data.py --refresh  # re-download raw sources first

Raw downloads land in data/raw/ (gitignored — the pipeline rebuilds them); the
GeoPackage in data/ IS committed, so a default install needs no network.

ArcGIS / ArcPy equivalent
    This script replaces what an ArcPy user would do with a .gdb and Esri
    tooling: instead of `arcpy.conversion.FeatureClassToFeatureClass` copying
    layers out of a File Geodatabase and `arcpy.management.Project` reprojecting
    them, we read GeoJSON with GeoPandas (GDAL/OGR under the hood — the same
    OGR that ArcGIS itself embeds), reproject with `to_crs` (pyproj, i.e. the
    PROJ library ArcGIS also uses), and write an OGC GeoPackage — an open,
    single-file format that plays the role the proprietary .gdb played, with no
    Esri license required.
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

import geopandas as gpd

# Native CRS for Cook County authoritative data: EPSG:3435, NAD83 / Illinois
# State Plane East (ftUS). We store every layer in this planar CRS so the
# point-in-polygon test runs in a projected space (no geographic-CRS distortion)
# and query points — arriving as WGS84 lon/lat — are reprojected onto it.
TARGET_CRS = "EPSG:3435"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
GPKG_PATH = DATA_DIR / "layers.gpkg"

# A polite, identifying User-Agent — some portals reject the default urllib UA.
USER_AGENT = "point-in-polygon-service/0.1 (data pipeline; AGPLv3 FOSS)"


class LayerSource:
    """One configured layer: where to fetch it and how to normalize it.

    field_map renames source attribute names to the service's normalized,
    nominative names (golden rule 5). Only the mapped columns plus geometry
    survive into the GeoPackage — everything else the portal ships (edit
    timestamps, internal keys, shape-area helpers) is dropped.
    """

    def __init__(self, layer_id, name, url, field_map, source):
        self.layer_id = layer_id
        self.name = name
        self.url = url
        self.field_map = field_map
        self.source = source

    @property
    def raw_path(self):
        return RAW_DIR / f"{self.layer_id}.geojson"


SOURCES = [
    LayerSource(
        layer_id="police_districts",
        name="Chicago Police Districts",
        # Chicago Data Portal dataset 24zt-jpfn, "Boundaries - Police Districts
        # (current)", exported as GeoJSON (WGS84, no CRS member -> EPSG:4326).
        url="https://data.cityofchicago.org/api/geospatial/24zt-jpfn"
        "?method=export&format=GeoJSON",
        # Source carries dist_num ("17") and dist_label ("17TH"); there is no
        # separate long name in the open dataset, so dist_name derives from the
        # portal's dist_label. Recorded in docs/data-provenance.md.
        field_map={"dist_num": "dist_num", "dist_label": "dist_name"},
        source="City of Chicago Data Portal, dataset 24zt-jpfn "
        "(Boundaries - Police Districts, current). Public domain.",
    ),
    LayerSource(
        layer_id="municipalities",
        name="Cook County Municipalities",
        # Cook County GIS, politicalBoundary/MapServer layer 2 ("Municipality"),
        # queried as GeoJSON. ArcGIS emits f=geojson in WGS84 regardless of the
        # layer's native 3435; we reproject back to 3435 below.
        url="https://gis.cookcountyil.gov/traditional/rest/services/"
        "politicalBoundary/MapServer/2/query"
        "?where=1%3D1&outFields=MUNICIPALITY,AGENCY_DESC&f=geojson",
        field_map={"MUNICIPALITY": "muni_name", "AGENCY_DESC": "agency"},
        source="Cook County GIS, politicalBoundary/MapServer layer 2 "
        "(Municipality). Cook County open data.",
    ),
]


def fetch(source, refresh):
    """Download a source to data/raw/, skipping the download if already present.

    ArcPy equivalent: the manual 'add the service to a map and export' step, or
    a `arcpy.conversion.JSONToFeatures` after downloading — here it's one HTTP
    GET to disk.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if source.raw_path.exists() and not refresh:
        print(f"  [{source.layer_id}] using cached {source.raw_path.name}")
        return source.raw_path
    print(f"  [{source.layer_id}] downloading {source.url}")
    request = urllib.request.Request(source.url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = response.read()
    source.raw_path.write_bytes(payload)
    print(f"  [{source.layer_id}] wrote {source.raw_path.name} ({len(payload):,} bytes)")
    return source.raw_path


def normalize(source, raw_path):
    """Read, reproject to TARGET_CRS, rename to normalized columns, validate.

    ArcPy equivalent: `arcpy.management.Project` (reproject) +
    `arcpy.management.AlterField` (rename) + a Repair Geometry pass, condensed.
    """
    frame = gpd.read_file(raw_path)
    if frame.empty:
        raise ValueError(f"{source.layer_id}: source returned zero features")

    missing = [col for col in source.field_map if col not in frame.columns]
    if missing:
        raise ValueError(
            f"{source.layer_id}: source is missing expected fields {missing}; "
            f"got {list(frame.columns)}. The portal schema may have changed — "
            f"update field_map and docs/data-provenance.md."
        )

    if frame.crs is None:
        # GeoJSON without a CRS member is WGS84 by the GeoJSON spec (RFC 7946).
        frame = frame.set_crs("EPSG:4326")
    frame = frame.to_crs(TARGET_CRS)

    # Keep only the normalized attributes + geometry.
    frame = frame[list(source.field_map) + ["geometry"]].rename(
        columns=source.field_map
    )

    # Repair any invalid geometry (self-intersections etc.) — an invalid polygon
    # silently breaks the `covers` predicate and the spatial index downstream.
    invalid = ~frame.geometry.is_valid
    if invalid.any():
        print(f"  [{source.layer_id}] repairing {int(invalid.sum())} invalid geometries")
        frame.geometry = frame.geometry.make_valid()

    return frame


def build(refresh=False):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Rewrite the GeoPackage from scratch so a rebuild is deterministic and never
    # leaves a stale layer behind.
    if GPKG_PATH.exists():
        GPKG_PATH.unlink()

    report = []
    for source in SOURCES:
        print(f"[{source.layer_id}] {source.name}")
        raw_path = fetch(source, refresh)
        frame = normalize(source, raw_path)
        frame.to_file(GPKG_PATH, layer=source.layer_id, driver="GPKG")
        attrs = [column for column in frame.columns if column != "geometry"]
        report.append((source.layer_id, len(frame), attrs, str(frame.crs)))
        print(
            f"  [{source.layer_id}] wrote {len(frame)} features, "
            f"attributes {attrs}, CRS {frame.crs}"
        )

    print(f"\nBuilt {GPKG_PATH.relative_to(PROJECT_ROOT)}:")
    for layer_id, count, attrs, crs in report:
        print(f"  {layer_id:20} {count:>4} features  {crs}  {attrs}")
    print(
        "\nRecord these counts and retrieval date in docs/data-provenance.md "
        "(F1-T3)."
    )
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="re-download raw sources even if cached in data/raw/",
    )
    args = parser.parse_args(argv)
    try:
        build(refresh=args.refresh)
    except Exception as error:  # noqa: BLE001 — surface a clean message, non-zero exit
        print(f"\nbuild failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
