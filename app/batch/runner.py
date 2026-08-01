"""F7-T3 — the batch engine: rows in, `RowResult`s out.

This is the whole of the batch feature's logic and none of its plumbing. It does
no file I/O, no argument parsing, and no printing: it takes an iterable of row
dicts and yields exactly one `RowResult` per row. The CLI (F7-T4) and the later
API endpoint (F7b) are thin shells around `run_batch`, so both inherit the same
tested behaviour.

Four rules drive the design:

- **One result per row, always.** A row that fails becomes a `STATUS_ERROR`
  result and the run continues (plan D19). A run over 2,000 addresses must not
  die on row 1,847 because one cell holds `N/A`.
- **Whole-run failures fail *first*.** A missing geocoder or an unknown layer id
  is detected before any row is processed, not thirty minutes into a rate-limited
  run. These raise `BatchError`; per-row problems never do.
- **No PII anywhere** (SPEC §9). The queried address is never placed in a reason
  string, never logged, and never interpolated into an exception. Reasons come
  from a fixed vocabulary keyed on the failure kind — a provider's own exception
  text is deliberately *not* echoed, because a geocoder is free to include the
  query it failed on in its message. A row's position is safe to report; its
  contents are not. There is no logger in this module on purpose.
- **The lat/lon path never touches a geocoder.** With `mapping.geocodes` False,
  no provider is constructed, no rate limiter is consulted, and no byte leaves
  the machine — the run works with the network physically severed.

ArcGIS / ArcPy equivalent
    The geocoding path is `arcpy.geocoding.geocodeAddresses` (address table in,
    point feature class out) followed by `arcpy.analysis.SpatialJoin` against the
    boundary layer; the lat/lon path is `arcpy.management.XYTableToPoint` feeding
    the same spatial join. Where those tools write intermediate feature classes
    into a scratch geodatabase and fail the whole tool run on a bad record, this
    streams row by row through memory, writes nothing, and turns a bad record
    into one flagged output row.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator

from app.batch import (
    STATUS_ERROR,
    STATUS_MATCHED,
    STATUS_NO_GEOCODE,
    STATUS_OUTSIDE,
    BatchError,
    ColumnMapping,
    RowResult,
)
from app.geocoding.base import Geocoder, GeocoderUnavailable
from app.lookup import InvalidCoordinateError, PolygonLookup

# The fixed reason vocabulary. Every string here is a constant: none is built
# from row content or from a provider's exception text, so no reason can ever
# carry an address (SPEC §9).
REASON_MISSING_ADDRESS = "address column is empty"
REASON_MISSING_COORDINATE = "coordinate column is empty"
REASON_UNPARSEABLE_COORDINATE = "coordinate is not a number"
REASON_COORDINATE_OUT_OF_RANGE = "coordinate outside valid WGS84 range"
REASON_NO_GEOCODE = "address not found by the geocoder"
REASON_GEOCODER_UNAVAILABLE = "geocoder unavailable"
REASON_GEOCODER_NO_POINT = "geocoder reported a match with no coordinates"


class RateLimiter:
    """Enforce a minimum interval between successive geocode calls.

    Public geocoders publish a courtesy rate (Nominatim's is one request per
    second) and a batch run is exactly the workload that violates it. The clock
    and the sleep function are injectable so tests can prove the interval is
    honoured without spending real seconds waiting for it.

    The first `wait()` never blocks; each later one blocks only for whatever is
    left of the interval since the previous call returned.

    ArcGIS / ArcPy equivalent
        `arcpy.geocoding.geocodeAddresses` against a hosted ArcGIS locator lets
        the service's own throttling handle this and fails the tool when it trips
        a quota. Here the throttle is client-side and explicit, so a run stays
        inside a provider's published limit instead of discovering it as errors.
    """

    def __init__(
        self,
        min_interval_seconds: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if min_interval_seconds < 0:
            raise ValueError(
                f"min_interval_seconds must be >= 0, got {min_interval_seconds}"
            )
        self.min_interval_seconds = float(min_interval_seconds)
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None

    def wait(self) -> None:
        """Block until the configured interval since the previous call has passed."""
        if self.min_interval_seconds <= 0:
            return
        now = self._clock()
        if self._last_call is not None:
            remaining = self.min_interval_seconds - (now - self._last_call)
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last_call = now


def run_batch(
    rows: Iterable[dict[str, str]],
    *,
    mapping: ColumnMapping,
    lookup: PolygonLookup,
    layer_id: str,
    geocoder: Geocoder | None = None,
    rate_limiter: RateLimiter | None = None,
    progress: Callable[[int, str], None] | None = None,
) -> Iterator[RowResult]:
    """Locate every row, yielding one `RowResult` per input row, in order.

    `rows` is consumed lazily, so a large source never lands in memory whole and
    a caller can stream results to a writer as they arrive.

    Whole-run preconditions are checked eagerly, before `rows` is touched: an
    unknown `layer_id`, or a mapping that geocodes with no `geocoder` supplied,
    raises `BatchError` from this call rather than from the first `next()`.
    Everything after that is per-row: a bad cell or a provider failure produces a
    `STATUS_ERROR` result and the run continues.

    `progress`, if given, is called after each row with
    `(rows_done, last_status)` so a CLI can render a counter. This module never
    prints and never logs.
    """
    if layer_id not in lookup.layer_ids:
        raise BatchError(
            f"unknown layer {layer_id!r}; configured: {list(lookup.layer_ids)}"
        )
    if mapping.geocodes and geocoder is None:
        raise BatchError(
            "this column mapping geocodes addresses but no geocoder was supplied"
        )
    return _iterate(
        rows,
        mapping=mapping,
        lookup=lookup,
        layer_id=layer_id,
        geocoder=geocoder,
        rate_limiter=rate_limiter,
        progress=progress,
    )


def _iterate(
    rows: Iterable[dict[str, str]],
    *,
    mapping: ColumnMapping,
    lookup: PolygonLookup,
    layer_id: str,
    geocoder: Geocoder | None,
    rate_limiter: RateLimiter | None,
    progress: Callable[[int, str], None] | None,
) -> Iterator[RowResult]:
    """The generator behind `run_batch`. Split out so the precondition checks
    above run at call time instead of being deferred to the first `next()`."""
    rows_done = 0
    checked_columns = False
    for row in rows:
        if not checked_columns:
            _require_columns(row, mapping)
            checked_columns = True

        if mapping.geocodes:
            result = _locate_by_address(
                row,
                mapping=mapping,
                lookup=lookup,
                layer_id=layer_id,
                geocoder=geocoder,  # type: ignore[arg-type]  # checked in run_batch
                rate_limiter=rate_limiter,
            )
        else:
            result = _locate_by_coordinates(
                row, mapping=mapping, lookup=lookup, layer_id=layer_id
            )

        rows_done += 1
        if progress is not None:
            progress(rows_done, result.status)
        yield result


def _require_columns(row: dict[str, str], mapping: ColumnMapping) -> None:
    """Fail the whole run if the mapped columns are absent from the source.

    A column the operator named that the file simply does not have is a mapping
    mistake, not a data problem: every row would fail identically, so the run is
    stopped on the first row instead of producing a file of uniform errors. A
    column that exists but is *blank in this row* is the opposite case, and stays
    a per-row `STATUS_ERROR`.

    A CLI run has already been through `sources.validate_column_mapping`, which
    checks the same thing against the header row and fails before the file is
    even streamed. This is not that check repeated for its own sake: the engine
    accepts any iterable of dicts (that is what lets F7b reuse it without a
    `Source`), so it cannot assume a caller validated headers it may not have.
    The wording is kept identical to the header-level check so an operator sees
    one message for one mistake, whichever layer catches it.
    """
    missing = [column for column in mapping.required_columns() if column not in row]
    if missing:
        label = "columns" if len(missing) > 1 else "column"
        missing_names = ", ".join(repr(column) for column in missing)
        present = ", ".join(repr(column) for column in sorted(row)) or "(none)"
        raise BatchError(
            f"{label} {missing_names} not found. "
            f"Columns in this source: {present}"
        )


def _locate_by_coordinates(
    row: dict[str, str],
    *,
    mapping: ColumnMapping,
    lookup: PolygonLookup,
    layer_id: str,
) -> RowResult:
    """The offline path: read lon/lat from the row and locate directly.

    No geocoder, no rate limiter, no network. Coordinates are parsed leniently
    (surrounding whitespace is ignored) but never guessed at: anything that is
    not a number becomes a `STATUS_ERROR` row.
    """
    try:
        lat = _coerce_coordinate(row.get(mapping.lat))
        lon = _coerce_coordinate(row.get(mapping.lon))
    except _CoordinateError as error:
        return RowResult(row=row, status=STATUS_ERROR, reason=error.reason)

    return _locate_point(row, lon=lon, lat=lat, lookup=lookup, layer_id=layer_id)


def _locate_by_address(
    row: dict[str, str],
    *,
    mapping: ColumnMapping,
    lookup: PolygonLookup,
    layer_id: str,
    geocoder: Geocoder,
    rate_limiter: RateLimiter | None,
) -> RowResult:
    """The geocoding path: address → point → polygon.

    The rate limiter is consulted immediately before the provider call and only
    then — a blank address costs neither a request nor a slot in the budget.
    """
    address = row.get(mapping.address)
    address = "" if address is None else str(address).strip()
    if not address:
        return RowResult(row=row, status=STATUS_ERROR, reason=REASON_MISSING_ADDRESS)

    if rate_limiter is not None:
        rate_limiter.wait()

    try:
        geocoded = geocoder.geocode(address)
    except GeocoderUnavailable:
        # The provider's own message is deliberately discarded: it is free to
        # quote the address it failed on, and a reason string ends up in the
        # operator's output file (SPEC §9).
        return RowResult(
            row=row,
            status=STATUS_ERROR,
            reason=REASON_GEOCODER_UNAVAILABLE,
            provider=getattr(geocoder, "name", None),
        )
    except Exception as error:  # noqa: BLE001 — one bad row must not end the run
        return RowResult(
            row=row,
            status=STATUS_ERROR,
            reason=_opaque_reason(error),
            provider=getattr(geocoder, "name", None),
        )

    if not geocoded.matched:
        return RowResult(
            row=row,
            status=STATUS_NO_GEOCODE,
            reason=REASON_NO_GEOCODE,
            provider=geocoded.provider,
        )
    if geocoded.point is None:
        return RowResult(
            row=row,
            status=STATUS_ERROR,
            reason=REASON_GEOCODER_NO_POINT,
            provider=geocoded.provider,
        )

    lon, lat = geocoded.point
    return _locate_point(
        row,
        lon=lon,
        lat=lat,
        lookup=lookup,
        layer_id=layer_id,
        matched_address=geocoded.matched_address,
        score=geocoded.score,
        provider=geocoded.provider,
    )


def _locate_point(
    row: dict[str, str],
    *,
    lon: float,
    lat: float,
    lookup: PolygonLookup,
    layer_id: str,
    matched_address: str | None = None,
    score: float | None = None,
    provider: str | None = None,
) -> RowResult:
    """Run one point through the engine and map `Match` onto a `RowResult`.

    Shared by both paths so a geocoded point and a supplied point are judged by
    exactly the same code — the batch answer for a coordinate is identical to the
    one `GET /locate` gives for it.
    """
    geocode_fields = dict(
        matched_address=matched_address, score=score, provider=provider, lon=lon, lat=lat
    )
    try:
        match = lookup.locate(lon, lat, layer_id)
    except Exception as error:  # noqa: BLE001 — bad point, not a broken run
        return RowResult(
            row=row,
            status=STATUS_ERROR,
            reason=_locate_failure_reason(error),
            **geocode_fields,
        )

    if match.found:
        return RowResult(
            row=row, status=STATUS_MATCHED, feature=match.feature, **geocode_fields
        )
    return RowResult(
        row=row, status=STATUS_OUTSIDE, reason=match.reason, **geocode_fields
    )


class _CoordinateError(Exception):
    """A cell that cannot be read as a coordinate. Carries a fixed reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _coerce_coordinate(value) -> float:
    """One coordinate cell → float, or `_CoordinateError` with a fixed reason.

    The offending value is never put in the reason: a coordinate is itself the
    location data this service refuses to persist or echo (SPEC §9).
    """
    text = "" if value is None else str(value).strip()
    if not text:
        raise _CoordinateError(REASON_MISSING_COORDINATE)
    try:
        number = float(text)
    except ValueError:
        raise _CoordinateError(REASON_UNPARSEABLE_COORDINATE) from None
    if number != number:  # NaN: float("nan") parses but is not a coordinate
        raise _CoordinateError(REASON_UNPARSEABLE_COORDINATE)
    return number


def _locate_failure_reason(error: Exception) -> str:
    """A fixed reason for a failed `locate` call — never the exception's text.

    `InvalidCoordinateError` quotes the offending lon/lat in its message, which
    is location data; every other failure could quote anything at all.
    """
    if isinstance(error, InvalidCoordinateError):
        return REASON_COORDINATE_OUT_OF_RANGE
    return _opaque_reason(error)


def _opaque_reason(error: Exception) -> str:
    """Name the failure by its exception type only.

    Enough for an operator to tell a timeout from a parse failure and take it to
    the maintainer, with no chance of the message carrying the row's address.
    """
    return f"unexpected error ({type(error).__name__})"
