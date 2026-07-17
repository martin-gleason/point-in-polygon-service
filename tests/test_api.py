"""F4 — API tests for the point-in-polygon service.

Uses FastAPI's TestClient (no running server). Geocoding endpoints mock the
upstream ArcGIS HTTP with respx, so the whole suite passes with no network.
Exercises the SPEC §4 response shapes and error model for every endpoint:
/health, /layers, GET /geocode, GET /locate, POST /locate.
"""
import logging
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_GPKG = PROJECT_ROOT / "data" / "layers.gpkg"

pytestmark = pytest.mark.skipif(
    not REAL_GPKG.exists(), reason="data/layers.gpkg not built"
)

# The upstream endpoint config.toml's cook_county_arcgis provider POSTs to; the
# adapter appends /findAddressCandidates. respx intercepts it — no live calls.
ARCGIS_FIND_URL = (
    "https://gis.cookcountyil.gov/traditional/rest/services/"
    "AddressLocator/CookAddressComposite/GeocodeServer/findAddressCandidates"
)


def _candidate(address, lon, lat, score):
    return {"candidates": [{"address": address, "location": {"x": lon, "y": lat}, "score": score}]}


# City Hall geocodes into Police District 1; Evanston geocodes but is outside all
# Chicago police districts; the empty set is a no-match.
CITY_HALL = _candidate("121 N LA SALLE ST, CHICAGO, IL", -87.63231, 41.88366, 97.15)
EVANSTON = _candidate("EVANSTON, IL", -87.68770, 42.04512, 95.0)
NO_MATCH = {"candidates": []}


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


# ---- GET /geocode (F4-T3) ----

@respx.mock
def test_geocode_match(client):
    respx.post(ARCGIS_FIND_URL).mock(return_value=httpx.Response(200, json=CITY_HALL))
    response = client.get("/geocode", params={"address": "121 N LaSalle St"})
    assert response.status_code == 200
    body = response.json()
    assert body["matched"] is True
    assert body["point"] == {"lon": -87.63231, "lat": 41.88366}
    assert body["score"] == pytest.approx(97.15)
    assert body["provider"] == "cook_county_arcgis"


@respx.mock
def test_geocode_no_match(client):
    respx.post(ARCGIS_FIND_URL).mock(return_value=httpx.Response(200, json=NO_MATCH))
    body = client.get("/geocode", params={"address": "zzz nowhere"}).json()
    assert body["matched"] is False
    assert body["point"] is None  # §4: point:null on no-match
    assert "score" not in body


def test_geocode_missing_address_400(client):
    response = client.get("/geocode")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_geocode_unknown_provider_400(client):
    response = client.get("/geocode", params={"address": "x", "provider": "nope"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_provider"


@respx.mock
def test_geocode_upstream_failure_502(client):
    respx.post(ARCGIS_FIND_URL).mock(return_value=httpx.Response(500))
    response = client.get("/geocode", params={"address": "121 N LaSalle St"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "geocoder_unavailable"


# ---- GET /locate: geocode → point-in-polygon (F4-T4, F4-T8) ----

@respx.mock
def test_locate_address_to_district(client):
    respx.post(ARCGIS_FIND_URL).mock(return_value=httpx.Response(200, json=CITY_HALL))
    response = client.get(
        "/locate", params={"address": "121 N LaSalle St", "layer": "police_districts"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["geocode"]["matched"] is True
    assert body["layer"] == "police_districts"
    assert body["match"]["found"] is True
    assert body["match"]["feature"]["dist_num"] == "1"


@respx.mock
def test_locate_address_geocodes_but_outside(client):
    respx.post(ARCGIS_FIND_URL).mock(return_value=httpx.Response(200, json=EVANSTON))
    body = client.get(
        "/locate", params={"address": "Evanston", "layer": "police_districts"}
    ).json()
    assert body["geocode"]["matched"] is True
    assert body["match"]["found"] is False
    assert body["match"]["reason"] == "point_outside_all_polygons"


@respx.mock
def test_locate_address_fails_to_geocode(client):
    respx.post(ARCGIS_FIND_URL).mock(return_value=httpx.Response(200, json=NO_MATCH))
    body = client.get(
        "/locate", params={"address": "zzz nowhere", "layer": "police_districts"}
    ).json()
    assert body["geocode"]["matched"] is False
    assert "match" not in body  # §4: no match when the address fails to geocode


def test_locate_unknown_layer_404_before_geocode(client):
    # Unknown layer must 404 without any upstream call; respx.mock with no routes
    # would raise if a request were attempted.
    with respx.mock:
        response = client.get(
            "/locate", params={"address": "121 N LaSalle St", "layer": "nope"}
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_layer"


@respx.mock
def test_locate_address_upstream_failure_502(client):
    respx.post(ARCGIS_FIND_URL).mock(side_effect=httpx.ConnectError("down"))
    response = client.get(
        "/locate", params={"address": "121 N LaSalle St", "layer": "police_districts"}
    )
    assert response.status_code == 502


# ---- F4-T6 (no-PII), F4-T7 (OpenAPI contract) ----

@respx.mock
def test_queried_address_never_logged(client, caplog):
    # SPEC §9 / D5: the *application* must never log the queried address. We quiet
    # httpx's own client logger first — that's the TestClient's HTTP stack (the
    # stand-in for the browser making the request), not the service; in
    # production the server runs uvicorn with --no-access-log. What remains is
    # the app's own logging, which must not contain the address.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    secret_address = "4242 SECRET-CANARY BLVD"
    body = _candidate(secret_address, -87.63231, 41.88366, 97.15)
    respx.post(ARCGIS_FIND_URL).mock(return_value=httpx.Response(200, json=body))
    with caplog.at_level(logging.DEBUG):
        client.get("/locate", params={"address": secret_address, "layer": "police_districts"})
    assert "SECRET-CANARY" not in caplog.text


def test_openapi_matches_section_4_contract_exactly(client):
    # F4-T7 / SPEC §9: the generated contract equals the §4 endpoint set exactly —
    # nothing added or renamed outside the spec.
    paths = client.get("/openapi.json").json()["paths"]
    documented = {(path, method) for path, methods in paths.items() for method in methods}
    assert documented == {
        ("/health", "get"),
        ("/layers", "get"),
        ("/geocode", "get"),
        ("/locate", "get"),
        ("/locate", "post"),
    }
