#!/usr/bin/env python3
"""F7-T4 — batch point-in-polygon from the command line.

A thin argparse shell over `app.batch`: read a source (CSV, XLSX, or a
link-shared Google Sheet), locate every row, and write one output CSV that
reproduces the operator's original columns and appends the `pip_*` result
columns.

    python scripts/batch_locate.py caseload.csv --out located.csv \\
        --layer police_districts --address-column "Address"

    python scripts/batch_locate.py points.xlsx --out located.csv \\
        --layer police_districts --lat-column lat --lon-column lon

**Input may be an .xlsx; output is always a .csv.** Writing an Excel file means
copying every row through a temporary working file that a power loss or a killed
process can leave behind, and SPEC §9 forbids persisting a queried address
anywhere. An `--out` ending in `.xlsx` is therefore refused — and refused in the
first second, before the source is opened, so nobody discovers it at the end of
a half-hour run. Reading an `.xlsx` source has no such exposure and is untouched.

The lat/lon form never constructs a geocoder, never consults the rate limiter,
and sends nothing anywhere: it runs with the network physically severed.

Three behaviours are worth knowing before a long run:

- **Progress and diagnostics go to stderr, results go to the file.** stdout is
  left clean so the command stays pipeable inside a larger script.
- **Nominatim is refused for batch traffic** (plan D20). The OSM Foundation's
  usage policy caps the shared public instance at one request per second and
  forbids bulk/service traffic outright; a two-thousand-row run is exactly that.
  An operator running their *own* Nominatim may pass `--allow-nominatim`.
- **Exit code is the answer to "did it all work?"** — 0 when every row matched,
  1 when any row did not, 2 when the run could not start (or could not finish)
  at all. A single bad row never aborts the run (plan D19).

No queried address is ever printed, logged, or interpolated into an error
message (SPEC §9). The only place a row's contents appear is the output file the
operator named. Nothing else is written: no temp file, no cache, no state.

ArcGIS / ArcPy equivalent
    This is the whole classic desktop workflow in one command:
    `arcpy.geocoding.geocodeAddresses` (address table + locator → point feature
    class) — or `arcpy.management.XYTableToPoint` when the table already carries
    coordinates — followed by `arcpy.analysis.SpatialJoin` against the boundary
    layer and `arcpy.conversion.TableToTable` to export the result (Esri's
    `TableToExcel` has no counterpart here — see the note on output format
    above). Where the Esri chain materializes two intermediate feature classes in
    a scratch geodatabase and fails the whole tool on a bad record, this streams
    rows through memory, writes exactly one file, and turns a bad record into one
    flagged output row.
"""
from __future__ import annotations

import argparse
import itertools
import sys
from collections import Counter
from pathlib import Path

# Allow `python scripts/batch_locate.py` from a clone that has not been
# pip-installed: the package root is this file's parent's parent.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.batch import (  # noqa: E402 — after the sys.path bootstrap above
    STATUS_ERROR,
    STATUS_MATCHED,
    STATUS_NO_GEOCODE,
    STATUS_OUTSIDE,
    BatchError,
    ColumnMapping,
)
from app.batch.runner import RateLimiter, run_batch  # noqa: E402
from app.batch.sources import (  # noqa: E402
    is_url,
    read_source,
    validate_column_mapping,
)
from app.batch.writer import (  # noqa: E402
    XLSX_OUTPUT_REFUSED,
    XLSX_SUFFIX,
    write_results,
)
from app.config import ConfigError, load_config  # noqa: E402
from app.geocoding.registry import build_geocoders  # noqa: E402
from app.lookup import PolygonLookup  # noqa: E402

# Seconds between geocode calls when the operator names none. One second is the
# ceiling the strictest public provider in this project's config publishes
# (Nominatim's usage policy), so it is the safe default for every provider —
# an operator who knows their own locator tolerates more can lower it.
DEFAULT_RATE_LIMIT_SECONDS = 1.0

