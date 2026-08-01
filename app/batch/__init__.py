"""F7 — batch point-in-polygon: the shared contracts.

This package turns a list of rows (from a CSV, an XLSX, or a published Google
Sheet) into the same answers `GET /locate` gives for one address at a time. The
engine in `runner.py` does no file I/O at all: it takes rows in and yields
results out, so the CLI (F7-T4) and the later API endpoint (F7b) are two thin
shells over one tested core.

Three rules from the SPEC shape everything here:

- **Nothing is persisted** (SPEC §9, plan D15). Rows stream through memory.
  There is no job queue, no scratch file, no state store. The only file written
  is the one the operator names on the command line — their own data, on their
  own machine, at their request.
- **Every cell is a string** (plan R14). Coercing types silently destroys
  leading zeros in ZIP codes and house numbers, and no downstream step can
  recover them. Readers hand back `str` and let the geocoder decide meaning.
- **Column mapping is explicit** (plan D18). We never guess which column holds
  the address. Guessing wrong on a few thousand real addresses produces
  confident, plausible, unnoticed garbage — the worst failure this feature has.

ArcGIS / ArcPy equivalent
    This is `arcpy.geocoding.geocodeAddresses` (a table of addresses in, a point
    feature class out) followed by `arcpy.analysis.SpatialJoin` against the
    boundary layer, then exporting the joined table back to a spreadsheet. Here
    it is one pass over the rows with no intermediate feature classes, no
    geodatabase, and no Esri license.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

# Result columns are prefixed so a batch run can never silently overwrite a
# column the operator's own file already has (a "status" or "score" column is
# entirely plausible in a caseload spreadsheet).
RESULT_PREFIX = "pip_"

# Per-row outcomes. Every row gets exactly one, and a row that fails never
# aborts the run (plan D19).
STATUS_MATCHED = "matched"  # geocoded (or given) and landed in a polygon
STATUS_OUTSIDE = "outside_all_polygons"  # valid point, no containing polygon
STATUS_NO_GEOCODE = "no_geocode"  # the address could not be geocoded
STATUS_ERROR = "error"  # bad input or provider failure; see the reason column


class BatchError(Exception):
    """A whole-run failure: unreadable source, unusable column mapping, an
    unknown layer. Distinct from a per-row failure, which becomes a
    ``STATUS_ERROR`` row and lets the run continue."""


@dataclass(frozen=True)
class ColumnMapping:
    """Which of the operator's columns hold the address, or the coordinates.

    Exactly one mode must be supplied: an ``address`` column to geocode, or both
    ``lat`` and ``lon`` columns to use directly. The lat/lon mode never touches
    a geocoder — no network, no rate limit, no address leaving the machine — and
    is the fast path for an already-geocoded list.
    """

    address: str | None = None
    lat: str | None = None
    lon: str | None = None

    def __post_init__(self) -> None:
        has_address = self.address is not None
        has_point = self.lat is not None and self.lon is not None
        if has_address and has_point:
            raise BatchError(
                "give either an address column or lat/lon columns, not both"
            )
        if not has_address and not has_point:
            if self.lat is not None or self.lon is not None:
                raise BatchError("lat and lon columns must be given together")
            raise BatchError(
                "no column mapping: give --address-column, or both "
                "--lat-column and --lon-column"
            )

    @property
    def geocodes(self) -> bool:
        """True when rows carry addresses that need a geocoder."""
        return self.address is not None

    def required_columns(self) -> tuple[str, ...]:
        if self.geocodes:
            return (self.address,)  # type: ignore[return-value]
        return (self.lat, self.lon)  # type: ignore[return-value]


@dataclass(frozen=True)
class Source:
    """A read source: its header order, and a lazy iterator over its rows.

    ``headers`` preserves the operator's original column order so the output
    file can reproduce it exactly. ``rows`` is an iterator, not a list, so a
    large workbook never lands in memory whole.
    """

    headers: tuple[str, ...]
    rows: Iterator[dict[str, str]]
    # Where this came from, for error messages ("row 41 of caseload.csv").
    name: str = "<source>"


@dataclass(frozen=True)
class RowResult:
    """One row's outcome: the operator's original row, plus what we found.

    ``row`` is returned unmodified — the writer reproduces every original column
    before appending any result column, so a batch run is non-destructive to the
    operator's data.
    """

    row: dict[str, str]
    status: str
    reason: str | None = None
    matched_address: str | None = None
    score: float | None = None
    provider: str | None = None
    lon: float | None = None
    lat: float | None = None
    # The configured layer's attributes for a hit, e.g. {"dist_num": "17"}.
    feature: dict | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_MATCHED


def result_columns(layer_attributes: tuple[str, ...]) -> tuple[str, ...]:
    """The columns a run appends, in order, for a layer with these attributes.

    Kept here rather than in the writer so the CLI can print the output schema
    before a long run starts, and so F7b can document it without importing the
    file-writing code.
    """
    fixed = (
        f"{RESULT_PREFIX}status",
        f"{RESULT_PREFIX}reason",
        f"{RESULT_PREFIX}matched_address",
        f"{RESULT_PREFIX}score",
        f"{RESULT_PREFIX}provider",
        f"{RESULT_PREFIX}lon",
        f"{RESULT_PREFIX}lat",
    )
    return fixed + tuple(f"{RESULT_PREFIX}{attr}" for attr in layer_attributes)
