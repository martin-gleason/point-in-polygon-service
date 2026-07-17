"""F5-T5 — GeocoderChain: ordered fallback across providers (PLAN D7).

A GeocoderChain is itself a Geocoder: address in, GeocodeResult out. It wraps an
ordered list of member Geocoders and applies D7 fallthrough semantics:

- A member that raises `GeocoderUnavailable` (a transport failure — timeout,
  connect, 5xx, unparseable body) is skipped; the chain tries the next member.
- A member that RETURNS a `GeocodeResult` — whether a match OR an authoritative
  "no candidates" — ends the chain immediately. A "no candidates" answer is
  authoritative: the address was searched and not found. Continuing on to a
  second provider would silently mix providers, and D6 scores from different
  locators are not comparable, so the chain must stop.
- If every member raises `GeocoderUnavailable`, the chain raises
  `GeocoderUnavailable` — the whole fallback set is down.

The answering member's own `provider` field is preserved on the returned result;
the chain never rewrites it, so a caller can always see which locator actually
answered.

ArcGIS / ArcPy equivalent
    ArcPy expresses fallback with a *composite* locator — a `.loc` that lists
    several child locators and tries them in priority order until one returns a
    candidate. GeocoderChain is that composite, moved out of the locator file and
    into configuration: `[[geocoders]]` of `type = "chain"` names its children by
    id, exactly as a composite `.loc` references its participating locators.
"""
from __future__ import annotations

from app.geocoding.base import Geocoder, GeocodeResult, GeocoderUnavailable


class GeocoderChain:
    """A Geocoder that delegates to an ordered list of member Geocoders,
    falling through only on transport failure (D7)."""

    def __init__(self, name: str, providers: list[Geocoder]):
        self.name = name
        self.providers = providers

    @classmethod
    def from_config(
        cls, entry: dict, registry: dict[str, Geocoder]
    ) -> "GeocoderChain":
        """Build from a config mapping plus an already-built provider registry.

        `entry` carries `id` and `providers` (a list of member geocoder ids). The
        registry is populated with the leaf providers first, then chains are
        assembled from it — hence the two-arg signature. An unknown member id is
        a configuration error: fail loudly, naming the bad id and what is
        available. Raises `ValueError` (not ConfigError) to avoid coupling to
        config.py; the integrator maps it.
        """
        members: list[Geocoder] = []
        for provider_id in entry["providers"]:
            try:
                members.append(registry[provider_id])
            except KeyError:
                available = ", ".join(sorted(registry)) or "(none)"
                raise ValueError(
                    f"chain {entry['id']!r}: unknown provider id "
                    f"{provider_id!r}; available: {available}"
                ) from None
        return cls(name=entry["id"], providers=members)

    def geocode(self, address: str) -> GeocodeResult:
        for provider in self.providers:
            try:
                return provider.geocode(address)
            except GeocoderUnavailable:
                # Transport failure on this member — try the next (D7). A
                # returned result (match or authoritative no-match) would have
                # short-circuited the loop above.
                continue
        raise GeocoderUnavailable(f"{self.name}: all providers unavailable")