# Below this the run is faster than any public provider's published courtesy
# rate. Allowed (a self-hosted locator has no such limit) but called out, so
# nobody points a 0.05s run at someone else's server by accident.
POLITE_RATE_LIMIT_SECONDS = 1.0

# Geocoder config `type` that batch refuses without an explicit override (D20).
NOMINATIM_TYPE = "nominatim"
CHAIN_TYPE = "chain"

# How often the row counter is reprinted to stderr during a run.
PROGRESS_EVERY_ROWS = 50

# Statuses in the order the end-of-run summary lists them.
SUMMARY_STATUSES = (STATUS_MATCHED, STATUS_OUTSIDE, STATUS_NO_GEOCODE, STATUS_ERROR)

# Tally key for rows pulled off the source, kept beside the status counts.
ROWS_READ_KEY = "rows_read"

# Tally key for source records the reader flagged as spanning several lines.
MULTILINE_RECORDS_KEY = "multiline_records"

EXIT_OK = 0
EXIT_ROWS_FAILED = 1
EXIT_RUN_FAILED = 2


def build_parser() -> argparse.ArgumentParser:
    """The command-line interface, built in its own function so a test (or the
    runbook) can render the usage string without running a batch."""
    parser = argparse.ArgumentParser(
        prog="batch_locate.py",
        description=(
            "Locate every row of a CSV, XLSX, or link-shared Google Sheet "
            "against a configured polygon layer."
        ),
    )
    parser.add_argument(
        "source",
        help="input file path, or a Google Sheets URL copied from the browser",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="output file to write. Must be a .csv — this tool does not write "
        ".xlsx (Excel opens .csv files normally). A killed run leaves a valid "
        "partial .csv",
    )
    parser.add_argument(
        "--layer",
        required=True,
        help="id of the configured polygon layer to locate against",
    )
    parser.add_argument(
        "--address-column",
        dest="address_column",
        help="column holding the address to geocode (needs a geocoder)",
    )
    parser.add_argument(
        "--lat-column",
        dest="lat_column",
        help="column holding latitude (WGS84 degrees); use with --lon-column. "
        "This form never geocodes and needs no network",
    )
    parser.add_argument(
        "--lon-column",
        dest="lon_column",
        help="column holding longitude (WGS84 degrees); use with --lat-column",
    )
    parser.add_argument(
        "--provider",
        help="id of the configured geocoder to use (default: the configured "
        "default provider). Ignored for a lat/lon run",
    )
    parser.add_argument(
        "--config",
        help="path to config.toml (default: $PIP_CONFIG, else ./config.toml)",
    )
    parser.add_argument(
        "--rate-limit",
        dest="rate_limit",
        type=float,
        default=DEFAULT_RATE_LIMIT_SECONDS,
        help=f"minimum seconds between geocode calls "
        f"(default: {DEFAULT_RATE_LIMIT_SECONDS})",
    )
    parser.add_argument(
        "--max-rows",
        dest="max_rows",
        type=int,
        help="stop after this many rows (useful for a trial run before "
        "committing to the whole file)",
    )
    parser.add_argument(
        "--sheet-gid",
        dest="sheet_gid",
        help="tab id of a Google Sheet, overriding any gid in the URL",
    )
    parser.add_argument(
        "--allow-nominatim",
        dest="allow_nominatim",
        action="store_true",
        help="permit a nominatim provider for this batch run. ONLY for an "
        "instance you host yourself — the shared public instance's usage "
        "policy forbids batch traffic",
    )
    return parser


def build_column_mapping(args: argparse.Namespace) -> ColumnMapping:
    """The address-or-coordinates mapping the operator asked for.

    Raises `BatchError` (from `ColumnMapping` itself) when neither form or both
    forms are given — the mapping is never guessed from the headers (plan D18).
    """
    return ColumnMapping(
        address=args.address_column, lat=args.lat_column, lon=args.lon_column
    )


