"""F3-T3 — ArcGIS REST geocoder adapter (SPEC §5 modes 1 and 2).

Talks to a published ArcGIS `GeocodeServer`'s `findAddressCandidates` operation
over plain HTTP (httpx) — no `arcgis` SDK, no `arcpy`, no license. The *same*
class serves both:

- **Mode 1 (public agency):** point `base_url` at a public GeocodeServer — the
  default is Cook County's `AddressLocator/CookAddressComposite`.
- **Mode 2 (private/internal):** point `base_url` at a firewalled internal
  ArcGIS Server and set `token_env` to the name of the environment variable
  holding its token. Only configuration differs — not code.

Credentials are referenced by the *name* of an environment variable, never by
value, and never committed (D2, SPEC §9).

ArcGIS / ArcPy equivalent
    Replaces `arcpy.geocoding` against a `.loc` locator, or the ArcGIS Python
    API's `Geocoder(locator_url).geocode(address)`. This calls the same
    GeocodeServer REST operation those tools call under the hood —
    `findAddressCandidates` — and reads the same candidate fields (Score,
    Match_addr, location x/y).
"""
from __future__ import annotations

import os

import httpx

from app.geocoding.base import GeocodeResult, GeocoderUnavailable

FIND_CANDIDATES = "findAddressCandidates"


def _candidate_score(candidate: dict) -> float:
    """A candidate's score as a float, defaulting a missing OR null score to 0.0
    so ranking and thresholding never crash on a malformed server response."""
    score = candidate.get("score")
    return float(score) if isinstance(score, (int, float)) else 0.0


class ArcGISRestGeocoder:
    """A Geocoder backed by an ArcGIS `GeocodeServer`.

    min_score filters out weak candidates: a best candidate below it is treated
    as no match. Default 0 returns any candidate the server offers (the server
    already ranks them); raise it in config to demand a stronger match.
    """

    def __init__(
        self,
        name: str,
        base_url: str,
        token_env: str | None = None,
        timeout: float = 10.0,
        min_score: float = 0.0,
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.token_env = token_env
        self.timeout = timeout
        self.min_score = min_score

    @classmethod
    def from_config(cls, entry: dict) -> "ArcGISRestGeocoder":
        """Build from a config mapping (a [[geocoders]] entry in config.toml).

        Proves the mode-1/mode-2 switch is config-only: the same call builds a
        public or a private-server geocoder depending purely on base_url /
        token_env.
        """
        return cls(
            name=entry["id"],
            base_url=entry["base_url"],
            token_env=entry.get("token_env"),
            timeout=float(entry.get("timeout", 10.0)),
            min_score=float(entry.get("min_score", 0.0)),
        )

    def geocode(self, address: str) -> GeocodeResult:
        params = {
            "SingleLine": address,
            "f": "json",
            "outSR": "4326",  # return WGS84 lon/lat — the API's coordinate system
            "maxLocations": "1",
            "outFields": "Match_addr,Score",
        }
        # Attach a token only if a private/internal server needs one (mode 2).
        # The value comes from the environment by name — never hardcoded (D2).
        if self.token_env:
            token = os.environ.get(self.token_env)
            if token:
                params["token"] = token

        url = f"{self.base_url}/{FIND_CANDIDATES}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                # POST with the params in the body, NOT the query string: the
                # address (PII, §9/D5) and any token (§9/D2) must never appear in
                # a URL that can land in an access log or an exception message.
                response = client.post(url, data=params)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as error:
            # error's string form embeds the request URL; with POST that URL is
            # the bare endpoint, but we still report only the status code so no
            # request detail can ever leak into logs (§9).
            raise GeocoderUnavailable(
                f"{self.name}: HTTP {error.response.status_code}"
            ) from error
        except (httpx.HTTPError, ValueError) as error:
            # Timeout / connect / unparseable JSON — report the failure kind, not
            # the message, to keep request data out of logs (§9).
            raise GeocoderUnavailable(
                f"{self.name}: request failed ({type(error).__name__})"
            ) from error

        if not isinstance(data, dict):
            raise GeocoderUnavailable(f"{self.name}: unexpected response body")

        # ArcGIS reports failures as HTTP 200 with an {"error": ...} body.
        if "error" in data:
            detail = data["error"]
            code = detail.get("code") if isinstance(detail, dict) else None
            raise GeocoderUnavailable(f"{self.name}: ArcGIS error {code or 'unknown'}")

        candidates = data.get("candidates", [])
        if not candidates:
            return GeocodeResult.no_match(address, self.name)

        best = max(candidates, key=_candidate_score)
        if _candidate_score(best) < self.min_score:
            return GeocodeResult.no_match(address, self.name)

        location = best.get("location") or {}
        longitude, latitude = location.get("x"), location.get("y")
        if longitude is None or latitude is None:
            # A candidate with a score but no location is unusable — treat as a
            # provider failure so a chain can fall through (D7).
            raise GeocoderUnavailable(f"{self.name}: candidate had no location")

        return GeocodeResult(
            query=address,
            matched=True,
            provider=self.name,
            point=(float(longitude), float(latitude)),
            score=_candidate_score(best),
            matched_address=best.get("address"),
        )
