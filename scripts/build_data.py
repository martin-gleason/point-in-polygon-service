#!/usr/bin/env python3
"""F1 — data pipeline: open data -> data/layers.gpkg.

Fetches the two v1 polygon layers from public open-data portals, reprojects
them to the region's native planar CRS (EPSG:3435, Illinois State Plane East),
normalizes their attribute names, validates them, and writes them as layers of
a single GeoPackage that the service ships and loads at startup.

    python scripts/build_data.py            # build both layers into data/layers.gpkg
    python scripts/build_data.py --refresh  # re-download raw sources first
    python scripts/build_data.py --address-points  # opt-in offline-geocoder build

Raw downloads land in data/raw/ (gitignored — the pipeline rebuilds them); the
GeoPackage in data/ IS committed, so a default install needs no network.

The optional ``--address-points`` mode is a SEPARATE, opt-in build for the fully
offline geocoder (SPEC §5 mode 3, LocalAddressPointGeocoder). It fetches Cook
County's published open address points and writes a LOCAL
data/address_points.gpkg (layer "address_points"). That file is ~2M points and
is NEVER committed — the agency builds it on its own hardware, and the test
suite ships only a tiny in-test fixture. This mode does not run in the default
build and does not touch data/layers.gpkg.

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
import json
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

# Offline geocoder (SPEC §5 mode 3) target. Kept in its own single-layer
# GeoPackage, distinct from the polygon layers.gpkg, because it is built locally
# and NEVER committed (the full county file is ~2M points). The service's
# LocalAddressPointGeocoder loads this file when configured.
ADDRESS_POINTS_GPKG_PATH = DATA_DIR / "address_points.gpkg"
ADDRESS_POINTS_LAYER = "address_points"

# Address points are stored in WGS84 (EPSG:4326), NOT the 3435 planar CRS the
# polygon layers use: the geocoder's contract is to emit (lon, lat) WGS84
# (GeocodeResult.point, SPEC §4), so we store the points in that CRS and hand
# them back with no reprojection. The point-in-polygon step reprojects the
# resulting point onto 3435 downstream, exactly as it does for any other
# geocoder's WGS84 output.
ADDRESS_POINTS_CRS = "EPSG:4326"

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


class AddressPointSource:
    """The single address-point source for the offline geocoder (SPEC §5 mode 3).

    Structurally the point-layer analogue of LayerSource: it says where to fetch
    and how to normalize. It differs in two ways that matter — the payload is
    Point geometry (not polygons), and it is paged, because the county file is
    ~2M features and no single ArcGIS REST query returns them all.

    field_map renames the source's address attributes to the four normalized,
    nominative columns the LocalAddressPointGeocoder indexes: number, street,
    city, zip (golden rule 5).
    """

    def __init__(self, layer_id, name, base_url, query, field_map, source, page_size=1000):
        self.layer_id = layer_id
        self.name = name
        self.base_url = base_url
        self.query = query
        self.field_map = field_map
        self.source = source
        self.page_size = page_size

    @property
    def raw_path(self):
        return RAW_DIR / f"{self.layer_id}.geojson"

    def page_url(self, offset):
        """URL for one page of the paged ArcGIS REST feature query.

        ArcGIS caps a single response at its `maxRecordCount`; `resultOffset` +
        `resultRecordCount` walk the full set, the REST equivalent of a paged
        cursor over a huge feature class.
        """
        return (
            f"{self.base_url}?{self.query}"
            f"&resultOffset={offset}&resultRecordCount={self.page_size}"
        )


# Cook County's authoritative address points, published as open data. These are
# the same address points many Esri composite locators are built from (SPEC §5.3
# calls this out), which is exactly why loading them offline reproduces a mode-1
# geocode on an air-gapped box. Served from the same Cook County GIS ArcGIS REST
# host as the municipalities polygon layer above.
#
# NOTE: the exact service path and source field names below are pinned from the
# county's published Address Point layer; if the portal reorganizes the service
# or renames a field, normalize_address_points() fails loudly with the missing
# field list (same discipline as normalize()), and this definition +
# docs/data-provenance.md are updated together. This mode is opt-in and is never
# exercised in CI, so the pin is validated at the first real local build.
ADDRESS_POINTS = AddressPointSource(
    layer_id="address_points",
    name="Cook County Address Points",
    base_url="https://gis.cookcountyil.gov/traditional/rest/services/"
    "eGIS_Base/AddressPoint/MapServer/0/query",
    # where=1=1 selects all; outSR=4326 asks ArcGIS to deliver WGS84 lon/lat so
    # the stored points already speak the geocoder's coordinate system.
    query="where=1%3D1&outFields=ADDRNOCOM,STNAMECOM,POSTCOMM,ZIP5"
    "&outSR=4326&f=geojson",
    # ADDRNOCOM = complete address number, STNAMECOM = complete street name,
    # POSTCOMM = postal community (city), ZIP5 = 5-digit ZIP.
    field_map={
        "ADDRNOCOM": "number",
        "STNAMECOM": "street",
        "POSTCOMM": "city",
        "ZIP5": "zip",
    },
    source="Cook County GIS, eGIS_Base/AddressPoint MapServer layer 0 "
    "(Address Points). Cook County open data.",
)


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


def fetch_address_points(source, refresh):
    """Download the paged address-point feature set to data/raw/.

    Walks the ArcGIS REST query with resultOffset until a short page signals the
    end, concatenating every page into one GeoJSON FeatureCollection on disk.
    The queried features carry no PII on the way out — only a fixed where=1=1 and
    the outFields list travel in the URL — so this fetch never logs an address.

    ArcPy equivalent: `arcpy.conversion.FeatureClassToFeatureClass` (or
    `ExportFeatures`) copying the whole address feature class out of a service /
    File Geodatabase; here it is a paged HTTP GET to a single GeoJSON file.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if source.raw_path.exists() and not refresh:
        print(f"  [{source.layer_id}] using cached {source.raw_path.name}")
        return source.raw_path

    features = []
    offset = 0
    while True:
        url = source.page_url(offset)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=300) as response:
            page = json.loads(response.read())
        page_features = page.get("features", [])
        features.extend(page_features)
        print(f"  [{source.layer_id}] fetched {len(features):,} address points")
        # A page shorter than the requested size is the last page. (ArcGIS also
        # sets exceededTransferLimit, but a short page is the robust terminator.)
        if len(page_features) < source.page_size:
            break
        offset += source.page_size

    collection = {"type": "FeatureCollection", "features": features}
    source.raw_path.write_text(json.dumps(collection))
    print(
        f"  [{source.layer_id}] wrote {source.raw_path.name} "
        f"({len(features):,} features)"
    )
    return source.raw_path