def refuse_xlsx_output(out_spec: str) -> None:
    """Refuse an `.xlsx` destination, in plain English, before anything happens.

    The writer refuses it too (`app.batch.writer.resolve_format` is the authority
    and holds the wording), but the writer is not reached until the source has
    been read and every row geocoded. Someone who types `--out results.xlsx`
    would learn about it half an hour later, after a run whose result is then
    thrown away. Checking the name here — it is a string check, needing no file,
    no config and no network — turns that into a first-second answer.
    """
    if Path(out_spec).suffix.lower() == XLSX_SUFFIX:
        raise BatchError(XLSX_OUTPUT_REFUSED)


def print_keep_it_private_note(out_spec: str) -> None:
    """One closing line about what the operator now has on disk.

    The output file holds the addresses that were looked up — that is what it is
    for — and someone who does not think about file permissions should still be
    told, once, in words rather than jargon. Not repeated, not a warning, and it
    names the file only, never a row's contents (SPEC §9).
    """
    print(
        f"NOTE: {out_spec} contains the addresses you looked up. It is created "
        f"so that only your user account can read it — keep it somewhere "
        f"private, and delete it when you no longer need it.",
        file=sys.stderr,
    )


def refuse_overwriting_source(source_spec: str, out_spec: str) -> None:
    """Refuse a run whose `--out` is the source file itself.

    This is not a tidiness rule, it is data loss. The source is read as a *lazy*
    stream while the output is written, and the writer opens `--out` with "w",
    which truncates. Pointing both at one path destroys the operator's input the
    instant the first result row is written — and worse, the reader then reads
    back the rows the writer just appended and locates them again, so three input
    rows become an unbounded file that grows until the disk is full.

    Paths are compared after `resolve()` so a relative path, a symlink, and an
    absolute path naming one file are all caught. A URL source cannot collide
    with a local output and is passed through.
    """
    if is_url(source_spec):
        return
    source_path = Path(source_spec).expanduser()
    out_path = Path(out_spec).expanduser()
    try:
        same_file = source_path.resolve() == out_path.resolve()
    except OSError:  # an unresolvable path is read_source's error to report
        return
    if same_file:
        raise BatchError(
            f"--out {out_spec} is the source file itself; a batch run would "
            f"overwrite the input while still reading it. Write to a new file."
        )


def resolve_provider_id(app_config, requested: str | None) -> str:
    """The geocoder id a run will use, or `BatchError` naming what is configured.

    With no `--provider`, the config's default provider is used — the same one
    `GET /locate` would pick, so a batch run and a single lookup agree.
    """
    if not app_config.geocoders:
        raise BatchError(
            "this column mapping geocodes addresses, but no [[geocoders]] are "
            "configured. Add one to config.toml, or supply --lat-column and "
            "--lon-column to skip geocoding entirely."
        )
    provider_id = requested or app_config.default_geocoder
    if provider_id not in app_config.geocoders:
        raise BatchError(
            f"unknown geocoder {provider_id!r}; configured: "
            f"{sorted(app_config.geocoders)}"
        )
    return provider_id


def nominatim_provider_ids(app_config, provider_id: str) -> list[str]:
    """Every nominatim-typed provider a run through `provider_id` could reach.

    A chain is followed into its members: `--provider default` must be refused
    just as firmly as `--provider nominatim` if the chain would fall through to
    Nominatim on a timeout. The `seen` set makes a config that (wrongly) chains
    back onto itself terminate instead of recursing forever.
    """
    found: list[str] = []
    seen: set[str] = set()

    def walk(current_id: str) -> None:
        if current_id in seen:
            return
        seen.add(current_id)
        entry = app_config.geocoders.get(current_id)
        if entry is None:
            return
        if entry.type == NOMINATIM_TYPE:
            found.append(current_id)
            return
        if entry.type == CHAIN_TYPE:
            for member_id in entry.options.get("providers", []):
                walk(str(member_id))

    walk(provider_id)
    return found


