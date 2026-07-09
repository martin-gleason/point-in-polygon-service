"""F3-T4 — tests for ArcGISRestGeocoder, all HTTP mocked with respx.

The mocked responses are shaped from the live Cook County GeocodeServer capture
recorded in docs/data-provenance.md (F3-T1), so the suite proves the adapter
against the real contract while never touching the network — it passes offline.
"""
import httpx
import pytest
import respx

from app.geocoding.arcgis import ArcGISRestGeocoder
from app.geocoding.base import GeocoderUnavailable

BASE_URL = (
    "https://gis.cookcountyil.gov/traditional/rest/services/"
    "AddressLocator/CookAddressComposite/GeocodeServer"
)
FIND_URL = f"{BASE_URL}/findAddressCandidates"

# Shaped from the live capture (121 N LaSalle St → City Hall).
MATCH_RESPONSE = {
    "spatialReference": {"wkid": 4326, "latestWkid": 4326},
    "candidates": [
        {
            "address": "121 N LA SALLE ST, CHICAGO, IL",
            "location": {"x": -87.63231460695, "y": 41.883658312069},
            "score": 97.15,
            "attributes": {"Match_addr": "121 N LA SALLE ST, CHICAGO, IL", "Score": 97.15},
        }
    ],
}
NO_MATCH_RESPONSE = {"spatialReference": {"wkid": 4326}, "candidates": []}
ARCGIS_ERROR_RESPONSE = {
    "error": {"code": 400, "message": "Unable to complete operation.", "details": []}
}


def _geocoder(**kwargs) -> ArcGISRestGeocoder:
    return ArcGISRestGeocoder(name="cook_county_arcgis", base_url=BASE_URL, **kwargs)


@respx.mock
def test_geocode_match():
    respx.get(FIND_URL).mock(return_value=httpx.Response(200, json=MATCH_RESPONSE))
    result = _geocoder().geocode("121 N LaSalle St, Chicago, IL")
    assert result.matched is True
    assert result.provider == "cook_county_arcgis"
    assert result.point == pytest.approx((-87.63231460695, 41.883658312069))
    assert result.score == pytest.approx(97.15)
    assert result.matched_address == "121 N LA SALLE ST, CHICAGO, IL"


@respx.mock
def test_geocode_no_candidates():
    respx.get(FIND_URL).mock(return_value=httpx.Response(200, json=NO_MATCH_RESPONSE))
    result = _geocoder().geocode("zzzz nowhere 99999")
    assert result.matched is False
    assert result.point is None
    assert result.score is None
    assert result.query == "zzzz nowhere 99999"


@respx.mock
def test_timeout_raises_unavailable():
    respx.get(FIND_URL).mock(side_effect=httpx.TimeoutException("timed out"))
    with pytest.raises(GeocoderUnavailable):
        _geocoder().geocode("anything")


@respx.mock
def test_http_500_raises_unavailable():
    respx.get(FIND_URL).mock(return_value=httpx.Response(500, text="server error"))
    with pytest.raises(GeocoderUnavailable):
        _geocoder().geocode("anything")


@respx.mock
def test_arcgis_error_body_raises_unavailable():
    # ArcGIS reports failures as HTTP 200 with an {"error": ...} body.
    respx.get(FIND_URL).mock(return_value=httpx.Response(200, json=ARCGIS_ERROR_RESPONSE))
    with pytest.raises(GeocoderUnavailable):
        _geocoder().geocode("anything")


@respx.mock
def test_min_score_filters_weak_candidate():
    weak = {"candidates": [{"address": "X", "location": {"x": -87.6, "y": 41.8}, "score": 40.0}]}
    respx.get(FIND_URL).mock(return_value=httpx.Response(200, json=weak))
    result = _geocoder(min_score=90.0).geocode("weak match")
    assert result.matched is False


@respx.mock
def test_token_attached_from_env(monkeypatch):
    monkeypatch.setenv("PRIVATE_GEOCODER_TOKEN", "secret-token-123")
    route = respx.get(FIND_URL).mock(return_value=httpx.Response(200, json=MATCH_RESPONSE))
    _geocoder(token_env="PRIVATE_GEOCODER_TOKEN").geocode("121 N LaSalle St")
    assert route.called
    assert route.calls.last.request.url.params["token"] == "secret-token-123"


@respx.mock
def test_no_token_when_env_unset():
    route = respx.get(FIND_URL).mock(return_value=httpx.Response(200, json=MATCH_RESPONSE))
    _geocoder(token_env="UNSET_TOKEN_VAR").geocode("121 N LaSalle St")
    assert "token" not in route.calls.last.request.url.params


def test_from_config_mode1_and_mode2_are_config_only():
    # Same adapter class; public vs private server differ only in config.
    public = ArcGISRestGeocoder.from_config(
        {"id": "cook_county_arcgis", "base_url": BASE_URL}
    )
    private = ArcGISRestGeocoder.from_config(
        {
            "id": "agency_internal",
            "base_url": "https://gis.internal.example.gov/arcgis/rest/services/Loc/GeocodeServer",
            "token_env": "AGENCY_TOKEN",
            "timeout": 5.0,
            "min_score": 85.0,
        }
    )
    assert public.token_env is None
    assert private.token_env == "AGENCY_TOKEN"
    assert private.base_url != public.base_url
    assert private.min_score == 85.0
    assert type(public) is type(private) is ArcGISRestGeocoder
