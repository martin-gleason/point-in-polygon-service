"""F5-T4 — fully-offline address-point geocoder (SPEC §5 mode 3, PLAN D8).

`LocalAddressPointGeocoder` matches an address against a **local** address-point
reference table (a GeoPackage of points, one per known address) by exact string
equality after normalization. Pure FOSS: no server, no internet, no API key —
the layer is loaded once at construction and every lookup is an in-memory dict
hit. Because there is no transport, this geocoder never raises
`GeocoderUnavailable`; an address it doesn't hold is simply a `no_match`.

Matching pipeline
    query → parse into (house number, street line, optional city/ZIP tail) →
    normalize number and street (app.geocoding.normalize) → look up the exact
    (number, street) key built the same way from every reference row at load
    time. Two spellings of one address ("121 North La Salle Street" /
    "121 N LASALLE ST") canonicalize to the same key, so equality is meaningful.
    When a query carries a city or ZIP, it is used as an optional filter (D8) to
    disambiguate the same street number that recurs across municipalities.

    v1 limitation: the parser handles the common "<number> <street>" form and a
    trailing ", City, ST, ZIP" (or a bare trailing ZIP) — the number is the
    leading token, the street line is what remains after the city/state/ZIP tail
    is stripped. It does not parse unit numbers, intersections, PO boxes, or
    addresses with no leading number. Those simply miss → no_match.

ArcGIS / ArcPy equivalent
    Replaces building and geocoding against a local *address-point* locator —
    `arcpy.geocoding.CreateLocator` with an "AddressPoints" role over a point
    feature class, then `arcpy.geocoding.GeocodeAddresses` (or the ArcGIS Python
    API's `geocode()` against that locator). Here the "locator" is an in-memory
    dict keyed by the normalized address, and geopandas (`read_file` +
    `to_crs`) plays the role of the feature-class read and its on-the-fly
    reprojection to WGS84.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd

from app.geocoding.base import GeocodeResult
from app.geocoding.normalize import normalize_number, normalize_street

# WGS84 lon/lat is the API's coordinate system (SPEC §4); every point the
# geocoder returns is reprojected to it at load time.
WGS84 = "EPSG:4326"

# A US ZIP: five digits, optionally + four (ZIP+4). Used to peel a ZIP off the
# end of a query and to recognize a ZIP token in the city/state/ZIP tail.
_ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")


def _is_zip(token: str) -> bool:
    return bool(_ZIP_RE.match(token))


@dataclass(frozen=True)
class _AddressPoint:
    """One reference address: its WGS84 location and the fields needed to
    reconstruct a canonical `matched_address` string."""

    longitude: float
    latitude: float
    number: str
    street: str
    city: str | None = None
    zip_code: str | None = None

    @property
    def point(self) -> tuple[float, float]:
        return (self.longitude, self.latitude)

    def canonical_address(self) -> str:
        """Rebuild a human-readable address from the stored reference fields:
        "<number> <street>[, <city>][ <zip>]". Uses the row's own (already
        normalized) values so the echoed match is deterministic."""
        line = f"{self.number} {self.street}".strip()
        if self.city:
            line = f"{line}, {self.city}"
        if self.zip_code:
            line = f"{line} {self.zip_code}"
        return line


class LocalAddressPointGeocoder:
    """A Geocoder backed by a local address-point GeoPackage.

    The layer is read once at construction, reprojected to WGS84 if needed, and
    folded into a dict keyed by (normalized_number, normalized_street). Missing
    file / layer / columns fail fast with a clear `ValueError` (the project's
    ConfigError-style fail-fast, without importing ConfigError from config.py).
    """

    def __init__(
        self,
        name: str,
        path: str | Path,
        layer: str,
        number_field: str,
        street_field: str,
        city_field: str | None = None,
        zip_field: str | None = None,
    ):
        self.name = name
        self.path = Path(path)
        self.layer = layer
        self.number_field = number_field
        self.street_field = street_field
        self.city_field = city_field
        self.zip_field = zip_field

        if not self.path.exists():
            raise ValueError(
                f"{name}: address-point file not found: {self.path}"
            )

        try:
            frame = gpd.read_file(self.path, layer=layer)
        except Exception as error:  # noqa: BLE001 — surface any read failure as config error
            raise ValueError(
                f"{name}: could not read layer {layer!r} from {self.path} "
                f"({type(error).__name__})"
            ) from error

        required = [number_field, street_field]
        if city_field:
            required.append(city_field)
        if zip_field:
            required.append(zip_field)
        missing = [column for column in required if column not in frame.columns]
        if missing:
            raise ValueError(
                f"{name}: layer {layer!r} is missing configured columns {missing}"
            )

        if frame.crs is None:
            raise ValueError(f"{name}: layer {layer!r} has no CRS")

        # Reproject the point geometry to WGS84 lon/lat if the layer is stored in
        # another CRS (e.g. EPSG:3435 for Cook County). ArcGIS/ArcPy equivalent:
        # the locator projecting candidate x/y to the output spatial reference.
        if frame.crs.to_epsg() != 4326:
            frame = frame.to_crs(WGS84)

        # (number, street) → list of reference points. A list, not a single
        # value: the same house-number-and-street recurs across municipalities in
        # a county-wide table (e.g. "100 Main St" in several suburbs), and the
        # optional city/ZIP filter disambiguates them. A single-valued map would
        # silently drop all but the last such row (last-wins).
        self._index: dict[tuple[str, str], list[_AddressPoint]] = {}
        for _, row in frame.iterrows():
            geometry = row.geometry
            if geometry is None or geometry.is_empty:
                continue

            number_raw = row[number_field]
            street_raw = row[street_field]
            if number_raw is None or street_raw is None:
                continue

            key = (
                normalize_number(str(number_raw)),
                normalize_street(str(street_raw)),
            )
            if not key[0] or not key[1]:
                continue

            self._index.setdefault(key, []).append(
                _AddressPoint(
                    longitude=float(geometry.x),
                    latitude=float(geometry.y),
                    number=str(number_raw).strip(),
                    street=str(street_raw).strip(),
                    city=self._optional(row, city_field),
                    zip_code=self._optional(row, zip_field),
                )
            )

    @staticmethod
    def _optional(row, field: str | None) -> str | None:
        """A row's value for an optional field, or None when unconfigured/blank."""
        if not field:
            return None
        value = row[field]
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def from_config(cls, entry: dict) -> "LocalAddressPointGeocoder":
        """Build from a config mapping (a [[geocoders]] entry in config.toml).

        Keys: id, path, layer, number_field, street_field, and optional
        city_field / zip_field. A relative `path` is resolved against the
        directory holding config.toml when the entry carries a `_config_dir`
        hint (injected by the loader), otherwise against the current working
        directory — so the offline layer travels with the config, not the
        process's cwd.
        """
        path = Path(entry["path"])
        if not path.is_absolute():
            config_dir = entry.get("_config_dir")
            base = Path(config_dir) if config_dir else Path.cwd()
            path = base / path

        return cls(
            name=entry["id"],
            path=path,
            layer=entry["layer"],
            number_field=entry["number_field"],
            street_field=entry["street_field"],
            city_field=entry.get("city_field"),
            zip_field=entry.get("zip_field"),
        )

    def geocode(self, address: str) -> GeocodeResult:
        """Look up `address` in the local reference table by exact normalized
        (number, street) key, with the query's city/ZIP as an optional
        disambiguating filter. A hit returns matched=True at score 100.0 (an
        exact reference-point match is authoritative — there is no fuzzy ranking
        here); anything else, including an unparseable query, is a `no_match`.
        Never raises."""
        parsed = self._parse_query(address)
        if parsed is None:
            return GeocodeResult.no_match(address, self.name)
        number_raw, street_raw, city_hint, zip_hint = parsed

        key = (normalize_number(number_raw), normalize_street(street_raw))
        candidates = self._index.get(key)
        if not candidates:
            return GeocodeResult.no_match(address, self.name)

        entry = self._pick(candidates, city_hint, zip_hint)
        return GeocodeResult(
            query=address,
            matched=True,
            provider=self.name,
            point=entry.point,
            score=100.0,
            matched_address=entry.canonical_address(),
        )

    @staticmethod
    def _pick(
        candidates: list["_AddressPoint"], city_hint: str | None, zip_hint: str | None
    ) -> "_AddressPoint":
        """Choose among reference points sharing a (number, street) key. If the
        query carried a city or ZIP, prefer a candidate it matches (the D8
        optional filter); if the hint matches none, fall back to the first
        candidate rather than a false miss. First-match on residual ambiguity,
        matching the engine's D4 stable-first convention."""
        if city_hint or zip_hint:
            city = city_hint.upper() if city_hint else None
            zip5 = zip_hint[:5] if zip_hint else None
            for candidate in candidates:
                if city and (candidate.city or "").upper() == city:
                    return candidate
                if zip5 and (candidate.zip_code or "")[:5] == zip5:
                    return candidate
        return candidates[0]

    @staticmethod
    def _parse_query(address: str) -> tuple[str, str, str | None, str | None] | None:
        """Parse a query into (house number, street line, city hint, ZIP hint).

        The leading token of the first comma-segment is the house number and the
        rest of that segment is the street — after a trailing ZIP is peeled off
        (a 5-digit or ZIP+4 token can't be part of a street name, so it's safe to
        strip even without a comma; a bare directional suffix like "NE" is left
        intact). Anything after the first comma is a city/state/ZIP tail: a
        ZIP-pattern token becomes the ZIP hint, a 2-letter token is treated as a
        state and ignored, and the remaining words become the city hint. Returns
        None when there is no "<number> <street>" to match (empty, single token,
        or no leading number+street)."""
        stripped = address.strip()
        if not stripped:
            return None

        segments = [segment.strip() for segment in stripped.split(",")]
        head_parts = segments[0].split(maxsplit=1)
        if len(head_parts) < 2:
            return None
        number, street = head_parts[0], head_parts[1]

        zip_hint: str | None = None
        # A trailing ZIP in the head street line (the no-comma "…St 60602" form).
        street_tokens = street.split()
        if len(street_tokens) > 1 and _is_zip(street_tokens[-1]):
            zip_hint = street_tokens[-1]
            street = " ".join(street_tokens[:-1])

        city_words: list[str] = []
        for segment in segments[1:]:
            for token in segment.split():
                if _is_zip(token):
                    zip_hint = token
                elif len(token) == 2 and token.isalpha():
                    continue  # a state abbreviation — not used for matching
                else:
                    city_words.append(token)
        city_hint = " ".join(city_words) or None

        if not number or not street:
            return None
        return (number, street, city_hint, zip_hint)