def refuse_nominatim(app_config, provider_id: str, allow: bool) -> None:
    """Stop a batch run that would send bulk traffic to Nominatim (plan D20).

    The OSM Foundation's Nominatim usage policy caps the shared public instance
    at one request per second and rules out bulk and service-rate use of it
    altogether; a batch of a few thousand addresses is precisely the traffic the
    policy exists to prevent. This is not a rate-limit question — throttling to
    1 req/s does not make the run permitted — so the refusal is unconditional
    unless the operator asserts, with `--allow-nominatim`, that the instance is
    their own.
    """
    if allow:
        return
    offenders = nominatim_provider_ids(app_config, provider_id)
    if not offenders:
        return

    via = (
        f"provider {provider_id!r}"
        if offenders == [provider_id]
        else f"provider {provider_id!r} (via chain member(s) "
        f"{', '.join(repr(o) for o in offenders)})"
    )
    raise BatchError(
        f"refusing to run a batch through {via}: the OpenStreetMap Foundation's "
        "Nominatim usage policy forbids bulk and service-rate traffic against "
        "the shared public instance "
        "(https://operations.osmfoundation.org/policies/nominatim/), and "
        "throttling does not make it permitted. Choose another --provider, or "
        "pass --allow-nominatim if the instance is one you host yourself."
    )


def select_geocoder(app_config, provider_id: str):
    """Build the configured providers and hand back the selected one."""
    geocoders = build_geocoders(app_config)
    try:
        return geocoders[provider_id]
    except KeyError:  # pragma: no cover — resolve_provider_id checked this
        raise BatchError(
            f"geocoder {provider_id!r} is configured but could not be built"
        ) from None


def format_duration(seconds: float) -> str:
    """A rough human duration: seconds under a minute, then minutes, then hours."""
    if seconds < 60:
        return f"{seconds:.0f} sec"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} hr"


def describe_eta(row_count: int | None, rate_limit: float) -> str:
    """What a geocoding run is about to cost, in wall-clock time.

    `row_count` is `None` for the usual case: the source is a single-pass stream,
    and counting it up front would mean reading the file (or re-fetching the
    Sheet) twice. With `--max-rows` the count is a known ceiling, so the estimate
    is exact; otherwise the rate is quoted per thousand rows and the operator
    does the multiplication against a file whose size they know.
    """
    per_row = max(rate_limit, 0.0)
    tail = "Ctrl-C is safe; partial output is kept."
    if row_count is not None:
        total = format_duration(row_count * per_row)
        return (
            f"{row_count:,} rows at {per_row:g} s between calls = about "
            f"{total}. {tail}"
        )
    if per_row <= 0:
        return f"no rate limit set; the run is as fast as the provider. {tail}"
    per_thousand = format_duration(1000 * per_row)
    return (
        f"{per_row:g} s between calls = about {per_thousand} per 1,000 rows "
        f"(row count is not known until the source is read). {tail}"
    )


def describe_exception(error: BaseException) -> str:
    """Name an unexpected exception without quoting anything it was handed.

    Returns the exception's type and the file, line, and function that raised
    it — `MemoryError raised at app/batch/sources.py:212 in read_xlsx_rows` —
    which is what a bug report needs. The exception's own message is
    deliberately omitted: a third-party library is free to interpolate the
    value it choked on into it, and in this program that value can be a
    queried address (SPEC §9).
    """
    name = type(error).__name__
    traceback_entry = error.__traceback__
    origin = None
    while traceback_entry is not None:  # walk to the innermost frame
        origin = traceback_entry
        traceback_entry = traceback_entry.tb_next
    if origin is None:  # pragma: no cover — a raised exception always has one
        return name
    frame = origin.tb_frame
    return (
        f"{name} raised at {Path(frame.f_code.co_filename).name}:"
        f"{origin.tb_lineno} in {frame.f_code.co_name}"
    )


def count_rows(rows, tally: Counter, key: str = ROWS_READ_KEY):
    """Yield rows unchanged, tallying them as they leave the source.

    The point is the *comparison*: a run reports what it read as well as what it
    wrote, and the operator compares both against the row count of their own
    spreadsheet. A source row that never reached the run — the CSV reader's
    unclosed-quote case, where an unterminated field swallows the lines after
    it — shows up nowhere else, because every counter downstream only ever sees
    the rows that survived the parse.
    """
    for row in rows:
        tally[key] += 1
        yield row


