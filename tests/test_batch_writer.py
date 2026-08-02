"""F7-T3 — tests for the batch result writer.

Everything runs against tmp_path files built in-test: no network, no fixture
data on disk, no geocoder. The tests that matter most here are the two that
protect people rather than data shape — the formula-injection test (plan D21)
and the partial-write test (plan R15).
"""
import csv
import os
import stat

import pytest

from app.batch import (
    STATUS_ERROR,
    STATUS_MATCHED,
    STATUS_NO_GEOCODE,
    STATUS_OUTSIDE,
    BatchError,
    RowResult,
    result_columns,
)
from app.batch.writer import neutralize_cell, resolve_format, write_results

# An operator's file, in an order nothing may disturb: the id column is last on
# purpose, and "score" collides in spirit with pip_score.
HEADERS = ("address", "score", "case_id")
ATTRIBUTES = ("dist_num", "dist_label")

# The classic DDE payload. If this survives to a cell unescaped, opening the
# output in Excel prompts to launch a program.
INJECTION = "=cmd|'/c calc'!A1"


def _matched(address="121 N La Salle St", **overrides) -> RowResult:
    fields = dict(
        row={"address": address, "score": "manual-7", "case_id": "C-001"},
        status=STATUS_MATCHED,
        matched_address="121 N LA SALLE ST, CHICAGO 60602",
        score=100.0,
        provider="local_offline",
        lon=-87.63192,
        lat=41.88354,
        feature={"dist_num": "17", "dist_label": "Albany Park"},
    )
    fields.update(overrides)
    return RowResult(**fields)


