"""F5-T1 — tests for CensusGeocoder, all HTTP mocked with respx.

The mocked responses are shaped from the US Census onelineaddress location
service contract (result.addressMatches[] with matchedAddress + coordinates
x/y). All network is mocked so the suite passes offline.
"""
import httpx
import pytest
import respx

from app.geocoding.census import CensusGeocoder, ONELINE_PATH
from app.geocoding.base import GeocoderUnavailable

BASE_URL = "https://geocoding.geo.census.gov"
ONELINE_URL = f"{BASE_URL}/{ONELINE_PATH}"

# Shaped from the Census onelineaddress contract (1600 Pennsylvania Ave).
MATCH_RESPONSE = {
    "result": {
        "input": {"address": {"address": "1600 Pennsylvania Ave NW, Washington, DC"}},
        "addressMatches": [
            {
                "matchedAddress": "1600 PENNSYLVANIA AVE NW, WASHINGTON, DC, 20500",
                "coordinates": {"x": -77.03654, "y": 38.898748},
                "tigerLine": {"tigerLineId": "76225813", "side": "L"},
            }
        ],
    }
}
NO_MATCH_RESPONSE = {
    "result": {
        "input": {"address": {"address": "zzzz nowhere 99999"}},
        "addressMatches": [],
    }
}


def _geocoder(**kwargs) -> CensusGeocoder:
    return CensusGeocoder(name="census", base_url=BASE_URL, **kwargs)


@respx.mock
def test_geocode_match():
    respx.get(ONELINE_URL).mock(return_value=httpx.Response(200, json=MATCH_RESPONSE))
    result = _geocoder().geocode("1600 Pennsylvania Ave NW, Washington, DC")
    assert result.matched is True
    assert result.provider == "census"
    # point is (lon, lat) in WGS84.
    assert result.point == pytest.approx((-77.03654, 38.898748))
    # Census reports no score → a match is full score 100.0 (D6).
    assert result.score == 100.0
    assert result.matched_address == "1600 PENNSYLVANIA AVE NW, WASHINGTON, DC, 20500"
    assert result.query == "1600 Pennsylvania Ave NW, Washington, DC"


@respx.mock
def test_geocode_no_candidates():
    respx.get(ONELINE_URL).mock(return_value=httpx.Response(200, json=NO_MATCH_RESPONSE))
    result = _geocoder().geocode("zzzz nowhere 99999")
    assert result.matched is False
    assert result.point is None
    assert result.score is None
    assert result.matched_address is None
    assert result.query == "zzzz nowhere 99999"


@respx.mock
def test_default_benchmark_and_format_in_query():
    route = respx.get(ONELINE_URL).mock(
        return_value=httpx.Response(200, json=MATCH_RESPONSE)
    )
    _geocoder().geocode("1600 Pennsylvania Ave NW")
    request = route.calls.last.request
    assert request.method == "GET"
    assert request.url.params["benchmark"] == "Public_AR_Current"
    assert request.url.params["format"] == "json"
    assert request.url.params["address"] == "1600 Pennsylvania Ave NW"


@respx.mock
def test_timeout_raises_unavailable():
    respx.get(ONELINE_URL).mock(side_effect=httpx.TimeoutException("timed out"))
    with pytest.raises(GeocoderUnavailable):
        _geocoder().geocode("anything")


@respx.mock
def test_http_500_raises_unavailable():
    respx.get(ONELINE_URL).mock(return_value=httpx.Response(500, text="server error"))
    with pytest.raises(GeocoderUnavailable):
        _geocoder().geocode("anything")


@respx.mock
def test_500_exception_leaks_neither_address_nor_url():
    # §9/D5 non-negotiable: onelineaddress is a GET, so the address is in the
    # outbound URL. On an upstream error the exception the service logs must
    # contain neither the queried address nor any URL.
    address = "1600 Pennsylvania Ave NW, Washington, DC"
    respx.get(ONELINE_URL).mock(return_value=httpx.Response(500, text="server error"))
    with pytest.raises(GeocoderUnavailable) as exc_info:
        _geocoder().geocode(address)
    message = str(exc_info.value)
    assert "Pennsylvania" not in message
    assert address not in message
    # No URL may leak — neither the host nor a scheme prefix.
    assert "census.gov" not in message
    assert "://" not in message


@respx.mock
def test_malformed_body_raises_unavailable():
    # Valid JSON but not the documented shape (misrouted URL / proxy) must not
    # crash with an AttributeError — D7 promises GeocoderUnavailable.
    respx.get(ONELINE_URL).mock(return_value=httpx.Response(200, json=[1, 2, 3]))
    with pytest.raises(GeocoderUnavailable):
        _geocoder().geocode("anything")


@respx.mock
def test_missing_result_key_raises_unavailable():
    respx.get(ONELINE_URL).mock(return_value=httpx.Response(200, json={"foo": "bar"}))
    with pytest.raises(GeocoderUnavailable):
        _geocoder().geocode("anything")


@respx.mock
def test_match_without_coordinates_raises_unavailable():
    body = {
        "result": {
            "addressMatches": [{"matchedAddress": "SOMEWHERE"}]  # no coordinates
        }
    }
    respx.get(ONELINE_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(GeocoderUnavailable):
        _geocoder().geocode("weird server")


def test_from_config_defaults_and_overrides():
    default = CensusGeocoder.from_config({"id": "census"})
    assert default.name == "census"
    assert default.base_url == "https://geocoding.geo.census.gov"
    assert default.benchmark == "Public_AR_Current"
    assert default.timeout == 10.0

    custom = CensusGeocoder.from_config(
        {
            "id": "census_2020",
            "base_url": "https://geocoding.geo.census.gov/",
            "benchmark": "Public_AR_Census2020",
            "timeout": 5.0,
        }
    )
    assert custom.name == "census_2020"
    assert custom.base_url == "https://geocoding.geo.census.gov"  # trailing / stripped
    assert custom.benchmark == "Public_AR_Census2020"
    assert custom.timeout == 5.0