def print_summary(
    counts: Counter,
    written: int,
    out_path: str,
    *,
    rows_read: int | None = None,
    source_name: str = "the source",
    multiline_records: int = 0,
) -> None:
    """The end-of-run tally, to stderr. Counts only — never a row's contents."""
    if rows_read is not None:
        print(f"\nread  {rows_read:,} data rows from {source_name}", file=sys.stderr)
        print(f"wrote {written:,} rows to {out_path}", file=sys.stderr)
    else:  # pragma: no cover — main() always knows the read count
        print(f"\nwrote {written:,} rows to {out_path}", file=sys.stderr)
    statuses = list(SUMMARY_STATUSES) + sorted(
        status for status in counts if status not in SUMMARY_STATUSES
    )
    for status in statuses:
        print(f"  {status:<22} {counts.get(status, 0):>8,}", file=sys.stderr)

    if multiline_records:
        # Not an error and deliberately not an exit code: a spreadsheet with a
        # genuine multi-line notes column produces this on every clean run. It is
        # a prompt to compare two numbers the operator can see and this program
        # cannot — how many data rows their file has, and how many were read.
        rows_label = "row" if multiline_records == 1 else "rows"
        print(
            f"NOTE: {multiline_records:,} {rows_label} contained a cell spanning "
            f"multiple lines (warned above). Compare the number of data rows in "
            f"your source against the read/wrote counts here: if they differ, an "
            f"unclosed \" quote merged rows and some are missing from this run.",
            file=sys.stderr,
        )


