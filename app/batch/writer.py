"""F7-T3 — batch results out: write the CSV the operator can open.

**Output is CSV, and only CSV.** Writing an `.xlsx` meant handing every row to
openpyxl, which copies them — addresses included — into a temporary working file
under `$TMPDIR` while it assembles the workbook. openpyxl deletes that copy when
the workbook is saved, and again through an `atexit` hook if the run raises, but
neither cleanup runs if the process is killed outright or the machine loses
power, and the copy is then left on disk. SPEC §9 makes "no persistence of
queried addresses or PII" non-negotiable, so the write path is gone rather than
hedged: the only artifact of a run is the one CSV the operator named. Reading an
`.xlsx` **source** has no such exposure and stays fully supported
(`app.batch.sources`) — this restriction is about writing only.

`write_results` consumes the iterator of `RowResult`s the runner yields and
writes one output file. Two rules govern the shape of that file:

- **The operator's data is never touched.** Every original column is reproduced
  first, in the source's original header order, and only then are the `pip_`
  result columns appended (`result_columns` in this package defines them). A
  batch run is additive; it never drops, renames, or reorders a column someone
  built a workflow around.
- **Every cell is neutralized against formula injection** (plan D21). A cell
  whose text begins with ``=``, ``+``, ``-``, ``@``, TAB, or CR is executed as a
  formula when the file is opened in Excel or Sheets. `pip_matched_address`
  carries text straight from a third-party geocoder, so this is a live injection
  path, not a theoretical one — but the escaping is applied to *every* cell,
  including the operator's own passthrough columns and the header row itself,
  because their source file may itself have come from somewhere untrusted and a
  column *name* is as attacker-controlled as a value. It is applied at the write
  boundary (`_safe_cells`), the one place every cell must pass through, so no
  future caller can route around it.

Rows are written **as they are consumed**, not buffered and flushed at the end
(plan R15). A 2,000-row run killed at row 1,400 leaves a valid 1,400-row CSV,
which is what makes `--skip-rows` a sufficient answer to "can I resume?" without
persisting any state.

The output file is created readable and writable by its owner only (mode 0600),
because it holds the operator's addresses by construction and the usual default
would let every other account on a shared machine read it.

This module writes no cache and logs no line containing a row's contents (SPEC
§9); errors are raised without quoting cell values. It writes the one file the
caller named and nothing else — no temp file, no sibling, no staging copy.

ArcGIS / ArcPy equivalent
    This is `arcpy.conversion.TableToTable` / `ExportTable` writing the joined
    geocode + Spatial Join result out to a `.csv` — the final export step of the
    classic geocode-then-spatial-join workflow. `arcpy.conversion.TableToExcel`
    is the `.xlsx` counterpart and has deliberately **no** equivalent here, for
    the reason given at the top of this docstring. The formula-injection escaping
    likewise has no Esri equivalent; Esri's exporters do not do it, which is
    precisely why it is done here.
"""
from __future__ import annotations

import csv
import os
import re
from collections.abc import Iterable
from pathlib import Path

from app.batch import BatchError, RowResult, result_columns

# Output formats, keyed by the path suffix that selects them. CSV is the only
# one, on purpose — see the module docstring.
FORMAT_CSV = "csv"
SUPPORTED_FORMATS = (FORMAT_CSV,)

_SUFFIX_FORMATS = {".csv": FORMAT_CSV}

# Suffix (and explicit format name) that gets its own refusal rather than the
# generic "unsupported format" one, because ".xlsx out" is a reasonable thing to
# have expected and the person deserves to know why it is not offered.
XLSX_SUFFIX = ".xlsx"
XLSX_FORMAT_NAME = "xlsx"

# The whole of what a person sees when they ask for an .xlsx output. Written for
# someone who does not know what a temp file is: no "SIGKILL", no "atexit", no
# "staging", and it ends with the thing to type instead. Defined here, and used
# by the CLI too, so there is exactly one wording to keep true.
XLSX_OUTPUT_REFUSED = (
    "cannot write .xlsx: this tool writes .csv only. Making an Excel file means "
    "copying your addresses into a temporary working file first, and that copy "
    "can be left behind on your computer if it loses power or the program is "
    "stopped. Use --out <name>.csv instead — Excel opens .csv files normally."
)

# Permission bits the output file is created with: owner read/write, nobody
# else. The file holds the operator's addresses, and a shared machine's default
# would otherwise usually make it world-readable.
OWNER_ONLY_MODE = 0o600

# Leading characters that make a spreadsheet cell a formula rather than text
# (plan D21). TAB and CR are included because Excel strips them and then
# evaluates what is left.
FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")

# Prefixed to a triggering cell. A single quote is the spreadsheet convention
# for "treat the rest of this as literal text"; it is not displayed by Excel.
FORMULA_ESCAPE = "'"

