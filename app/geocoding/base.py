"""F3-T2 — the Geocoder interface.

Every geocoding mode in SPEC §5 (public agency, private/internal, offline,
optional arcpy) sits behind this one interface: an address string in, a
`GeocodeResult` out. The concrete provider, its endpoint, its authentication,
and whether it needs the internet are all configuration, not code.

ArcGIS / ArcPy equivalent
    In the arcpy prototype, geocoding meant `arcpy.geocoding` against a local
    `.loc` composite locator, or the ArcGIS Python API's `geocode()` against a
    published locator through a `GIS` connection. This interface is the open
    replacement: any HTTP geocoder (or a local one) implements `geocode()`, and
    the ArcGIS REST adapter (arcgis.py) talks to a published `GeocodeServer`
    directly over HTTP with no SDK and no license.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class GeocoderUnavailable(Exception):
    """The provider could not be reached or answered with a transport-level
    failure (timeout, connection error, 5xx, unparseable body, ArcGIS error
    payload). Distinct from a successful "no candidates" answer. Maps to HTTP
    502 at the API layer (SPEC §4); in a provider chain it triggers fallthrough
    to the next provider (D7)."""


@dataclass(frozen=True)
class GeocodeResult:
    """The outcome of geocoding one address.

    `point` is (lon, lat) in WGS84 (EPSG:4326) — the coordinate system the whole
    API speaks (SPEC §4). `score` is normalized 0–100 (D6). A no-match carries
    matched=False and leaves point/score/matched_address as None.
    """

    query: str
    matched: bool
    provider: str
    point: tuple[float, float] | None = None
    score: float | None = None
    matched_address: str | None = None

    @classmethod
    def no_match(cls, query: str, provider: str) -> "GeocodeResult":
        return cls(query=query, matched=False, provider=provider)


@runtime_checkable
class Geocoder(Protocol):
    """Address in → GeocodeResult out. `name` is the provider id echoed in the
    result and used to select a provider (`?provider=` at the API layer)."""

    name: str

    def geocode(self, address: str) -> GeocodeResult: ...
