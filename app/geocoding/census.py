"""F5-T1 — US Census Geocoder adapter (SPEC §5 mode 3: free, key-free public).

Talks to the US Census Bureau's public "onelineaddress" location service over
plain HTTP (httpx) — no API key, no license, no account. That key-free property
is the whole point of this provider: it is the fallback anyone can run offline
of any commercial account (D6, SPEC §5).

The service takes a single-line address and returns candidate matches with a
matched address string and x/y coordinates already in WGS84 lon/lat. The Census
geocoder reports no relevance score, so a returned match is treated as a full
score of 100.0 (D6) — the service only returns candidates it is confident in.

ArcGIS / ArcPy equivalent
    Stands in for `arcpy.geocoding` against a local `.loc` locator, or the
    ArcGIS Python API's `Geocoder(locator_url).geocode(address)`. It calls the
    Census equivalent of `findAddressCandidates` — the `locations/onelineaddress`
    REST operation — and reads the same candidate fields (matchedAddress and the
    location x/y) that an ArcGIS candidate exposes as Match_addr and location.

PII discipline (SPEC §9 / D5)
    Census `onelineaddress` is a GET, so the address rides in the query string of
    the outbound URL. On ANY upstream error we must therefore raise
    GeocoderUnavailable reporting ONLY the status code or the exception type name
    — never `str(error)` or `error.request.url`, both of which embed the queried
    address and would leak PII into a log.
"""
from __future__ import annotations

import httpx

from app.geocoding.base import GeocodeResult, GeocoderUnavailable

ONELINE_PATH = "geocoder/locations/onelineaddress"
DEFAULT_BASE_URL = "https://geocoding.geo.census.gov"
DEFAULT_BENCHMARK = "Public_AR_Current"

# The Census geocoder returns no relevance score; a returned candidate is a
# confident match, so it earns the full normalized score (D6).
CENSUS_MATCH_SCORE = 100.0


class CensusGeocoder:
    """A Geocoder backed by the US Census Bureau onelineaddress service.

    base_url and benchmark are configurable so a caller can pin a historical
    vintage benchmark; the defaults hit the current public production service.
    """

    def __init__(
        self,
        name: str,
        base_url: str = DEFAULT_BASE_URL,
        benchmark: str = DEFAULT_BENCHMARK,
        timeout: float = 10.0,
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.benchmark = benchmark
        self.timeout = timeout

    @classmethod
    def from_config(cls, entry: dict) -> "CensusGeocoder":
        """Build from a config mapping (a [[geocoders]] entry in config.toml)."""
        return cls(
            name=entry["id"],
            base_url=entry.get("base_url", DEFAULT_BASE_URL),
            benchmark=entry.get("benchmark", DEFAULT_BENCHMARK),
            timeout=float(entry.get("timeout", 10.0)),
        )

    def geocode(self, address: str) -> GeocodeResult:
        params = {
            "address": address,
            "benchmark": self.benchmark,
            "format": "json",
        }
        url = f"{self.base_url}/{ONELINE_PATH}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                # A GET puts the address in the query string. That is the service's
                # contract; the compensating control is that no request detail
                # (URL or error message) is ever allowed into an exception below.
                response = client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as error:
            # str(error) and error.request.url both embed the queried address
            # (it's in the GET query string) — report ONLY the status code (§9).
            raise GeocoderUnavailable(
                f"{self.name}: HTTP {error.response.status_code}"
            ) from error
        except (httpx.HTTPError, ValueError) as error:
            # Timeout / connect / unparseable JSON — report the failure kind, not
            # the message or URL, to keep the address out of logs (§9/D5).
            raise GeocoderUnavailable(
                f"{self.name}: request failed ({type(error).__name__})"
            ) from error

        if not isinstance(data, dict):
            raise GeocoderUnavailable(f"{self.name}: unexpected response body")

        result = data.get("result")
        if not isinstance(result, dict):
            raise GeocoderUnavailable(f"{self.name}: unexpected response body")

        matches = result.get("addressMatches")
        if not isinstance(matches, list):
            raise GeocoderUnavailable(f"{self.name}: unexpected response body")
        if not matches:
            return GeocodeResult.no_match(address, self.name)

        best = matches[0]
        coordinates = best.get("coordinates") if isinstance(best, dict) else None
        longitude = coordinates.get("x") if isinstance(coordinates, dict) else None
        latitude = coordinates.get("y") if isinstance(coordinates, dict) else None
        if longitude is None or latitude is None:
            # A match with no coordinates is unusable — treat as a provider
            # failure so a chain can fall through (D7), mirroring arcgis.py.
            raise GeocoderUnavailable(f"{self.name}: match had no coordinates")

        return GeocodeResult(
            query=address,
            matched=True,
            provider=self.name,
            point=(float(longitude), float(latitude)),
            score=CENSUS_MATCH_SCORE,
            matched_address=best.get("matchedAddress"),
        )
