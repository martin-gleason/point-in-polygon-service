"""F5-T2 — OpenStreetMap Nominatim geocoder adapter (SPEC §5 mode 2).

Talks to a Nominatim `search` endpoint over plain HTTP (httpx) — no SDK, no
license, no API key. Nominatim is the geocoder behind OpenStreetMap; running
your own instance is the FOSS, key-free way to satisfy SPEC §5 mode 2 (a
private/internal geocoder you host yourself).

Public vs. self-hosted
    The default `base_url` is the public instance,
    ``https://nominatim.openstreetmap.org``. That instance is provided on a
    best-effort basis under a strict usage policy
    (https://operations.osmfoundation.org/policies/nominatim/): **at most 1
    request per second**, and a genuine, identifying ``User-Agent`` (or
    ``Referer``) is **required** — anonymous bulk traffic gets blocked. Because
    of that policy the public instance is **opt-in** and is deliberately **not**
    part of the default provider chain; point `base_url` at a Nominatim instance
    you host to use it as a default provider. This adapter always sends the
    identifying ``User-Agent`` header the policy demands, but it does not
    rate-limit for you — respect the ≤1 req/s ceiling in the caller.

ArcGIS / ArcPy equivalent
    Replaces `arcpy.geocoding` against a `.loc` locator, or the ArcGIS Python
    API's `Geocoder(locator_url).geocode(address)`, with an open OSM-backed
    locator. Nominatim's ``search`` operation is the analogue of ArcGIS's
    ``findAddressCandidates``: address in, ranked candidates out. Nominatim's
    ``importance`` (0..1, OSM's relevance signal) stands in for ArcGIS's
    ``Score`` (0..100) once normalized (D6).
"""
from __future__ import annotations

import httpx

from app.geocoding.base import GeocodeResult, GeocoderUnavailable

DEFAULT_BASE_URL = "https://nominatim.openstreetmap.org"
SEARCH = "search"


def _importance_score(item: dict) -> float:
    """Nominatim ``importance`` (0..1) normalized to the API's 0–100 score (D6).

    A missing OR null importance defaults to 0.0 so a sparse candidate never
    crashes scoring — mirrors the ArcGIS adapter's null-score handling.
    """
    importance = item.get("importance")
    if not isinstance(importance, (int, float)):
        return 0.0
    return round(importance * 100, 2)


class NominatimGeocoder:
    """A Geocoder backed by an OpenStreetMap Nominatim ``search`` endpoint.

    The identifying ``User-Agent`` is mandatory (Nominatim usage policy); it is
    supplied at construction and sent on every request. An optional contact
    ``email`` is appended so the OSMF operators can reach you — recommended for
    any non-trivial use of the public instance.
    """

    def __init__(
        self,
        name: str,
        user_agent: str,
        base_url: str = DEFAULT_BASE_URL,
        email: str | None = None,
        timeout: float = 10.0,
    ):
        self.name = name
        self.user_agent = user_agent
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.timeout = timeout

    @classmethod
    def from_config(cls, entry: dict) -> "NominatimGeocoder":
        """Build from a config mapping (a [[geocoders]] entry in config.toml).

        ``user_agent`` is required by the Nominatim usage policy; a config that
        omits it is an operator error, not a runtime condition, so fail loudly
        and early with an actionable message rather than emit anonymous traffic
        the upstream will block.
        """
        user_agent = entry.get("user_agent")
        if not user_agent:
            raise ValueError(
                "NominatimGeocoder requires 'user_agent': set a genuine, "
                "identifying User-Agent (e.g. your app name + contact URL) — "
                "the Nominatim usage policy blocks anonymous requests."
            )
        return cls(
            name=entry["id"],
            user_agent=user_agent,
            base_url=entry.get("base_url", DEFAULT_BASE_URL),
            email=entry.get("email"),
            timeout=float(entry.get("timeout", 10.0)),
        )

    def geocode(self, address: str) -> GeocodeResult:
        params = {
            "q": address,
            "format": "jsonv2",
            "limit": "1",
            "addressdetails": "0",
        }
        # An optional contact email is part of the identifying handshake the
        # usage policy asks for; it is operator config, not user PII.
        if self.email:
            params["email"] = self.email

        # Nominatim's search is a GET, so the address (PII, §9/D5) unavoidably
        # rides in the query string of THIS request. That is fine on the wire;
        # the discipline that matters is that it must never reach a log or an
        # exception message — so below we report only the failure kind/status
        # and never str(error) (whose string form embeds the request URL) and
        # never the URL itself.
        headers = {"User-Agent": self.user_agent}
        url = f"{self.base_url}/{SEARCH}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(url, params=params, headers=headers)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as error:
            # Report only the status code — never the URL (which carries the
            # queried address) that error's string form would embed (§9).
            raise GeocoderUnavailable(
                f"{self.name}: HTTP {error.response.status_code}"
            ) from error
        except (httpx.HTTPError, ValueError) as error:
            # Timeout / connect / unparseable JSON — report the failure kind,
            # not the message, to keep the queried address out of logs (§9).
            raise GeocoderUnavailable(
                f"{self.name}: request failed ({type(error).__name__})"
            ) from error

        # Nominatim answers a search with a JSON array of candidates. A non-list
        # body (an error object, a proxy's HTML-as-JSON, a misrouted URL) is a
        # transport-level failure, not a "no candidates" answer (D7).
        if not isinstance(data, list):
            raise GeocoderUnavailable(f"{self.name}: unexpected response body")

        if not data:
            return GeocodeResult.no_match(address, self.name)

        best = data[0]
        if not isinstance(best, dict):
            raise GeocoderUnavailable(f"{self.name}: unexpected candidate shape")

        # lat/lon arrive as STRINGS in Nominatim's JSON; coerce to float.
        latitude, longitude = best.get("lat"), best.get("lon")
        if latitude is None or longitude is None:
            # A candidate with no coordinates is unusable — treat as a provider
            # failure so a chain can fall through (D7).
            raise GeocoderUnavailable(f"{self.name}: candidate had no location")
        try:
            point = (float(longitude), float(latitude))  # (lon, lat) WGS84
        except (TypeError, ValueError) as error:
            raise GeocoderUnavailable(
                f"{self.name}: candidate had unparseable coordinates"
            ) from error

        return GeocodeResult(
            query=address,
            matched=True,
            provider=self.name,
            point=point,
            score=_importance_score(best),
            matched_address=best.get("display_name"),
        )
