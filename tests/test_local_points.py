"""F5-T4 — tests for the fully-offline LocalAddressPointGeocoder.

Everything runs against a tiny synthetic address-point GeoPackage built in a
fixture (3 points, authored in EPSG:3435 so the load-time reprojection to WGS84
is exercised — the same idiom as tests/test_lookup.py's _build_squares). No
network, no shipped data: the suite is self-contained and passes offline.
"""
from pathlib import Path

import geopandas as gpd
import pytest
from pyproj import Transformer
from shapely.geometry import Point

from app.geocoding.base import GeocodeResult, GeocoderUnavailable
from app.geocoding.local_points import LocalAddressPointGeocoder

# Author the reference points in EPSG:3435 (Illinois State Plane East, ftUS —
# the Cook County native CRS). Known WGS84 anchors are projected FORWARD to 3435
# so the geocoder must reproject BACK to WGS84 at load time to return them.
_FWD = Transformer.from_crs("EPSG:4326", "EPSG:3435", always_xy=True)

# (canonical stored fields, WGS84 lon, lat)
_ROWS = [
    # Chicago City Hall.
    ("121", "N LA SALLE ST", "CHICAGO", "60602", -87.63192, 41.88354),
    ("233", "S WACKER DR", "CHICAGO", "60606", -87.63590, 41.87873),
    ("400", "W MADISON ST", "CHICAGO", "60606", -87.63700, 41.88180),
]


def _build_geocoder(tmp_path: Path, **overrides) -> LocalAddressPointGeocoder:
    numbers, streets, cities, zips, geoms = [], [], [], [], []
    for number, street, city, zip_code, lon, lat in _ROWS:
        numbers.append(number)
        streets.append(street)
        cities.append(city)
        zips.append(zip_code)
        x, y = _FWD.transform(lon, lat)
        geoms.append(Point(x, y))

    frame = gpd.GeoDataFrame(
        {"NUMBER": numbers, "STREET": streets, "CITY": cities, "ZIP": zips},
        geometry=geoms,
        crs="EPSG:3435",
    )
    gpkg = tmp_path / "address_points.gpkg"
    frame.to_file(gpkg, layer="points", driver="GPKG")

    kwargs = dict(
        name="local_offline",
        path=gpkg,
        layer="points",
        number_field="NUMBER",
        street_field="STREET",
        city_field="CITY",
        zip_field="ZIP",
    )
    kwargs.update(overrides)
    return LocalAddressPointGeocoder(**kwargs)


@pytest.fixture
def geocoder(tmp_path) -> LocalAddressPointGeocoder:
    return _build_geocoder(tmp_path)


def test_exact_match_after_normalization(geocoder):
    # Verbose, mixed-case, fully-spelled query must match the terse stored form.
    result = geocoder.geocode("121 north la salle street")
    assert isinstance(result, GeocodeResult)
    assert result.matched is True
    assert result.provider == "local_offline"
    assert result.score == 100.0
    # Reprojected back to WGS84 within round-trip tolerance of the anchor.
    assert result.point == pytest.approx((-87.63192, 41.88354), abs=1e-5)


def test_match_reconstructs_canonical_address(geocoder):
    # A plain "<number> <street>" query that normalizes to the stored key; the
    # echoed match is rebuilt from the stored reference fields (incl. city/zip).
    result = geocoder.geocode("121 North La Salle Street")
    assert result.matched is True
    assert result.matched_address == "121 N LA SALLE ST, CHICAGO 60602"


def test_score_is_100_for_a_hit(geocoder):
    assert geocoder.geocode("233 S Wacker Dr").score == 100.0


def test_miss_returns_no_match(geocoder):
    result = geocoder.geocode("999 W Nowhere Ave")
    assert result.matched is False
    assert result.point is None
    assert result.score is None
    assert result.matched_address is None
    assert result.query == "999 W Nowhere Ave"


def test_miss_never_raises(geocoder):
    # Local geocoder has no transport: a non-match is data, never an exception.
    for query in ["999 W Nowhere Ave", "", "   ", "CityHall", "not an address"]:
        result = geocoder.geocode(query)
        assert result.matched is False


def test_wrong_number_same_street_misses(geocoder):
    # Street normalizes fine but the house number isn't in the table → no_match.
    result = geocoder.geocode("122 N La Salle St")
    assert result.matched is False


def test_unparseable_single_token_query_misses(geocoder):
    assert geocoder.geocode("Chicago").matched is False


def test_geocode_never_raises_geocoder_unavailable(geocoder):
    # Belt-and-suspenders: the whole point of mode 3 is it can't be "unavailable".
    try:
        geocoder.geocode("anything at all 123")
    except GeocoderUnavailable:  # pragma: no cover
        pytest.fail("local geocoder must never raise GeocoderUnavailable")


# ---- fail-fast construction (ConfigError-style ValueError) ----

def test_missing_file_raises_value_error(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        LocalAddressPointGeocoder(
            name="local_offline",
            path=tmp_path / "does_not_exist.gpkg",
            layer="points",
            number_field="NUMBER",
            street_field="STREET",
        )


def test_missing_column_raises_value_error(tmp_path):
    with pytest.raises(ValueError, match="missing configured columns"):
        _build_geocoder(tmp_path, street_field="NO_SUCH_STREET_COLUMN")


# ---- from_config ----

def test_from_config_builds_and_matches(tmp_path):
    # Build the fixture gpkg, then construct via from_config with a relative path
    # resolved against a _config_dir hint.
    _build_geocoder(tmp_path)  # writes tmp_path/address_points.gpkg
    geocoder = LocalAddressPointGeocoder.from_config(
        {
            "id": "local_offline",
            "path": "address_points.gpkg",
            "layer": "points",
            "number_field": "NUMBER",
            "street_field": "STREET",
            "city_field": "CITY",
            "zip_field": "ZIP",
            "_config_dir": str(tmp_path),
        }
    )
    assert geocoder.name == "local_offline"
    assert geocoder.geocode("121 N La Salle St").matched is True


def test_from_config_without_optional_fields(tmp_path):
    _build_geocoder(tmp_path)
    geocoder = LocalAddressPointGeocoder.from_config(
        {
            "id": "local_offline",
            "path": "address_points.gpkg",
            "layer": "points",
            "number_field": "NUMBER",
            "street_field": "STREET",
            "_config_dir": str(tmp_path),
        }
    )
    result = geocoder.geocode("121 N La Salle St")
    assert result.matched is True
    # No city/zip configured → canonical address is just number + street.
    assert result.matched_address == "121 N LA SALLE ST"
