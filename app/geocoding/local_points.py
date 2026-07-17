"""F5-T4 — fully-offline address-point geocoder (SPEC §5 mode 3, PLAN D8).

`LocalAddressPointGeocoder` matches an address against a **local** address-point
reference table (a GeoPackage of points, one per known address) by exact string
equality after normalization. Pure FOSS: no server, no internet, no API key —
the layer is loaded once at construction and every lookup is an in-memory dict
hit. Because there is no transport, this geocoder never raises
`GeocoderUnavailable`; an address it doesn't hold is simply a `no_match`.

Matching pipeline
    query → split leading house number from the rest → normalize each half
    (app.geocoding.normalize) → look up the exact (number, street) key built the
    same way from every reference row at load time. Two spellings of one address
    ("121 North La Salle Street" / "121 N LASALLE ST") canonicalize to the same
    key, so equality is meaningful.

    v1 limitation: the query is parsed by a single leading-number split — the
    first whitespace-separated token is treated as the house number and the
    remainder as the street line. This handles the common "<number> <street>"
    form (optionally with a trailing ", City, ZIP" that normalization folds into
    the street tokens); it does not parse unit numbers, intersections, PO boxes,
    or addresses with no leading number. Those simply miss → no_match.

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

from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd

from app.geocoding.base import GeocodeResult
from app.geocoding.normalize import normalize_number, normalize_street

# WGS84 lon/lat is the API's coordinate system (SPEC §4); every point the
# geocoder returns is reprojected to it at load time.
WGS84 = "EPSG:4326"


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

        self._index: dict[tuple[str, str], _AddressPoint] = {}
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

            self._index[key] = _AddressPoint(
                longitude=float(geometry.x),
                latitude=float(geometry.y),
                number=str(number_raw).strip(),
                street=str(street_raw).strip(),
                city=self._optional(row, city_field),
                zip_code=self._optional(row, zip_field),
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
        key. A hit returns matched=True at score 100.0 (an exact reference-point
        match is authoritative — there is no fuzzy ranking here); anything else,
        including an unparseable query, is a `no_match`. Never raises."""
        number_raw, street_raw = self._split_leading_number(address)
        if not number_raw or not street_raw:
            return GeocodeResult.no_match(address, self.name)

        key = (normalize_number(number_raw), normalize_street(street_raw))
        entry = self._index.get(key)
        if entry is None:
            return GeocodeResult.no_match(address, self.name)

        return GeocodeResult(
            query=address,
            matched=True,
            provider=self.name,
            point=entry.point,
            score=100.0,
            matched_address=entry.canonical_address(),
        )

    @staticmethod
    def _split_leading_number(address: str) -> tuple[str, str]:
        """Split a query into (house number, street line) on the first
        whitespace. v1's parser: the leading token is the number, the rest is
        the street. Returns ("", "") when there is no such split (empty query or
        a single token) so the caller can short-circuit to no_match."""
        stripped = address.strip()
        parts = stripped.split(maxsplit=1)
        if len(parts) < 2:
            return ("", "")
        return (parts[0], parts[1])
