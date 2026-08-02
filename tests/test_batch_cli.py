"""F7-T4 — tests for the batch CLI.

`main(argv)` is called directly — no subprocess, no network, no real geocoder.
The fixtures build a tiny two-square GeoPackage and a config.toml beside it (the
same idiom as tests/test_lookup.py and tests/test_batch_runner.py), so the CLI
exercises the real `load_config` → `PolygonLookup` → `run_batch` → `write_results`
path with only the geocoder replaced by a stub.

The address-path tests exist as much for SPEC §9 as for behaviour: every one of
them asserts that the queried address never appears on stdout or stderr.
"""
import csv
import importlib.util
import os
import stat
import sys
from pathlib import Path

import geopandas as gpd
import pytest
from pyproj import Transformer
from shapely.geometry import box

from app.batch import STATUS_ERROR, STATUS_MATCHED, STATUS_NO_GEOCODE, STATUS_OUTSIDE
from app.geocoding.base import GeocodeResult, GeocoderUnavailable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLI_PATH = PROJECT_ROOT / "scripts" / "batch_locate.py"


def _load_cli():
    """Import scripts/batch_locate.py as a module (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location("batch_locate_cli", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_cli()

# One square in EPSG:3435; query points are produced by inverting known planar
# coordinates back to WGS84 so the test never hardcodes a projection sign.
CX, CY = 1_100_000.0, 1_900_000.0
HALF = 2_000.0
_INV = Transformer.from_crs("EPSG:3435", "EPSG:4326", always_xy=True)
INSIDE_LON, INSIDE_LAT = _INV.transform(CX, CY)
OUTSIDE_LON, OUTSIDE_LAT = _INV.transform(CX + 50 * HALF, CY)

# A stand-in for a real caseload address. No test may find this string in any
# byte the CLI writes to a stream (SPEC §9).
SECRET_ADDRESS = "1428 N Elm St Apt 3B, Chicago, IL 60622"


class StubGeocoder:
    """A scripted geocoder: queued outcomes out, queries recorded.

    A queued exception instance is raised rather than returned, which is how the
    provider-failure rows are produced. `BaseException`, not `Exception`, so a
    queued `KeyboardInterrupt` can stand in for the operator hitting Ctrl-C.
    """

    def __init__(self, outcomes, name="stub"):
        self.name = name
        self._outcomes = list(outcomes)
        self.queries: list[str] = []

    def geocode(self, address: str) -> GeocodeResult:
        self.queries.append(address)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def matched_at(lon, lat, *, query=SECRET_ADDRESS, provider="stub"):
    return GeocodeResult(
        query=query,
        matched=True,
        provider=provider,
        point=(lon, lat),
        score=98.0,
        matched_address="1428 N ELM ST, CHICAGO, IL, 60622",
    )


GEOCODER_BLOCK = """
[[geocoders]]
id = "stub"
type = "census"
"""

NOMINATIM_BLOCK = """
[[geocoders]]
id = "osm"
type = "nominatim"
user_agent = "test"
"""

NOMINATIM_CHAIN_BLOCK = """
[[geocoders]]
id = "osm"
type = "nominatim"
user_agent = "test"