# A cell whose *entire* text is a plain decimal number is a numeric literal, not
# a formula, in every spreadsheet — no leading character can make `-87.63192`
# execute anything. Excluding these from the escape is what keeps `pip_lon` (a
# negative number for every address in Chicago, so it trips the "-" trigger on
# every single row) usable as a number downstream instead of arriving as
# `'-87.63192` text. The exclusion is deliberately strict: `-1+1` and `+cmd`
# do not match it and are still escaped.
_PLAIN_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")


def neutralize_cell(text: str) -> str:
    """Return `text` made safe to open in a spreadsheet (plan D21).

    A cell beginning with one of `FORMULA_TRIGGERS` is prefixed with a single
    quote so the spreadsheet treats it as literal text instead of a formula.
    Text that does not begin with a trigger is returned unchanged — this is not
    a general-purpose sanitizer and must not mangle ordinary data. The one
    documented carve-out is a cell that is entirely a plain number
    (`_PLAIN_NUMBER`), which cannot be a formula in the first place.
    """
    if text.startswith(FORMULA_TRIGGERS) and not _PLAIN_NUMBER.match(text):
        return FORMULA_ESCAPE + text
    return text


def _render(value) -> str:
    """Render one result value as an output-ready, injection-safe string.

    Everything is written as text (plan R14): coercing to a number is how
    leading zeros in ZIP codes and house numbers get destroyed, and the loss is
    unrecoverable downstream. `None` becomes an empty cell rather than the
    string "None".
    """
    if value is None:
        return ""
    return neutralize_cell(value if isinstance(value, str) else str(value))


def _safe_cells(values: Iterable) -> list[str]:
    """Render a whole record at the **write boundary** — the only way out.

    Every row this module emits, header row included, passes through here on its
    way to `csv.writer.writerow`, so no cell can reach a
    file without `neutralize_cell` (plan D21). Escaping used to live one layer
    up, in row rendering, which left the header row — verbatim column names from
    the operator's input file, and therefore just as untrusted as its data — to
    be written raw. A header named `=cmd|'/c calc'!A0` became a live formula in
    cell A1. Escaping belongs at the boundary precisely so that "did I remember
    to escape this one?" is not a question any future caller has to answer.
    """
    return [_render(value) for value in values]


def resolve_format(path: Path, fmt: str | None = None) -> str:
    """The output format to use for `path`, explicit `fmt` winning if given.

    Raises `BatchError` for an unsupported explicit format or an unrecognized
    suffix, rather than guessing: silently writing CSV bytes to a name ending
    `.xls` produces a file the operator's spreadsheet refuses to open, and the
    error surfaces long after the run that could have been fixed.

    An `.xlsx` destination gets its own plain-English refusal
    (`XLSX_OUTPUT_REFUSED`). It is refused by *destination name*, not only by
    requested format: `fmt="csv"` with a `results.xlsx` path would put CSV bytes
    behind a name Excel then refuses to open, which is the same trap in a
    different coat.
    """
    if (
        path.suffix.lower() == XLSX_SUFFIX
        or (fmt is not None and fmt.strip().lower().lstrip(".") == XLSX_FORMAT_NAME)
    ):
        raise BatchError(XLSX_OUTPUT_REFUSED)

    if fmt is not None:
        normalized = fmt.strip().lower().lstrip(".")
        if normalized not in SUPPORTED_FORMATS:
            raise BatchError(
                f"unsupported output format {fmt!r}; "
                f"supported: {', '.join(SUPPORTED_FORMATS)}"
            )
        return normalized

    suffix = path.suffix.lower()
    try:
        return _SUFFIX_FORMATS[suffix]
    except KeyError:
        raise BatchError(
            f"cannot infer output format from {path.name!r}; "
            f"use a .csv suffix, or pass an explicit format"
        ) from None


def _output_row(
    result: RowResult,
    headers: tuple[str, ...],
    layer_attributes: tuple[str, ...],
) -> list:
    """One output record: original columns in original order, then results.

    A header missing from the row yields an empty cell (a ragged source row is
    not worth aborting a 2,000-row run over), and any key the row carries that
    is not in `headers` is ignored — `headers` is the authority on shape.

    The values are returned raw; rendering and injection-escaping happen at the
    write boundary (`_safe_cells`) so that this function cannot be the place
    where a cell escapes unescaped.
    """
    feature = result.feature or {}
    values = [result.row.get(header) for header in headers]
    values += [
        result.status,
        result.reason,
        result.matched_address,
        result.score,
        result.provider,
        result.lon,
        result.lat,
    ]
    values += [feature.get(attr) for attr in layer_attributes]
    return values