def main(argv=None) -> int:
    """Run one batch. Returns the process exit code; never raises for a run
    failure and never prints a traceback.

    0 — every row matched a polygon.
    1 — the run finished but at least one row did not match (plan D19), or the
        operator interrupted it (the partial output file is kept).
    2 — the run could not be started or completed: bad config, unreadable
        source, unusable column mapping, a refused provider.
    """
    args = build_parser().parse_args(argv)

    # Rows read off the source, and records the reader flagged as spanning
    # several physical lines. Declared before the try so the summary can still
    # quote them if the run ends early.
    source_tally: Counter = Counter()

    def report_source_warning(message: str) -> None:
        """Print one reader warning to stderr and remember that it fired.

        `app.batch.sources` prints nothing itself; it hands the text here. To
        stderr, never stdout, which stays clean and pipeable. The reader's
        messages carry a file name, a line number and a count — never a cell's
        contents (SPEC §9).
        """
        source_tally[MULTILINE_RECORDS_KEY] += 1
        print(f"WARNING: {message}", file=sys.stderr)

    try:
        # First, before the config is loaded and long before the source is read
        # or a single address leaves the machine: the one refusal that costs
        # nothing to check must not wait behind the ones that do.
        refuse_xlsx_output(args.out)

        mapping = build_column_mapping(args)
        app_config = load_config(Path(args.config) if args.config else None)

        if args.layer not in app_config.layers:
            raise BatchError(
                f"unknown layer {args.layer!r}; configured: "
                f"{sorted(app_config.layers)}"
            )
        if args.rate_limit < 0:
            raise BatchError(
                f"--rate-limit must be >= 0, got {args.rate_limit}"
            )
        if args.max_rows is not None and args.max_rows < 1:
            raise BatchError(f"--max-rows must be >= 1, got {args.max_rows}")
        refuse_overwriting_source(args.source, args.out)

        # Everything that can be refused is refused before the source is opened
        # or fetched, and long before a single address leaves the machine.
        geocoder = None
        rate_limiter = None
        if mapping.geocodes:
            provider_id = resolve_provider_id(app_config, args.provider)
            refuse_nominatim(app_config, provider_id, args.allow_nominatim)
            geocoder = select_geocoder(app_config, provider_id)
            rate_limiter = RateLimiter(args.rate_limit)

        lookup = PolygonLookup(app_config)
        source = read_source(
            args.source,
            sheet_gid=args.sheet_gid,
            on_warning=report_source_warning,
        )
        validate_column_mapping(source, mapping)

        rows = count_rows(source.rows, source_tally)
        if args.max_rows is not None:
            rows = itertools.islice(rows, args.max_rows)

        if mapping.geocodes:
            print(f"provider: {provider_id}", file=sys.stderr)
            if args.rate_limit < POLITE_RATE_LIMIT_SECONDS:
                print(
                    f"NOTE: --rate-limit {args.rate_limit:g} is faster than the "
                    f"{POLITE_RATE_LIMIT_SECONDS:g} s/request courtesy rate public "
                    f"providers publish. Use it only against a locator you host.",
                    file=sys.stderr,
                )
            print(describe_eta(args.max_rows, args.rate_limit), file=sys.stderr)

        counts: Counter = Counter()

        def progress(rows_done: int, status: str) -> None:
            counts[status] += 1
            if rows_done % PROGRESS_EVERY_ROWS == 0:
                print(
                    f"  {rows_done:,} rows processed "
                    f"({counts[STATUS_MATCHED]:,} matched)",
                    file=sys.stderr,
                )

        results = run_batch(
            rows,
            mapping=mapping,
            lookup=lookup,
            layer_id=args.layer,
            geocoder=geocoder,
            rate_limiter=rate_limiter,
            progress=progress,
        )
        written = write_results(
            results,
            path=args.out,
            headers=source.headers,
            layer_attributes=app_config.layers[args.layer].attributes,
        )
    except KeyboardInterrupt:
        print(
            f"\ninterrupted; the rows completed so far are in {args.out}",
            file=sys.stderr,
        )
        return EXIT_ROWS_FAILED
    except (BatchError, ConfigError) as error:
        print(f"\nbatch failed: {error}", file=sys.stderr)
        return EXIT_RUN_FAILED
    except OSError as error:
        # The last line of the no-traceback contract. `app.batch` turns the I/O
        # failures it can name into BatchError; this catches the ones it cannot
        # (a disk filling up mid-write, a removable volume disappearing). The
        # message names the errno and the file, never a row's contents.
        print(f"\nbatch failed: I/O error: {error}", file=sys.stderr)
        return EXIT_RUN_FAILED
    except Exception as error:  # noqa: BLE001 — this IS the contract
        # The contract in this function's docstring is "never a traceback, exit
        # 2 when the run cannot finish". The three handlers above name only the
        # failures this project raises itself; anything a dependency raises —
        # a `csv.Error` from a malformed quote, a `MemoryError` from a hostile
        # .xlsx, a `struct.error` from a corrupt zip member — would otherwise
        # escape, print a traceback, and exit 1, which is the code that means
        # "the run finished, some rows did not match". An operator's script
        # cannot tell those apart, so an unhandled exception must land here.
        #
        # Only the exception's *type* and origin are printed, never its message:
        # the messages above are this project's own and are PII-free by
        # construction, but a third-party exception is free to interpolate the
        # value it choked on, and that value may be an address (SPEC §9). Type
        # plus the file and line that raised is enough to diagnose from, and
        # cannot carry a row's contents.
        print(
            f"\nbatch failed: unexpected {describe_exception(error)}. "
            f"The output file may be incomplete. This is a bug: please report "
            f"it with this line and the command you ran (not the input data).",
            file=sys.stderr,
        )
        return EXIT_RUN_FAILED

    print_summary(
        counts,
        written,
        args.out,
        rows_read=source_tally[ROWS_READ_KEY],
        source_name=source.name,
        multiline_records=source_tally[MULTILINE_RECORDS_KEY],
    )
    print_keep_it_private_note(args.out)
    unmatched = sum(counts.values()) - counts.get(STATUS_MATCHED, 0)
    if unmatched:
        print(
            f"{unmatched:,} row(s) did not match; see the pip_status and "
            f"pip_reason columns.",
            file=sys.stderr,
        )
        return EXIT_ROWS_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