[[geocoders]]
id = "fallback"
type = "chain"
providers = ["osm"]
default = true
"""


@pytest.fixture
def config_path(tmp_path) -> Path:
    """A config.toml with one layer backed by a real one-square GeoPackage."""
    frame = gpd.GeoDataFrame(
        {"name": ["alpha"], "code": ["A"]},
        geometry=[box(CX - HALF, CY - HALF, CX + HALF, CY + HALF)],
        crs="EPSG:3435",
    )
    gpkg = tmp_path / "squares.gpkg"
    frame.to_file(gpkg, layer="squares", driver="GPKG")

    path = tmp_path / "config.toml"
    path.write_text(
        '[[layers]]\n'
        'id = "squares"\n'
        'name = "Test Squares"\n'
        'path = "squares.gpkg"\n'
        'layer = "squares"\n'
        'attributes = ["name", "code"]\n'
        'source = "synthetic test fixture"\n'
    )
    return path


def with_geocoders(config_path: Path, block: str) -> Path:
    """Append a [[geocoders]] block to the fixture config."""
    config_path.write_text(config_path.read_text() + block)
    return config_path


def write_csv(path: Path, headers, rows) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)
    return path


def read_output(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture
def points_csv(tmp_path) -> Path:
    """Two rows: one inside the square, one far outside it."""
    return write_csv(
        tmp_path / "points.csv",
        ["case_id", "lat", "lon"],
        [
            ["0042", f"{INSIDE_LAT}", f"{INSIDE_LON}"],
            ["0043", f"{OUTSIDE_LAT}", f"{OUTSIDE_LON}"],
        ],
    )


def latlon_argv(source, out, config_path, *extra):
    return [
        str(source),
        "--out",
        str(out),
        "--layer",
        "squares",
        "--lat-column",
        "lat",
        "--lon-column",
        "lon",
        "--config",
        str(config_path),
        *extra,
    ]


def address_argv(source, out, config_path, *extra):
    return [
        str(source),
        "--out",
        str(out),
        "--layer",
        "squares",
        "--address-column",
        "Address",
        "--config",
        str(config_path),
        "--rate-limit",
        "0",
        *extra,
    ]


# ---- the lat/lon path: no geocoder, no network -------------------------


def test_latlon_run_matches_and_exits_zero(tmp_path, config_path, capsys):
    source = write_csv(
        tmp_path / "one.csv",
        ["case_id", "lat", "lon"],
        [["0042", f"{INSIDE_LAT}", f"{INSIDE_LON}"]],
    )
    out = tmp_path / "out.csv"

    assert cli.main(latlon_argv(source, out, config_path)) == 0

    written = read_output(out)
    assert len(written) == 1
    assert written[0]["pip_status"] == STATUS_MATCHED
    assert written[0]["pip_name"] == "alpha"
    assert written[0]["pip_code"] == "A"
    # The operator's own columns survive untouched, leading zero included.
    assert written[0]["case_id"] == "0042"


def test_latlon_run_needs_no_geocoder_even_if_none_configured(
    tmp_path, config_path, monkeypatch
):
    """The offline path must never build a provider — not even to discard it."""

    def explode(_config):
        raise AssertionError("the lat/lon path must not build a geocoder")

    monkeypatch.setattr(cli, "build_geocoders", explode)
    out = tmp_path / "out.csv"
    source = write_csv(
        tmp_path / "one.csv",
        ["lat", "lon"],
        [[f"{INSIDE_LAT}", f"{INSIDE_LON}"]],
    )
    assert cli.main(latlon_argv(source, out, config_path)) == 0


def test_any_unmatched_row_exits_one(tmp_path, config_path, points_csv):
    out = tmp_path / "out.csv"

    assert cli.main(latlon_argv(points_csv, out, config_path)) == 1

    written = read_output(out)
    assert [row["pip_status"] for row in written] == [STATUS_MATCHED, STATUS_OUTSIDE]


def test_unparseable_coordinate_is_one_error_row_not_a_dead_run(
    tmp_path, config_path
):
    source = write_csv(
        tmp_path / "mixed.csv",
        ["case_id", "lat", "lon"],
        [
            ["1", "N/A", "N/A"],
            ["2", f"{INSIDE_LAT}", f"{INSIDE_LON}"],
        ],
    )
    out = tmp_path / "out.csv"

    assert cli.main(latlon_argv(source, out, config_path)) == 1

    written = read_output(out)
    assert [row["pip_status"] for row in written] == [STATUS_ERROR, STATUS_MATCHED]


def test_max_rows_caps_the_run(tmp_path, config_path, points_csv):
    out = tmp_path / "out.csv"

    exit_code = cli.main(latlon_argv(points_csv, out, config_path, "--max-rows", "1"))

    assert exit_code == 0
    assert len(read_output(out)) == 1


def test_progress_and_summary_go_to_stderr_leaving_stdout_clean(
    tmp_path, config_path, points_csv, capsys
):
    out = tmp_path / "out.csv"
    cli.main(latlon_argv(points_csv, out, config_path))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert STATUS_MATCHED in captured.err
    assert str(out) in captured.err


def test_row_counter_is_printed_as_the_run_proceeds(
    tmp_path, config_path, points_csv, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "PROGRESS_EVERY_ROWS", 1)
    cli.main(latlon_argv(points_csv, tmp_path / "out.csv", config_path))

    message = capsys.readouterr().err
    assert "1 rows processed (1 matched)" in message
    assert "2 rows processed (1 matched)" in message


# ---- the unclosed-quote row loss: loud, never silent --------------------

# Verbatim the operator's file: three data rows, an unclosed " on the A-2 line.
# Python's CSV reader absorbs A-3 into A-2's notes cell, so two rows reach the
# run and the third is gone. Under csv.field_size_limit nothing is raised.
UNCLOSED_QUOTE_CSV = (
    "case,notes,lat,lon\n"
    "A-1,fine,41.88,-87.63\n"
    'A-2,"he said,41.87,-87.62\n'
    "A-3,also fine,41.92,-87.65\n"
)


def multiline_csv(path: Path, lat, lon) -> Path:
    """Three rows where the middle one has a *legitimate* multi-line cell."""
    path.write_text(
        "case,notes,lat,lon\n"
        f"B-1,fine,{lat},{lon}\n"
        f'B-2,"line one\nline two",{lat},{lon}\n'
        f"B-3,also fine,{lat},{lon}\n",
        newline="",
    )
    return path


def test_unclosed_quote_warns_on_stderr_and_never_quotes_the_cell(
    tmp_path, config_path, capsys
):
    source = tmp_path / "q.csv"
    source.write_text(UNCLOSED_QUOTE_CSV, newline="")
    out = tmp_path / "out.csv"

    cli.main(latlon_argv(source, out, config_path))

    streams = capsys.readouterr()
    assert streams.out == ""  # stdout stays pipeable
    assert "WARNING" in streams.err
    assert "line 3" in streams.err
    assert 'unclosed " quote' in streams.err
    # SPEC §9: no cell content on any stream, warning or summary.
    for cell_content in ("he said", "also fine", "41.87"):
        assert cell_content not in streams.err

    # Read and written counts are both stated, so the operator can compare them
    # against the three data rows their file actually has.
    assert "read  2 data rows" in streams.err
    assert "wrote 2 rows" in streams.err
    assert "row contained a cell spanning multiple lines" in streams.err


def test_legitimate_multiline_cell_parses_whole_and_does_not_change_exit_code(
    tmp_path, config_path, capsys
):
    source = multiline_csv(tmp_path / "notes.csv", INSIDE_LAT, INSIDE_LON)
    out = tmp_path / "out.csv"

    # A genuine multi-line cell is not an error: every row matched, so exit 0.
    assert cli.main(latlon_argv(source, out, config_path)) == 0

    written = read_output(out)
    assert [row["case"] for row in written] == ["B-1", "B-2", "B-3"]
    assert written[1]["notes"] == "line one\nline two"

    streams = capsys.readouterr()
    assert streams.out == ""
    assert "WARNING" in streams.err
    assert "read  3 data rows" in streams.err
    assert "wrote 3 rows" in streams.err


def test_clean_source_gets_no_multiline_warning(
    tmp_path, config_path, points_csv, capsys
):
    cli.main(latlon_argv(points_csv, tmp_path / "out.csv", config_path))
    message = capsys.readouterr().err
    assert "WARNING" not in message
    assert "spanning multiple lines" not in message
    assert "read  2 data rows" in message


def test_interrupt_keeps_the_rows_already_written(
    tmp_path, config_path, monkeypatch, capsys
):
    """Ctrl-C mid-run: the CSV keeps the completed rows and the exit code is 1."""
    with_geocoders(config_path, GEOCODER_BLOCK)
    geocoder = StubGeocoder([matched_at(INSIDE_LON, INSIDE_LAT), KeyboardInterrupt()])
    monkeypatch.setattr(cli, "build_geocoders", lambda config: {"stub": geocoder})
    source = write_csv(
        tmp_path / "in.csv", ["Address"], [[SECRET_ADDRESS], [SECRET_ADDRESS]]
    )
    out = tmp_path / "out.csv"

    assert cli.main(address_argv(source, out, config_path)) == 1

    written = read_output(out)
    assert [row["pip_status"] for row in written] == [STATUS_MATCHED]
    streams = capsys.readouterr()
    assert "interrupted" in streams.err
    assert SECRET_ADDRESS not in streams.out + streams.err


# ---- whole-run failures: exit 2 ----------------------------------------


def test_unknown_layer_exits_two_and_lists_configured_layers(
    tmp_path, config_path, points_csv, capsys
):
    out = tmp_path / "out.csv"
    argv = latlon_argv(points_csv, out, config_path)
    argv[argv.index("--layer") + 1] = "precincts"

    assert cli.main(argv) == 2

    captured = capsys.readouterr()
    assert "precincts" in captured.err
    assert "squares" in captured.err
    assert not out.exists()


def test_bad_column_name_names_the_real_headers(
    tmp_path, config_path, points_csv, capsys
):
    out = tmp_path / "out.csv"
    argv = latlon_argv(points_csv, out, config_path)
    argv[argv.index("--lat-column") + 1] = "Latitude"

    assert cli.main(argv) == 2

    message = capsys.readouterr().err
    assert "'Latitude'" in message
    # The message must show what the file actually has, so the fix is obvious.
    assert "'case_id'" in message and "'lat'" in message and "'lon'" in message
    assert not out.exists()


def test_both_mapping_forms_is_refused(tmp_path, config_path, points_csv, capsys):
    out = tmp_path / "out.csv"
    argv = latlon_argv(points_csv, out, config_path) + [
        "--address-column",
        "Address",
    ]

    assert cli.main(argv) == 2
    assert "not both" in capsys.readouterr().err


def test_neither_mapping_form_is_refused(tmp_path, config_path, points_csv, capsys):
    out = tmp_path / "out.csv"
    argv = [
        str(points_csv),
        "--out",
        str(out),
        "--layer",
        "squares",
        "--config",
        str(config_path),
    ]

    assert cli.main(argv) == 2
    assert "--address-column" in capsys.readouterr().err


def test_missing_source_file_exits_two(tmp_path, config_path, capsys):
    out = tmp_path / "out.csv"
    argv = latlon_argv(tmp_path / "nope.csv", out, config_path)

    assert cli.main(argv) == 2
    assert "not found" in capsys.readouterr().err


def test_missing_config_exits_two(tmp_path, points_csv, capsys):
    out = tmp_path / "out.csv"
    argv = latlon_argv(points_csv, out, tmp_path / "absent.toml")

    assert cli.main(argv) == 2
    assert "config file not found" in capsys.readouterr().err


def test_negative_rate_limit_is_refused(tmp_path, config_path, points_csv, capsys):
    out = tmp_path / "out.csv"
    argv = latlon_argv(points_csv, out, config_path, "--rate-limit", "-1")

    assert cli.main(argv) == 2
    assert "--rate-limit" in capsys.readouterr().err


def test_zero_max_rows_is_refused(tmp_path, config_path, points_csv, capsys):
    out = tmp_path / "out.csv"
    argv = latlon_argv(points_csv, out, config_path, "--max-rows", "0")

    assert cli.main(argv) == 2
    assert "--max-rows" in capsys.readouterr().err


def test_unsupported_output_suffix_is_refused(tmp_path, config_path, points_csv, capsys):
    argv = latlon_argv(points_csv, tmp_path / "out.dbf", config_path)

    assert cli.main(argv) == 2
    assert "out.dbf" in capsys.readouterr().err


# ---- the geocoding path -------------------------------------------------


def test_address_run_uses_the_configured_provider(
    tmp_path, config_path, monkeypatch, capsys
):
    with_geocoders(config_path, GEOCODER_BLOCK)
    geocoder = StubGeocoder([matched_at(INSIDE_LON, INSIDE_LAT)])
    monkeypatch.setattr(cli, "build_geocoders", lambda config: {"stub": geocoder})

    source = write_csv(tmp_path / "in.csv", ["Address"], [[SECRET_ADDRESS]])
    out = tmp_path / "out.csv"

    assert cli.main(address_argv(source, out, config_path)) == 0

    assert geocoder.queries == [SECRET_ADDRESS]
    written = read_output(out)
    assert written[0]["pip_status"] == STATUS_MATCHED
    assert written[0]["pip_provider"] == "stub"

    streams = capsys.readouterr()
    assert SECRET_ADDRESS not in streams.out + streams.err


def test_failed_geocode_never_leaks_the_address_to_a_stream(
    tmp_path, config_path, monkeypatch, capsys
):
    with_geocoders(config_path, GEOCODER_BLOCK)
    geocoder = StubGeocoder(
        [
            GeocodeResult.no_match(SECRET_ADDRESS, "stub"),
            GeocoderUnavailable(f"upstream 502 while geocoding {SECRET_ADDRESS}"),
        ]
    )
    monkeypatch.setattr(cli, "build_geocoders", lambda config: {"stub": geocoder})

    source = write_csv(
        tmp_path / "in.csv", ["Address"], [[SECRET_ADDRESS], [SECRET_ADDRESS]]
    )
    out = tmp_path / "out.csv"

    assert cli.main(address_argv(source, out, config_path)) == 1

    written = read_output(out)
    assert [row["pip_status"] for row in written] == [STATUS_NO_GEOCODE, STATUS_ERROR]
    streams = capsys.readouterr()
    assert SECRET_ADDRESS not in streams.out + streams.err


def test_eta_is_printed_before_a_geocoding_run(
    tmp_path, config_path, monkeypatch, capsys
):
    with_geocoders(config_path, GEOCODER_BLOCK)
    monkeypatch.setattr(
        cli,
        "build_geocoders",
        lambda config: {"stub": StubGeocoder([matched_at(INSIDE_LON, INSIDE_LAT)])},
    )
    source = write_csv(tmp_path / "in.csv", ["Address"], [[SECRET_ADDRESS]])

    argv = address_argv(source, tmp_path / "out.csv", config_path)
    argv[argv.index("--rate-limit") + 1] = "1.5"
    argv += ["--max-rows", "1"]

    assert cli.main(argv) == 0

    message = capsys.readouterr().err
    assert "1 rows at 1.5 s between calls" in message
    assert "Ctrl-C is safe" in message


def test_no_geocoders_configured_says_how_to_proceed(
    tmp_path, config_path, points_csv, capsys
):
    source = write_csv(tmp_path / "in.csv", ["Address"], [[SECRET_ADDRESS]])

    assert cli.main(address_argv(source, tmp_path / "out.csv", config_path)) == 2

    message = capsys.readouterr().err
    assert "no [[geocoders]] are configured" in message
    assert "--lat-column" in message


def test_unknown_provider_lists_configured_ids(tmp_path, config_path, capsys):
    with_geocoders(config_path, GEOCODER_BLOCK)
    source = write_csv(tmp_path / "in.csv", ["Address"], [[SECRET_ADDRESS]])

    exit_code = cli.main(
        address_argv(
            source, tmp_path / "out.csv", config_path, "--provider", "typo"
        )
    )

    assert exit_code == 2
    message = capsys.readouterr().err
    assert "'typo'" in message and "stub" in message


def test_fast_rate_limit_warns_but_runs(tmp_path, config_path, monkeypatch, capsys):
    with_geocoders(config_path, GEOCODER_BLOCK)
    monkeypatch.setattr(
        cli,
        "build_geocoders",
        lambda config: {"stub": StubGeocoder([matched_at(INSIDE_LON, INSIDE_LAT)])},
    )
    source = write_csv(tmp_path / "in.csv", ["Address"], [[SECRET_ADDRESS]])

    assert cli.main(address_argv(source, tmp_path / "out.csv", config_path)) == 0
    assert "courtesy rate" in capsys.readouterr().err


# ---- D20: Nominatim is refused for batch --------------------------------


def test_nominatim_provider_is_refused(tmp_path, config_path, monkeypatch, capsys):
    with_geocoders(config_path, NOMINATIM_BLOCK)
    monkeypatch.setattr(
        cli,
        "build_geocoders",
        lambda config: pytest.fail("a refused provider must never be built"),
    )
    source = write_csv(tmp_path / "in.csv", ["Address"], [[SECRET_ADDRESS]])
    out = tmp_path / "out.csv"

    assert cli.main(address_argv(source, out, config_path)) == 2

    message = capsys.readouterr().err
    assert "usage policy" in message
    assert "--allow-nominatim" in message
    assert not out.exists()


def test_a_chain_that_falls_through_to_nominatim_is_refused(
    tmp_path, config_path, capsys
):
    with_geocoders(config_path, NOMINATIM_CHAIN_BLOCK)
    source = write_csv(tmp_path / "in.csv", ["Address"], [[SECRET_ADDRESS]])

    assert cli.main(address_argv(source, tmp_path / "out.csv", config_path)) == 2

    message = capsys.readouterr().err
    assert "'fallback'" in message and "'osm'" in message


def test_allow_nominatim_lets_a_self_hosted_instance_run(
    tmp_path, config_path, monkeypatch
):
    with_geocoders(config_path, NOMINATIM_BLOCK)
    geocoder = StubGeocoder([matched_at(INSIDE_LON, INSIDE_LAT)], name="osm")
    monkeypatch.setattr(cli, "build_geocoders", lambda config: {"osm": geocoder})
    source = write_csv(tmp_path / "in.csv", ["Address"], [[SECRET_ADDRESS]])
    out = tmp_path / "out.csv"

    exit_code = cli.main(
        address_argv(source, out, config_path, "--allow-nominatim")
    )

    assert exit_code == 0
    assert geocoder.queries == [SECRET_ADDRESS]


def test_nominatim_is_irrelevant_to_a_latlon_run(tmp_path, config_path, points_csv):
    """A coordinate run never selects a provider, so D20 cannot block it."""
    with_geocoders(config_path, NOMINATIM_BLOCK)
    out = tmp_path / "out.csv"
    assert cli.main(latlon_argv(points_csv, out, config_path)) == 1


# ---- helpers ------------------------------------------------------------


def test_nominatim_provider_ids_walks_chains_and_survives_a_cycle(config_path):
    with_geocoders(
        config_path,
        """