def normalize_address_points(source, raw_path):
    """Read the address points, keep WGS84, rename to number/street/city/zip.

    Unlike normalize() for the polygon layers, this keeps the geometry in WGS84
    (ADDRESS_POINTS_CRS) rather than reprojecting to 3435 — see that constant.
    Attributes are coerced to trimmed strings so the geocoder can match on them
    without worrying about numeric/mixed dtypes.

    ArcPy equivalent: `arcpy.management.AlterField` (rename) over an address
    feature class, plus the field-cleanup a Calculate Field pass would do.
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
        frame = frame.set_crs(ADDRESS_POINTS_CRS)
    else:
        frame = frame.to_crs(ADDRESS_POINTS_CRS)

    # Keep only the normalized attributes + geometry.
    frame = frame[list(source.field_map) + ["geometry"]].rename(
        columns=source.field_map
    )

    # Coerce the address parts to clean, trimmed strings (ZIP and house number
    # must stay strings — leading zeros and unit suffixes are significant).
    for column in ("number", "street", "city", "zip"):
        frame[column] = frame[column].astype("string").str.strip()

    # This layer must be points; anything else means the wrong source field/geom.
    non_point = ~frame.geometry.geom_type.isin(["Point"])
    if non_point.any():
        raise ValueError(
            f"{source.layer_id}: {int(non_point.sum())} non-Point geometries; "
            f"expected an address-point layer."
        )
    frame = frame[frame.geometry.notna() & ~frame.geometry.is_empty]

    return frame


def build_address_points(refresh=False):
    """Opt-in build of the offline geocoder's local address-point GeoPackage.

    Writes data/address_points.gpkg (layer "address_points"). Deliberately
    SEPARATE from build(): it is never part of the default build, it never
    touches data/layers.gpkg, and its output is never committed (the full county
    file is ~2M points — the agency builds it locally; the test suite ships only
    a tiny in-test fixture).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    source = ADDRESS_POINTS
    print(f"[{source.layer_id}] {source.name}")
    print(
        "  NOTE: the full Cook County file is ~2M address points and is NEVER "
        "committed — this builds it locally for the offline geocoder (SPEC §5.3)."
    )
    raw_path = fetch_address_points(source, refresh)
    frame = normalize_address_points(source, raw_path)

    # Rewrite from scratch so a rebuild is deterministic (mirrors build()).
    if ADDRESS_POINTS_GPKG_PATH.exists():
        ADDRESS_POINTS_GPKG_PATH.unlink()
    frame.to_file(
        ADDRESS_POINTS_GPKG_PATH, layer=ADDRESS_POINTS_LAYER, driver="GPKG"
    )

    attrs = [column for column in frame.columns if column != "geometry"]
    print(
        f"\nBuilt {ADDRESS_POINTS_GPKG_PATH.relative_to(PROJECT_ROOT)}:\n"
        f"  {ADDRESS_POINTS_LAYER:20} {len(frame):>7} points  {frame.crs}  {attrs}"
    )
    print(
        "\nThis file is local-only (gitignored). Record the retrieval date and "
        "count in docs/data-provenance.md."
    )
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
    parser.add_argument(
        "--address-points",
        action="store_true",
        dest="address_points",
        help="OPT-IN: build the offline geocoder's local address-point "
        "GeoPackage (data/address_points.gpkg) instead of the default polygon "
        "build. Fetches Cook County's ~2M open address points — built locally, "
        "never committed (SPEC §5.3).",
    )
    args = parser.parse_args(argv)
    try:
        if args.address_points:
            build_address_points(refresh=args.refresh)
        else:
            build(refresh=args.refresh)
    except Exception as error:  # noqa: BLE001 — surface a clean message, non-zero exit
        print(f"\nbuild failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
