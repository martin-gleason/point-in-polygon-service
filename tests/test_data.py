"""F1-T4 — schema tests for the shipped GeoPackage (data/layers.gpkg).

These run against the committed GeoPackage, not a live download, so the default
install and CI need no network. They assert the structural contract each layer
must satisfy for the lookup engine (F2) to trust it: the right layers exist,
each carries exactly its normalized attributes, everything is in the stored CRS,
geometry is valid, and the data is non-empty and in a sane size range.

Feature counts are asserted as floors, not exact values: the open-data portals
update (a district re-drawn, a village annexes a parcel), and a brittle exact
count would fail a legitimate refresh. Provenance records the exact counts at
last build (docs/data-provenance.md).
"""
from pathlib import Path

import geopandas as gpd
import pyogrio
import pytest

GPKG_PATH = Path(__file__).resolve().parent.parent / "data" / "layers.gpkg"
STORED_CRS_EPSG = 3435

# Expected normalized attributes per layer (source-field mapping in build_data.py).
EXPECTED = {
    "police_districts": {"columns": {"dist_num", "dist_name"}, "min_features": 22},
    "municipalities": {"columns": {"muni_name", "agency"}, "min_features": 150},
}


def test_geopackage_exists():
    assert GPKG_PATH.exists(), (
        f"{GPKG_PATH} is missing — run `python scripts/build_data.py` (F1)."
    )


def test_expected_layers_present():
    layers = set(pyogrio.list_layers(GPKG_PATH)[:, 0])
    assert set(EXPECTED) <= layers, f"missing layers: {set(EXPECTED) - layers}"


@pytest.mark.parametrize("layer_id", sorted(EXPECTED))
def test_layer_schema(layer_id):
    spec = EXPECTED[layer_id]
    frame = gpd.read_file(GPKG_PATH, layer=layer_id)

    assert len(frame) >= spec["min_features"], (
        f"{layer_id}: {len(frame)} features, expected >= {spec['min_features']}"
    )

    attributes = set(frame.columns) - {"geometry"}
    assert attributes == spec["columns"], (
        f"{layer_id}: attributes {attributes} != expected {spec['columns']}"
    )

    assert frame.crs is not None and frame.crs.to_epsg() == STORED_CRS_EPSG, (
        f"{layer_id}: CRS is {frame.crs}, expected EPSG:{STORED_CRS_EPSG}"
    )

    assert frame.geometry.notna().all(), f"{layer_id}: has null geometry"
    # GEOS treats an empty polygon as valid, so is_valid alone would let a
    # collapsed geometry through — a point could never fall in it.
    assert (~frame.geometry.is_empty).all(), f"{layer_id}: has empty geometry"
    assert frame.geometry.is_valid.all(), f"{layer_id}: has invalid geometry"
    assert frame.geometry.geom_type.isin({"Polygon", "MultiPolygon"}).all(), (
        f"{layer_id}: non-polygon geometry present"
    )


def test_police_districts_cover_expected_range():
    """The core patrol districts must all be present (a coarse content check)."""
    frame = gpd.read_file(GPKG_PATH, layer="police_districts")
    present = set(frame["dist_num"])
    # Districts 13, 21, 23 do not exist (merged); 31 is O'Hare. Assert the
    # uncontroversial patrol districts are all loaded.
    core = {str(n) for n in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 25]}
    assert core <= present, f"missing core districts: {core - present}"