[[geocoders]]
id = "osm"
type = "nominatim"
user_agent = "test"

[[geocoders]]
id = "loop_a"
type = "chain"
providers = ["loop_b"]

[[geocoders]]
id = "loop_b"
type = "chain"
providers = ["loop_a", "osm"]
""",
    )
    from app.config import load_config

    app_config = load_config(config_path)

    assert cli.nominatim_provider_ids(app_config, "loop_a") == ["osm"]
    assert cli.nominatim_provider_ids(app_config, "osm") == ["osm"]


def test_format_duration_scales():
    assert cli.format_duration(30) == "30 sec"
    assert cli.format_duration(2000) == "33 min"
    assert cli.format_duration(7200) == "2.0 hr"


def test_describe_eta_without_a_row_count_quotes_a_rate():
    message = cli.describe_eta(None, 1.0)
    assert "per 1,000 rows" in message
    assert "Ctrl-C is safe" in message


def test_describe_eta_with_a_row_count_is_exact():
    assert cli.describe_eta(2000, 1.0).startswith(
        "2,000 rows at 1 s between calls = about 33 min"
    )


def test_usage_string_names_every_flag():
    usage = cli.build_parser().format_help()
    for flag in (
        "--out",
        "--layer",
        "--address-column",
        "--lat-column",
        "--lon-column",
        "--provider",
        "--config",
        "--rate-limit",
        "--max-rows",
        "--sheet-gid",
        "--allow-nominatim",
    ):
        assert flag in usage


def test_writing_onto_the_source_file_is_refused(tmp_path, config_path, points_csv, capsys):
    # Data loss, not tidiness: --out is opened with "w" (truncating) while the
    # source is still being streamed, so this destroys the operator's input and
    # then re-reads the rows it just wrote, growing the file without bound.
    before = points_csv.read_bytes()
    argv = latlon_argv(points_csv, points_csv, config_path)

    assert cli.main(argv) == 2
    assert "source file itself" in capsys.readouterr().err
    assert points_csv.read_bytes() == before


def test_writing_onto_the_source_by_another_path_is_refused(
    tmp_path, config_path, points_csv, capsys
):
    # The same file named two ways — a symlink here — must be caught too, or the
    # guard is trivially defeated by how the operator happened to type the path.
    link = tmp_path / "same_points.csv"
    link.symlink_to(points_csv)
    before = points_csv.read_bytes()

    assert cli.main(latlon_argv(points_csv, link, config_path)) == 2
    assert "source file itself" in capsys.readouterr().err
    assert points_csv.read_bytes() == before


def test_a_different_output_file_is_allowed(tmp_path, config_path, points_csv):
    # The guard must not be so eager that it blocks the ordinary case.
    out = tmp_path / "located.csv"

    assert cli.main(latlon_argv(points_csv, out, config_path)) == 1  # one row outside
    assert len(read_output(out)) == 2


def test_unwritable_output_directory_exits_two_without_a_traceback(
    tmp_path, config_path, points_csv, capsys
):
    # main() promises an exit code, never a traceback: an OSError from the
    # writer must arrive as a one-line refusal.
    argv = latlon_argv(points_csv, tmp_path / "absent-dir" / "out.csv", config_path)

    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert "batch failed" in err
    assert "Traceback" not in err


# ---- output is CSV only; .xlsx INPUT is untouched -----------------------

# Words a person who does not work in IT cannot act on. The refusal has to be
# readable by the operator, not by the maintainer.
JARGON = ("SIGKILL", "atexit", "staging", "stage", "TMPDIR", "openpyxl")


def assert_no_jargon(message: str) -> None:
    lowered = message.lower()
    for word in JARGON:
        assert word.lower() not in lowered, f"jargon {word!r} in: {message}"


def test_xlsx_output_is_refused_with_exit_two_and_no_jargon(
    tmp_path, config_path, points_csv, capsys
):
    argv = latlon_argv(points_csv, tmp_path / "results.xlsx", config_path)

    assert cli.main(argv) == 2

    streams = capsys.readouterr()
    assert "this tool writes .csv only" in streams.err
    assert "--out <name>.csv" in streams.err
    assert "Traceback" not in streams.err
    assert_no_jargon(streams.err)
    # And no file was made under the name that can never be written.
    assert not (tmp_path / "results.xlsx").exists()


def test_xlsx_output_is_refused_before_the_source_is_even_read(
    tmp_path, config_path, capsys
):
    """The check is lexical and costs nothing, so it must not queue behind the
    ones that cost a file read and a half-hour of geocoding.

    The source here does not exist. If the .xlsx refusal were late, the message
    would be about the missing file; that it is the .xlsx message proves nothing
    was opened first.
    """
    missing = tmp_path / "no-such-source.csv"
    assert not missing.exists()

    assert cli.main(latlon_argv(missing, tmp_path / "out.xlsx", config_path)) == 2

    err = capsys.readouterr().err
    assert "this tool writes .csv only" in err
    assert "no-such-source" not in err


def test_xlsx_output_is_refused_before_a_single_address_is_geocoded(
    tmp_path, config_path, monkeypatch, capsys
):
    # The expensive half. A geocoding run must not spend one request before
    # finding out the destination is unusable.
    with_geocoders(config_path, GEOCODER_BLOCK)
    geocoder = StubGeocoder([matched_at(INSIDE_LON, INSIDE_LAT)])
    monkeypatch.setattr(cli, "build_geocoders", lambda config: {"stub": geocoder})
    source = write_csv(tmp_path / "in.csv", ["Address"], [[SECRET_ADDRESS]])

    assert cli.main(address_argv(source, tmp_path / "out.xlsx", config_path)) == 2

    assert geocoder.queries == []
    streams = capsys.readouterr()
    assert "this tool writes .csv only" in streams.err
    assert SECRET_ADDRESS not in streams.out + streams.err


def test_an_xlsx_source_writes_a_csv_output_end_to_end(tmp_path, config_path):
    # Reading a workbook is untouched by the CSV-only output rule: an operator
    # whose caseload lives in Excel still runs it, they just get a .csv back.
    openpyxl = pytest.importorskip("openpyxl")

    workbook = openpyxl.Workbook()
    workbook.active.append(["case_id", "lat", "lon"])
    workbook.active.append(["0042", INSIDE_LAT, INSIDE_LON])
    workbook.active.append(["0043", OUTSIDE_LAT, OUTSIDE_LON])
    source = tmp_path / "caseload.xlsx"
    workbook.save(source)
    workbook.close()

    out = tmp_path / "out.csv"
    assert cli.main(latlon_argv(source, out, config_path)) == 1

    written = read_output(out)
    assert [row["pip_status"] for row in written] == [STATUS_MATCHED, STATUS_OUTSIDE]
    assert written[0]["pip_name"] == "alpha"
    # The leading zero survived the workbook read and the CSV write.
    assert written[0]["case_id"] == "0042"


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission bits are not enforced on Windows"
)
def test_the_output_file_is_readable_only_by_its_owner(
    tmp_path, config_path, points_csv
):
    out = tmp_path / "out.csv"
    cli.main(latlon_argv(points_csv, out, config_path))

    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_a_finished_run_says_what_the_file_holds_and_never_shows_an_address(
    tmp_path, config_path, monkeypatch, capsys
):
    # One closing line, in plain words, for someone who would not otherwise
    # think about where the file with their addresses in it is sitting.
    with_geocoders(config_path, GEOCODER_BLOCK)
    geocoder = StubGeocoder([matched_at(INSIDE_LON, INSIDE_LAT)])
    monkeypatch.setattr(cli, "build_geocoders", lambda config: {"stub": geocoder})
    source = write_csv(tmp_path / "in.csv", ["Address"], [[SECRET_ADDRESS]])
    out = tmp_path / "out.csv"

    assert cli.main(address_argv(source, out, config_path)) == 0

    streams = capsys.readouterr()
    assert "contains the addresses you looked up" in streams.err
    assert "only your user account can read it" in streams.err
    assert "delete it" in streams.err
    assert_no_jargon(streams.err)
    # It names the file, never a row (SPEC §9).
    assert SECRET_ADDRESS not in streams.out + streams.err
    # stdout stays clean and pipeable.
    assert streams.out == ""


# ---- C2: no unexpected exception escapes the top-level boundary ---------


def test_an_unexpected_error_exits_two_without_a_traceback(
    tmp_path, config_path, points_csv, monkeypatch, capsys
):
    """A MemoryError from deep in the pipeline is neither BatchError, ConfigError
    nor OSError. Without a catch-all it would print a traceback and exit 1 —
    indistinguishable from a normal run with unmatched rows."""

    def explode(*args, **kwargs):
        raise MemoryError()

    monkeypatch.setattr(cli, "run_batch", explode)

    assert cli.main(latlon_argv(points_csv, tmp_path / "out.csv", config_path)) == 2

    message = capsys.readouterr().err
    assert "batch failed" in message
    assert "Traceback" not in message
    # It must still say what went wrong and where.
    assert "MemoryError" in message
    assert "explode" in message


def test_an_unexpected_error_never_echoes_the_exception_message(
    tmp_path, config_path, monkeypatch, capsys
):
    """A third-party exception is free to interpolate the value it choked on,
    and here that value can be an address (SPEC §9). Only the type and origin
    are printed."""
    with_geocoders(config_path, GEOCODER_BLOCK)
    geocoder = StubGeocoder([matched_at(INSIDE_LON, INSIDE_LAT)])
    monkeypatch.setattr(cli, "build_geocoders", lambda config: {"stub": geocoder})

    def explode(*args, **kwargs):
        raise ValueError(f"could not parse {SECRET_ADDRESS!r}")

    monkeypatch.setattr(cli, "run_batch", explode)
    source = write_csv(tmp_path / "in.csv", ["Address"], [[SECRET_ADDRESS]])

    assert cli.main(address_argv(source, tmp_path / "out.csv", config_path)) == 2

    streams = capsys.readouterr()
    assert SECRET_ADDRESS not in streams.out + streams.err
    assert "ValueError" in streams.err


def test_ctrl_c_is_still_reported_as_an_interruption_not_a_crash(
    tmp_path, config_path, points_csv, monkeypatch, capsys
):
    # The catch-all must not swallow the Ctrl-C path: KeyboardInterrupt keeps
    # its own message and its own exit code.
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "run_batch", interrupt)

    assert cli.main(latlon_argv(points_csv, tmp_path / "out.csv", config_path)) == 1

    message = capsys.readouterr().err
    assert "interrupted" in message
    assert "unexpected" not in message


def test_describe_exception_names_the_type_and_origin_only():
    try:
        raise RuntimeError(SECRET_ADDRESS)
    except RuntimeError as error:
        described = cli.describe_exception(error)

    assert described.startswith("RuntimeError raised at test_batch_cli.py:")
    assert SECRET_ADDRESS not in described
