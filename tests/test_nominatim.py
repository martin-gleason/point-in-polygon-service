"""F5-T2 — tests for NominatimGeocoder, all HTTP mocked with respx.

The mocked responses are shaped from Nominatim's documented ``search`` JSON
(jsonv2: an array of candidates, each with STRING lat/lon, display_name, and an
importance in 0..1), so the suite proves the adapter against the real contract
while never touching the network — it passes offline.
"""
import httpx
import pytest
import respx

from app.geocoding.base import GeocoderUnavailable
from app.geocoding.nominatim import DEFAULT_BASE_URL, NominatimGeocoder

SEARCH_URL = f"{DEFAULT_BASE_URL}/search"
USER_AGENT = "point-in-polygon-service-tests/1.0 (https://example.gov/contact)"

# Shaped from Nominatim jsonv2: lat/lon are STRINGS, importance is 0..1.
MATCH_RESPONSE = [
    {
        "lat": "41.8836583",
        "lon": "-87.6323146",
        "display_name": "City Hall, 121, North LaSalle Street, Chicago, IL, USA",
        "importance": 0.75,
    }
]
NO_MATCH_RESPONSE: list = []


def _geocoder(**kwargs) -> NominatimGeocoder:
    kwargs.setdefault("user_agent", USER_AGENT)
    return NominatimGeocoder(name="osm_nominatim", **kwargs)


@respx.mock
def test_geocode_match():
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=MATCH_RESPONSE))
    result = _geocoder().geocode("121 N LaSalle St, Chicago, IL")
    assert result.matched is True
    assert result.provider == "osm_nominatim"
    # point is (lon, lat) WGS84, parsed from the STRING fields.
    assert result.point == pytest.approx((-87.6323146, 41.8836583))
    assert result.score == pytest.approx(75.0)  # importance 0.75 -> 75.0
    assert result.matched_address.startswith("City Hall")
    assert result.query == "121 N LaSalle St, Chicago, IL"


@respx.mock
def test_missing_importance_scores_zero():
    body = [{"lat": "41.8", "lon": "-87.6", "display_name": "X"}]
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=body))
    result = _geocoder().geocode("no importance field")
    assert result.matched is True
    assert result.score == 0.0


@respx.mock
def test_geocode_no_candidates():
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=NO_MATCH_RESPONSE))
    result = _geocoder().geocode("zzzz nowhere 99999")
    assert result.matched is False
    assert result.point is None
    assert result.score is None
    assert result.query == "zzzz nowhere 99999"


@respx.mock
def test_user_agent_header_is_sent():
    # The Nominatim usage policy REQUIRES an identifying User-Agent.
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=MATCH_RESPONSE)
    )
    _geocoder().geocode("121 N LaSalle St")
    assert route.calls.last.request.headers["User-Agent"] == USER_AGENT


@respx.mock
def test_email_param_sent_when_configured():
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=MATCH_RESPONSE)
    )
    _geocoder(email="ops@example.gov").geocode("121 N LaSalle St")
    assert "email=ops%40example.gov" in str(route.calls.last.request.url)


@respx.mock
def test_timeout_raises_unavailable():
    respx.get(SEARCH_URL).mock(side_effect=httpx.TimeoutException("timed out"))
    with pytest.raises(GeocoderUnavailable):
        _geocoder().geocode("anything")


@respx.mock
def test_http_500_raises_unavailable():
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(500, text="server error"))
    with pytest.raises(GeocoderUnavailable):
        _geocoder().geocode("anything")


@respx.mock
def test_non_list_body_raises_unavailable():
    # A valid-JSON but non-list 200 (an error object, misrouted URL, proxy) must
    # not crash — D7 promises GeocoderUnavailable.
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"error": "Bad request"})
    )
    with pytest.raises(GeocoderUnavailable):
        _geocoder().geocode("anything")


@respx.mock
def test_candidate_without_location_raises_unavailable():
    body = [{"display_name": "no coords here", "importance": 0.5}]
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(GeocoderUnavailable):
        _geocoder().geocode("anything")


@respx.mock
def test_address_never_leaks_into_exception():
    # §9 non-negotiable: on an upstream error the queried address (which, being a
    # GET, rides in the request URL) must not appear in the raised exception the
    # service will log.
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(500, text="server error"))
    with pytest.raises(GeocoderUnavailable) as exc_info:
        _geocoder().geocode("121 N LaSalle St, Chicago")
    assert "LaSalle" not in str(exc_info.value)


@respx.mock
def test_timeout_exception_does_not_leak_address():
    respx.get(SEARCH_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    with pytest.raises(GeocoderUnavailable) as exc_info:
        _geocoder().geocode("121 N LaSalle St, Chicago")
    assert "LaSalle" not in str(exc_info.value)


def test_from_config_requires_user_agent():
    with pytest.raises(ValueError) as exc_info:
        NominatimGeocoder.from_config({"id": "osm_nominatim"})
    assert "user_agent" in str(exc_info.value)


def test_from_config_builds_with_defaults():
    geocoder = NominatimGeocoder.from_config(
        {"id": "osm_nominatim", "user_agent": USER_AGENT}
    )
    assert geocoder.name == "osm_nominatim"
    assert geocoder.base_url == DEFAULT_BASE_URL
    assert geocoder.email is None
    assert geocoder.timeout == 10.0


def test_from_config_self_hosted_base_url():
    # SPEC §5 mode 2: point base_url at a self-hosted instance — config only.
    geocoder = NominatimGeocoder.from_config(
        {
            "id": "internal_nominatim",
            "user_agent": USER_AGENT,
            "base_url": "https://nominatim.internal.example.gov/",
            "email": "gis@example.gov",
            "timeout": 5.0,
        }
    )
    assert geocoder.base_url == "https://nominatim.internal.example.gov"  # trimmed
    assert geocoder.email == "gis@example.gov"
    assert geocoder.timeout == 5.0
