"""F5-T6 — the locked-workstation acceptance test (SPEC §7).

The headline promise of §5 mode 3: on an air-gapped machine, an address is
geocoded and located with **no internet at all**. This test proves it end to end
through the real FastAPI app:

1. Build a synthetic police-district polygon and a synthetic address-point layer
   (both in EPSG:3435, the Cook County native CRS, so the point is reprojected).
   The one address point sits inside the one district polygon.
2. Wire an AppConfig whose only geocoder is a `local_points` provider, marked the
   default — the fully-offline mode.
3. **Physically block the network**: monkeypatch socket creation to raise. Any
   provider that reached for the internet would fail the test.
4. `GET /locate?address=…&layer=…` and assert the address geocoded offline and
   landed in the right district.

No shipped data, no respx, no network — self-contained and hermetic.
"""
import socket
from pathlib import Path

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from pyproj import Transformer
from shapely.geometry import Point, box

from app.config import AppConfig, GeocoderConfig, LayerConfig
from app.main import create_app

# Author reference geometry in EPSG:3435 (ftUS) from a known WGS84 anchor, so the
# engine and the geocoder both exercise the WGS84 -> 3435 -> WGS84 round trip.
_FWD = Transformer.from_crs("EPSG:4326", "EPSG:3435", always_xy=True)
CITY_HALL_LONLAT = (-87.63192, 41.88354)


@pytest.fixture
def offline_app(tmp_path: Path) -> TestClient:
    ax, ay = _FWD.transform(*CITY_HALL_LONLAT)

    # One district polygon comfortably containing the address point.
    district = box(ax - 5_000, ay - 5_000, ax + 5_000, ay + 5_000)
    districts = gpd.GeoDataFrame(
        {"dist_num": ["1"], "dist_name": ["Central"]},
        geometry=[district],
        crs="EPSG:3435",
    )
    districts_gpkg = tmp_path / "districts.gpkg"
    districts.to_file(districts_gpkg, layer="districts", driver="GPKG")

    # One address point inside that polygon.
    points = gpd.GeoDataFrame(
        {"NUMBER": ["121"], "STREET": ["N LA SALLE ST"], "CITY": ["CHICAGO"], "ZIP": ["60602"]},
        geometry=[Point(ax, ay)],
        crs="EPSG:3435",
    )
    points_gpkg = tmp_path / "address_points.gpkg"
    points.to_file(points_gpkg, layer="points", driver="GPKG")

    config = AppConfig(
        layers={
            "police_districts": LayerConfig(
                id="police_districts",
                name="Test Police Districts",
                path=districts_gpkg,
                layer="districts",
                attributes=("dist_num", "dist_name"),
                source="synthetic offline-test fixture",
            )
        },
        geocoders={
            "offline": GeocoderConfig(
                id="offline",
                type="local_points",
                options={
                    "id": "offline",
                    "path": str(points_gpkg),  # absolute — no _config_dir needed
                    "layer": "points",
                    "number_field": "NUMBER",
                    "street_field": "STREET",
                    "city_field": "CITY",
                    "zip_field": "ZIP",
                },
            )
        },
        default_geocoder="offline",
    )
    # Build the app (reads the local GeoPackages) BEFORE cutting the network, so a
    # false failure can't come from file IO — only the request path is under the
    # no-network regime.
    return TestClient(create_app(config))


def _cut_network(monkeypatch) -> None:
    """Sever OUTBOUND network — DNS resolution and connects — while leaving
    socket *construction* intact. A blanket `socket.socket` patch would also
    break asyncio's in-process self-pipe (which the TestClient's event loop
    needs); air-gapping only means no traffic leaves the machine, so blocking
    getaddrinfo/create_connection/connect is both correct and sufficient. Any
    provider reaching for the internet dies here."""
    def _forbidden(*args, **kwargs):
        raise RuntimeError("network access attempted during the offline test")

    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)
    monkeypatch.setattr(socket.socket, "connect", _forbidden, raising=False)


def test_network_block_actually_bites(monkeypatch):
    # Guard against a vacuous test: prove the block really stops a socket.
    _cut_network(monkeypatch)
    with pytest.raises(RuntimeError):
        socket.create_connection(("example.com", 80))


def test_offline_geocode_then_locate_end_to_end(offline_app, monkeypatch):
    _cut_network(monkeypatch)

    response = offline_app.get(
        "/locate",
        params={"address": "121 north la salle street", "layer": "police_districts"},
    )

    assert response.status_code == 200
    body = response.json()
    # Geocoded with zero network — the offline provider answered.
    assert body["geocode"]["matched"] is True
    assert body["geocode"]["provider"] == "offline"
    assert body["geocode"]["score"] == 100.0
    # And located in the right polygon.
    assert body["match"]["found"] is True
    assert body["match"]["feature"]["dist_num"] == "1"


def test_offline_geocode_endpoint_no_network(offline_app, monkeypatch):
    _cut_network(monkeypatch)
    body = offline_app.get("/geocode", params={"address": "121 N La Salle St"}).json()
    assert body["matched"] is True
    assert body["provider"] == "offline"
    assert body["point"]["lon"] == pytest.approx(CITY_HALL_LONLAT[0], abs=1e-4)
    assert body["point"]["lat"] == pytest.approx(CITY_HALL_LONLAT[1], abs=1e-4)