def _refuse_colliding_columns(
    header_columns: tuple[str, ...],
    generated_columns: tuple[str, ...],
) -> None:
    """Refuse a run whose source already carries one of our `pip_` columns.

    The `pip_` prefix keeps us clear of an operator's own `status` column, but
    the one file that reliably *does* have a `pip_status` is this feature's own
    output — and re-running a located file against a second layer (wards *and*
    districts on one sheet) is the obvious thing to do with it. Concatenating
    blindly emits `pip_status` twice: `csv.DictReader` silently keeps one,
    pandas renames the other, and `sources` refuses the file outright, so the
    pipeline cannot read back what it just wrote.

    This fails fast instead of renaming. A generated `pip_status_1` would put
    wrong-but-plausible values under a name the operator may already have a
    script reading, and a silent rename is exactly the kind of thing nobody
    notices until the numbers are already in a report.
    """
    collisions = sorted(set(header_columns) & set(generated_columns))
    if collisions:
        raise BatchError(
            "duplicate column name(s) "
            f"{', '.join(repr(name) for name in collisions)}; the source file "
            f"already has the result column(s) this run would add, and column "
            f"names must be unique so a mapping is unambiguous. Write to a new "
            f"output file, or remove the old pip_ columns from the source first."
        )


def write_results(
    results: Iterable[RowResult],
    *,
    path: str | Path,
    headers: Iterable[str],
    layer_attributes: Iterable[str] = (),
    fmt: str | None = None,
) -> int:
    """Write `results` to `path`, returning the number of data rows written.

    `headers` is the source's original column order (`Source.headers`);
    `layer_attributes` is the configured layer's attribute list, which decides
    the trailing `pip_*` columns via `result_columns`. `fmt` overrides the
    format otherwise inferred from the path suffix.

    The iterator is consumed lazily and each row is flushed as it is written, so
    an interrupted run leaves a valid partial CSV (plan R15). An exception
    raised by `results` propagates to the caller with the file closed and the
    rows written so far intact.
    """
    output_path = Path(path)
    header_columns = tuple(headers)
    attributes = tuple(layer_attributes)
    resolve_format(output_path, fmt)  # refuses anything that is not a CSV
    generated_columns = result_columns(attributes)
    _refuse_colliding_columns(header_columns, generated_columns)
    header_row = header_columns + generated_columns

    return _write_csv(results, output_path, header_row, header_columns, attributes)


def _open_output(path: Path):
    """Open the output file for writing, or raise `BatchError` explaining why not.

    The caller contract for this module is `BatchError` (the CLI catches it and
    exits 2 without a traceback), so an `OSError` must never escape. A missing
    parent directory and a read-only destination are ordinary operator typos, not
    bugs, and they deserve the same one-line message every other refusal gets.
    The directory is *not* created for them: this feature writes exactly one file,
    the one that was named, and nothing else (SPEC §9).

    The file is created readable and writable **only by the user running the
    command** (mode 0600): it holds their addresses, and on a shared machine the
    usual default would let every other account read it. The mode is passed to
    `os.open` at creation rather than applied with `chmod` afterwards, because a
    chmod leaves a window — however short — in which the addresses are already on
    disk under the permissive default. If the destination already exists its mode
    is left as it is (`O_CREAT` only sets the mode on creation); that is the
    operator's own file and silently re-permissioning it would be a surprise.
    Windows does not enforce POSIX modes and ignores the value without
    complaining, so this is a no-op there rather than an error.
    """
    try:
        descriptor = os.open(
            path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, OWNER_ONLY_MODE
        )
    except OSError as error:
        raise BatchError(
            f"could not open {path} for writing: "
            f"{error.strerror or type(error).__name__}"
        ) from error
    try:
        return os.fdopen(descriptor, "w", newline="", encoding="utf-8-sig")
    except OSError as error:  # pragma: no cover — wrapping a live fd rarely fails
        os.close(descriptor)
        raise BatchError(
            f"could not open {path} for writing: "
            f"{error.strerror or type(error).__name__}"
        ) from error


def _write_csv(
    results: Iterable[RowResult],
    path: Path,
    header_row: tuple[str, ...],
    header_columns: tuple[str, ...],
    attributes: tuple[str, ...],
) -> int:
    """Stream results to a CSV, flushing every row so a kill leaves valid data.

    `newline=""` is required by the `csv` module (it manages its own line
    endings); `utf-8-sig` writes the BOM Excel needs to read non-ASCII street
    names correctly on Windows.
    """
    written = 0
    handle = _open_output(path)
    with handle:
        writer = csv.writer(handle)
        writer.writerow(_safe_cells(header_row))
        handle.flush()
        for result in results:
            writer.writerow(
                _safe_cells(_output_row(result, header_columns, attributes))
            )
            handle.flush()
            written += 1
    return written
