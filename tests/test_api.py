"""F4 — API tests for the point-in-polygon service slice.

Uses FastAPI's TestClient (no network, no running server). Exercises the SPEC §4
response shapes and error model for the endpoints shipped so far: /health,
/layers, POST /locate.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_GPKG = PROJECT_ROOT / "data" / "layers.gpkg"

pytestmark = pytest.mark.skipif(
    not REAL_GPKG.exists(), reason="data/layers.gpkg not built"
)


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app())


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert isinstance(body["version"], str) and body["version"]


def test_layers(client):
    response = client.get("/layers")
    assert response.status_code == 200
    layers = {layer["id"]: layer for layer in response.json()["layers"]}
    assert "police_districts" in layers
    assert "municipalities" in layers
    assert layers["police_districts"]["attributes"] == ["dist_num", "dist_name"]
    assert layers["police_districts"]["feature_count"] > 0


def test_locate_point_found(client):
    # Chicago City Hall → Police District 1.
    response = client.post(
        "/locate", json={"lat": 41.88354, "lon": -87.63192, "layer": "police_districts"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["layer"] == "police_districts"
    assert body["match"]["found"] is True
    assert body["match"]["feature"]["dist_num"] == "1"
    assert "reason" not in body["match"]  # exclude_none — §4 shape


def test_locate_point_outside(client):
    # Evanston: geocodes fine but is outside all Chicago police districts.
    response = client.post(
        "/locate", json={"lat": 42.04512, "lon": -87.68770, "layer": "police_districts"}
    )
    assert response.status_code == 200
    match = response.json()["match"]
    assert match["found"] is False
    assert match["reason"] == "point_outside_all_polygons"
    assert "feature" not in match  # exclude_none — §4 shape


def test_locate_unknown_layer_404(client):
    response = client.post(
        "/locate", json={"lat": 41.88, "lon": -87.63, "layer": "nope"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_layer"


def test_locate_invalid_coordinate_400(client):
    response = client.post(
        "/locate", json={"lat": 999.0, "lon": -87.63, "layer": "police_districts"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_coordinate"


def test_locate_missing_field_400(client):
    response = client.post("/locate", json={"lat": 41.88, "layer": "police_districts"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_openapi_documents_shipped_endpoints(client):
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    assert "/health" in paths and "get" in paths["/health"]
    assert "/layers" in paths and "get" in paths["/layers"]
    assert "/locate" in paths and "post" in paths["/locate"]