def _read_csv(path) -> tuple[list[str], list[list[str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    return rows[0], rows[1:]


# Words that would land in front of someone who does not work in IT and tell
# them nothing. A refusal they cannot act on is a refusal that gets worked
# around, so the wording is pinned by test, not by good intentions.
JARGON = ("SIGKILL", "atexit", "staging", "stage", "$TMPDIR", "openpyxl", "TMPDIR")


def assert_no_jargon(message: str) -> None:
    lowered = message.lower()
    for word in JARGON:
        assert word.lower() not in lowered, f"jargon {word!r} in: {message}"


# ---- format resolution ----

def test_format_inferred_from_suffix(tmp_path):
    assert resolve_format(tmp_path / "out.csv") == "csv"
    assert resolve_format(tmp_path / "OUT.CSV") == "csv"


def test_explicit_format_names_csv(tmp_path):
    assert resolve_format(tmp_path / "out.dat", ".CSV") == "csv"


def test_xlsx_destination_is_refused_in_plain_english(tmp_path):
    # Output is CSV only: building an Excel file copies every row — addresses
    # included — through a temporary working file that a power loss can leave on
    # disk, and SPEC §9 forbids that. The refusal has to be readable by someone
    # who has never heard of a temp file.
    with pytest.raises(BatchError) as raised:
        resolve_format(tmp_path / "results.xlsx")

    message = str(raised.value)
    assert "this tool writes .csv only" in message
    # It says what to type instead.
    assert "--out <name>.csv" in message
    assert_no_jargon(message)


def test_xlsx_is_refused_as_an_explicit_format_too(tmp_path):
    with pytest.raises(BatchError, match="writes .csv only"):
        resolve_format(tmp_path / "out.dat", "xlsx")


def test_xlsx_destination_is_refused_even_when_csv_format_is_forced(tmp_path):
    # The refusal is on the destination NAME, not just the requested format:
    # CSV bytes behind a .xlsx name is the same trap wearing a different coat.
    with pytest.raises(BatchError, match="this tool writes .csv only"):
        resolve_format(tmp_path / "out.xlsx", "csv")


def test_write_results_refuses_an_xlsx_path_and_creates_nothing(tmp_path):
    target = tmp_path / "out.xlsx"
    with pytest.raises(BatchError, match="this tool writes .csv only"):
        write_results([_matched()], path=target, headers=HEADERS)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_unknown_suffix_raises_batch_error(tmp_path):
    with pytest.raises(BatchError, match="cannot infer output format"):
        resolve_format(tmp_path / "results.xls")


def test_unsupported_explicit_format_raises_batch_error(tmp_path):
    with pytest.raises(BatchError, match="unsupported output format"):
        resolve_format(tmp_path / "out.csv", "parquet")


def test_write_results_rejects_unknown_suffix(tmp_path):
    target = tmp_path / "results.txt"
    with pytest.raises(BatchError):
        write_results([_matched()], path=target, headers=HEADERS)
    # Nothing is created for a run that can never succeed.
    assert not target.exists()


# ---- CSV round trip ----

def test_csv_round_trip_preserves_original_columns_then_results(tmp_path):
    target = tmp_path / "out.csv"
    count = write_results(
        [_matched()], path=target, headers=HEADERS, layer_attributes=ATTRIBUTES
    )
    assert count == 1

    header, rows = _read_csv(target)
    assert tuple(header) == HEADERS + result_columns(ATTRIBUTES)
    # The operator's own columns come first, in their original order, unaltered.
    assert header[: len(HEADERS)] == list(HEADERS)
    assert rows[0][: len(HEADERS)] == ["121 N La Salle St", "manual-7", "C-001"]
    # Their "score" column is untouched; ours is the prefixed one.
    assert rows[0][header.index("score")] == "manual-7"
    assert rows[0][header.index("pip_score")] == "100.0"
    assert rows[0][header.index("pip_status")] == STATUS_MATCHED
    assert rows[0][header.index("pip_matched_address")] == (
        "121 N LA SALLE ST, CHICAGO 60602"
    )
    assert rows[0][header.index("pip_provider")] == "local_offline"
    assert rows[0][header.index("pip_lon")] == "-87.63192"
    assert rows[0][header.index("pip_lat")] == "41.88354"
    assert rows[0][header.index("pip_dist_num")] == "17"
    assert rows[0][header.index("pip_dist_label")] == "Albany Park"


def test_csv_returns_row_count_and_writes_every_row(tmp_path):
    target = tmp_path / "out.csv"
    results = [_matched(address=f"{n} Main St") for n in range(5)]
    count = write_results(
        results, path=target, headers=HEADERS, layer_attributes=ATTRIBUTES
    )
    assert count == 5
    _, rows = _read_csv(target)
    assert len(rows) == 5
    assert [row[0] for row in rows] == [f"{n} Main St" for n in range(5)]


def test_empty_run_writes_header_only_and_returns_zero(tmp_path):
    target = tmp_path / "out.csv"
    assert write_results(iter([]), path=target, headers=HEADERS) == 0
    header, rows = _read_csv(target)
    assert tuple(header) == HEADERS + result_columns(())
    assert rows == []


def test_none_result_fields_become_empty_cells_not_the_string_none(tmp_path):
    target = tmp_path / "out.csv"
    write_results(
        [
            RowResult(
                row={"address": "nowhere", "score": "", "case_id": "C-002"},
                status=STATUS_NO_GEOCODE,
                reason="no_candidates",
            )
        ],
        path=target,
        headers=HEADERS,
        layer_attributes=ATTRIBUTES,
    )
    header, rows = _read_csv(target)
    assert rows[0][header.index("pip_status")] == STATUS_NO_GEOCODE
    assert rows[0][header.index("pip_reason")] == "no_candidates"
    for column in ("pip_matched_address", "pip_score", "pip_lon", "pip_dist_num"):
        assert rows[0][header.index(column)] == ""


def test_missing_and_extra_row_keys_follow_the_header_contract(tmp_path):
    # A ragged row must not abort the run or shift columns; a stray key the
    # header does not declare must not sneak an extra column into the output.
    target = tmp_path / "out.csv"
    write_results(
        [
            RowResult(
                row={"address": "1 Main St", "surprise": "should not appear"},
                status=STATUS_OUTSIDE,
                reason="point_outside_all_polygons",
                lon=-87.6,
                lat=41.9,
            )
        ],
        path=target,
        headers=HEADERS,
        layer_attributes=ATTRIBUTES,
    )
    header, rows = _read_csv(target)
    assert len(rows[0]) == len(header)
    assert "should not appear" not in rows[0]
    assert rows[0][: len(HEADERS)] == ["1 Main St", "", ""]
    assert rows[0][header.index("pip_status")] == STATUS_OUTSIDE


def test_missing_layer_attribute_yields_empty_cell(tmp_path):
    target = tmp_path / "out.csv"
    write_results(
        [_matched(feature={"dist_num": "17"})],
        path=target,
        headers=HEADERS,
        layer_attributes=ATTRIBUTES,
    )
    header, rows = _read_csv(target)
    assert rows[0][header.index("pip_dist_num")] == "17"
    assert rows[0][header.index("pip_dist_label")] == ""


# ---- formula injection (plan D21) ----

def test_injection_neutralized_in_matched_address_and_operator_column(tmp_path):
    target = tmp_path / "out.csv"
    write_results(
        [
            RowResult(
                # The operator's own column can carry a payload too — their file
                # may have come from an untrusted upload.
                row={"address": INJECTION, "score": "1", "case_id": "C-003"},
                status=STATUS_MATCHED,
                # ...and this one comes straight from a third-party geocoder.
                matched_address=INJECTION,
                score=99.0,
                provider="arcgis_rest",
                lon=-87.6,
                lat=41.9,
                feature={"dist_num": "17", "dist_label": "Albany Park"},
            )
        ],
        path=target,
        headers=HEADERS,
        layer_attributes=ATTRIBUTES,
    )
    header, rows = _read_csv(target)

    matched_cell = rows[0][header.index("pip_matched_address")]
    operator_cell = rows[0][header.index("address")]
    # BOTH cells are neutralized, and the payload itself is preserved verbatim
    # after the escape so the operator can still see what came back.
    assert matched_cell == "'" + INJECTION
    assert operator_cell == "'" + INJECTION
    assert not matched_cell.startswith("=")
    assert not operator_cell.startswith("=")


@pytest.mark.parametrize(
    "payload", ["=1+1", "+SUM(A1)", "-1+1", "@SUM(A1)", "\tcalc", "\rcalc"]
)
def test_every_trigger_character_is_escaped(tmp_path, payload):
    target = tmp_path / "out.csv"
    write_results(
        [
            RowResult(
                row={"address": "x", "score": "", "case_id": ""},
                status=STATUS_ERROR,
                reason=payload,
            )
        ],
        path=target,
        headers=HEADERS,
    )
    header, rows = _read_csv(target)
    assert rows[0][header.index("pip_reason")] == "'" + payload


@pytest.mark.parametrize(
    "benign",
    ["121 N La Salle St", "", "17", "Albany Park", "O'Hare", " =not-leading", "0.5"],
)
def test_benign_text_is_never_mangled(benign):
    # The escape must be surgical: a general-purpose "sanitizer" that rewrites
    # ordinary addresses would corrupt every row of a real caseload.
    assert neutralize_cell(benign) == benign


def test_negative_coordinate_is_left_as_a_usable_number(tmp_path):
    # pip_lon is negative for every address in Chicago, so it trips the "-"
    # trigger on every row. A cell that is wholly a number cannot be a formula,
    # so it is exempt — otherwise the coordinate column would arrive as text in
    # every output file the service has ever produced.
    target = tmp_path / "out.csv"
    write_results([_matched()], path=target, headers=HEADERS)
    header, rows = _read_csv(target)
    assert rows[0][header.index("pip_lon")] == "-87.63192"
    assert float(rows[0][header.index("pip_lon")]) == pytest.approx(-87.63192)


@pytest.mark.parametrize("number", ["-87.63192", "+41.9", "-1", "-1.0e-5", "-.5"])
def test_plain_numbers_are_exempt_from_escaping(number):
    assert neutralize_cell(number) == number


@pytest.mark.parametrize(
    "not_a_number", ["-1+1", "+1-cmd", "-1 ", "-", "-1a", "=1", "@1", "-1,000"]
)
def test_number_carve_out_does_not_leak_formulas(not_a_number):
    # The exemption must be exact-match only: anything a spreadsheet could still
    # evaluate has to be escaped.
    assert neutralize_cell(not_a_number) == "'" + not_a_number


def test_csv_escapes_a_malicious_header_name(tmp_path):
    # The header row is the operator's input column names, verbatim — as
    # attacker-controlled as any data cell. It used to be written raw, straight
    # past the D21 escape, so an input file whose first column is named
    # "=cmd|'/c calc'!A0" produced an output whose A1 fires on open.
    target = tmp_path / "evil-header.csv"
    write_results(
        [_matched(address="1 Main St")],
        path=target,
        headers=(INJECTION, "score", "case_id"),
    )
    header, rows = _read_csv(target)
    assert header[0] == "'" + INJECTION
    assert not header[0].startswith("=")
    # The data row still lines up with the header it was written under.
    assert len(rows[0]) == len(header)


# ---- re-running over prior output (pip_ column collision) ----

def test_source_that_already_has_pip_columns_is_refused(tmp_path):
    # The obvious second run: take this feature's own output and locate it
    # against a second layer to get wards AND districts on one sheet. Blind
    # concatenation emitted pip_status twice, producing a file our own reader
    # (sources._headers_from) refuses — the pipeline could not round-trip
    # itself. Fail fast instead, and never silently rename.
    target = tmp_path / "second-pass.csv"
    already_located = ("address", "case_id", "pip_status", "pip_lat")

    with pytest.raises(BatchError) as raised:
        write_results([_matched()], path=target, headers=already_located)

    message = str(raised.value)
    assert "duplicate column name(s)" in message
    assert "'pip_status'" in message and "'pip_lat'" in message
    # The operator is told what to do about it.
    assert "new output file" in message
    # And nothing was written for a run that can never succeed.
    assert not target.exists()


def test_xlsx_source_that_already_has_pip_columns_is_refused(tmp_path):
    # Still a real scenario now that output is CSV only: the operator's *source*
    # is a workbook they made by opening a previous run's CSV in Excel and saving
    # it as .xlsx, so it carries pip_ columns. The headers come from the real
    # .xlsx reader, which is what makes this more than a tuple literal.
    openpyxl = pytest.importorskip("openpyxl")
    from app.batch.sources import read_source

    workbook = openpyxl.Workbook()
    workbook.active.append(["address", "pip_status"])
    workbook.active.append(["1 Main St", "matched"])
    source_path = tmp_path / "already-located.xlsx"
    workbook.save(source_path)
    workbook.close()

    source = read_source(str(source_path))
    target = tmp_path / "second-pass.csv"

    with pytest.raises(BatchError, match="duplicate column name"):
        write_results([_matched()], path=target, headers=source.headers)
    assert not target.exists()


def test_layer_attribute_collision_is_refused(tmp_path):
    # The collision is not limited to the fixed columns: a source carrying
    # pip_dist_num from a prior district run collides with this layer too.
    target = tmp_path / "second-pass.csv"
    with pytest.raises(BatchError, match="'pip_dist_num'"):
        write_results(
            [_matched()],
            path=target,
            headers=("address", "pip_dist_num"),
            layer_attributes=ATTRIBUTES,
        )


def test_unprefixed_near_miss_column_is_still_allowed_through(tmp_path):
    # "status" is not "pip_status". The whole point of the prefix is that an
    # operator's own column names stay usable, so the collision check must not
    # over-reach and start rejecting ordinary files.
    target = tmp_path / "near-miss.csv"
    count = write_results(
        [
            RowResult(
                row={"address": "1 Main St", "status": "open", "lat": "n/a"},
                status=STATUS_MATCHED,
                lat=41.9,
            )
        ],
        path=target,
        headers=("address", "status", "lat"),
    )
    assert count == 1
    header, rows = _read_csv(target)
    assert header[:3] == ["address", "status", "lat"]
    # Untouched: their values, not ours.
    assert rows[0][header.index("status")] == "open"
    assert rows[0][header.index("lat")] == "n/a"
    assert rows[0][header.index("pip_status")] == STATUS_MATCHED
    assert rows[0][header.index("pip_lat")] == "41.9"


# ---- partial writes (plan R15) ----

def test_iterator_failure_leaves_a_valid_partial_csv(tmp_path):
    target = tmp_path / "partial.csv"

    def failing_results():
        for n in range(3):
            yield _matched(address=f"{n} Main St")
        raise RuntimeError("provider died mid-run")

    with pytest.raises(RuntimeError, match="provider died mid-run"):
        write_results(
            failing_results(),
            path=target,
            headers=HEADERS,
            layer_attributes=ATTRIBUTES,
        )

    # The file is closed, complete through row 3, and readable by a plain reader.
    header, rows = _read_csv(target)
    assert tuple(header) == HEADERS + result_columns(ATTRIBUTES)
    assert len(rows) == 3
    assert [row[0] for row in rows] == ["0 Main St", "1 Main St", "2 Main St"]
    assert all(len(row) == len(header) for row in rows)


def test_rows_are_on_disk_before_the_iterator_finishes(tmp_path):
    # Proves the write is genuinely incremental rather than buffered to the end:
    # the file already holds row 1 while row 2 is still being produced.
    target = tmp_path / "incremental.csv"
    seen_midway = []

    def probing_results():
        yield _matched(address="first")
        seen_midway.append(target.read_text(encoding="utf-8-sig"))
        yield _matched(address="second")

    assert write_results(probing_results(), path=target, headers=HEADERS) == 2
    assert seen_midway and "first" in seen_midway[0]
    assert "second" not in seen_midway[0]


def test_unwritable_destination_raises_batch_error_not_oserror(tmp_path):
    # A missing parent directory is an operator typo, not a bug. The module's
    # caller contract is BatchError (the CLI catches it and exits 2 without a
    # traceback), so no OSError may escape.
    target = tmp_path / "no-such-dir" / "out.csv"

    with pytest.raises(BatchError) as raised:
        write_results([_matched()], path=target, headers=HEADERS)

    assert "could not open" in str(raised.value)
    assert "out.csv" in str(raised.value)
    # And the directory was NOT conjured up: this feature writes exactly the one
    # file it was told to write and nothing else (SPEC §9).
    assert not target.parent.exists()


# ---- the output file's permissions ----

@pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission bits are not enforced on Windows"
)
def test_output_file_is_readable_only_by_its_owner(tmp_path):
    # The output holds the operator's addresses by construction. On a shared
    # machine the usual umask would leave it world-readable, and someone who is
    # not an IT person would never think to check.
    target = tmp_path / "private.csv"
    write_results([_matched()], path=target, headers=HEADERS)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission bits are not enforced on Windows"
)
def test_an_existing_destination_is_overwritten_without_crashing(tmp_path):
    # Re-running over yesterday's output is ordinary. O_CREAT does not re-apply
    # the mode to a file that already exists, which is fine — it is the
    # operator's own file — but it must not fail, and it must be truncated.
    target = tmp_path / "yesterday.csv"
    target.write_text("stale contents that must not survive\n" * 50)
    target.chmod(0o644)

    assert write_results([_matched()], path=target, headers=HEADERS) == 1

    header, rows = _read_csv(target)
    assert "stale contents" not in target.read_text(encoding="utf-8-sig")
    assert len(rows) == 1


def test_a_csv_run_writes_exactly_one_file(tmp_path):
    # SPEC §9: the file the operator named is the only artifact of a run. No
    # sibling temp file, no lock file, no cache.
    target = tmp_path / "out.csv"

    write_results(
        [_matched(), _matched()],
        path=target,
        headers=HEADERS,
        layer_attributes=ATTRIBUTES,
    )

    assert [entry.name for entry in tmp_path.iterdir()] == ["out.csv"]
