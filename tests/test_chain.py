"""F5-T5 — tests for GeocoderChain (PLAN D7 fallback semantics).

No network and no real providers: tiny in-file fakes model the three member
behaviours a chain must distinguish — a match, an authoritative no-match, and a
transport failure. The suite runs fully offline.
"""
from __future__ import annotations

import pytest

from app.geocoding.base import Geocoder, GeocodeResult, GeocoderUnavailable
from app.geocoding.chain import GeocoderChain


class FakeMatch:
    """Returns a match, and counts how often it was consulted (spy)."""

    def __init__(self, name: str):
        self.name = name
        self.calls = 0

    def geocode(self, address: str) -> GeocodeResult:
        self.calls += 1
        return GeocodeResult(
            query=address,
            matched=True,
            provider=self.name,
            point=(-87.6, 41.8),
            score=99.0,
            matched_address="123 FAKE ST",
        )


class FakeNoMatch:
    """Returns an authoritative no-match, and counts consultations (spy)."""

    def __init__(self, name: str):
        self.name = name
        self.calls = 0

    def geocode(self, address: str) -> GeocodeResult:
        self.calls += 1
        return GeocodeResult.no_match(address, self.name)


class FakeDown:
    """Always raises GeocoderUnavailable (transport failure)."""

    def __init__(self, name: str):
        self.name = name
        self.calls = 0

    def geocode(self, address: str) -> GeocodeResult:
        self.calls += 1
        raise GeocoderUnavailable(f"{self.name}: request failed (TimeoutException)")


def test_chain_is_a_geocoder():
    chain = GeocoderChain("chain", [FakeMatch("a")])
    assert isinstance(chain, Geocoder)


def test_first_match_wins():
    first = FakeMatch("first")
    second = FakeMatch("second")
    chain = GeocoderChain("chain", [first, second])

    result = chain.geocode("anywhere")

    assert result.matched is True
    assert result.provider == "first"
    assert first.calls == 1
    assert second.calls == 0  # never consulted once the first answered


def test_down_provider_is_skipped_to_next():
    down = FakeDown("down")
    up = FakeMatch("up")
    chain = GeocoderChain("chain", [down, up])

    result = chain.geocode("anywhere")

    assert result.matched is True
    assert result.provider == "up"
    assert down.calls == 1
    assert up.calls == 1


def test_authoritative_no_match_stops_the_chain():
    # D7: a "no candidates" answer is authoritative. A later provider that WOULD
    # match must never be consulted — mixing providers on no-match is forbidden.
    no_match = FakeNoMatch("primary")
    would_match = FakeMatch("secondary")
    chain = GeocoderChain("chain", [no_match, would_match])

    result = chain.geocode("nowhere 99999")

    assert result.matched is False
    assert result.provider == "primary"
    assert no_match.calls == 1
    assert would_match.calls == 0  # proven never consulted


def test_result_provider_is_answering_member_not_chain():
    chain = GeocoderChain("the_chain", [FakeDown("down"), FakeMatch("winner")])
    result = chain.geocode("anywhere")
    assert result.provider == "winner"
    assert result.provider != "the_chain"  # chain never overwrites it


def test_all_down_raises_unavailable():
    chain = GeocoderChain("chain", [FakeDown("a"), FakeDown("b")])
    with pytest.raises(GeocoderUnavailable) as exc_info:
        chain.geocode("anywhere")
    assert "chain: all providers unavailable" in str(exc_info.value)


def test_empty_chain_raises_unavailable():
    chain = GeocoderChain("chain", [])
    with pytest.raises(GeocoderUnavailable):
        chain.geocode("anywhere")


def test_from_config_resolves_ids_in_order():
    a = FakeMatch("a")
    b = FakeNoMatch("b")
    registry: dict[str, Geocoder] = {"a": a, "b": b}
    chain = GeocoderChain.from_config(
        {"id": "combo", "providers": ["b", "a"]}, registry
    )
    assert chain.name == "combo"
    assert chain.providers == [b, a]  # resolved and ordered as configured


def test_from_config_unknown_member_raises_valueerror():
    registry: dict[str, Geocoder] = {"a": FakeMatch("a")}
    with pytest.raises(ValueError) as exc_info:
        GeocoderChain.from_config(
            {"id": "combo", "providers": ["a", "missing"]}, registry
        )
    message = str(exc_info.value)
    assert "missing" in message  # names the unknown id
    assert "a" in message  # lists what is available


def test_from_config_unknown_member_error_lists_available_ids():
    registry: dict[str, Geocoder] = {"census": FakeMatch("census")}
    with pytest.raises(ValueError) as exc_info:
        GeocoderChain.from_config({"id": "c", "providers": ["nope"]}, registry)
    assert "census" in str(exc_info.value)
