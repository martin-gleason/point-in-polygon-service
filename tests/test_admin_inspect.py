"""F8-T2 — tests for the candidate reader.

Every fixture is built here, in the test, out of the stdlib and GeoPandas: real
zip archives (including a zip-slip archive, a symlink archive and a compression
bomb), real shapefile sets written by GDAL, a real GeoPackage, real GeoJSON.
Every HTTP call is mocked with respx. The suite touches the network exactly
never, which is the point — the reader has to be exercisable on an air-gapped
box, and a paging bug that only shows up against a live county server is a
paging bug nobody ever sees.

The assertion that runs through all of it: the coordinate reference system that
comes out is the one that went in, bit for bit. `app.admin.validate`'s two
hardest checks (PIP-L003, PIP-L004) work by comparing what a file *declares*
against the numbers it *holds*, and a reader that helpfully fills in EPSG:4326
destroys both of them.
"""
from __future__ import annotations

import json
import sqlite3
import struct
import tempfile
import zipfile
from pathlib import Path

import geopandas as gpd
import httpx
import pytest
import respx
from shapely.geometry import Polygon

from app.admin import inspect as reader
from app.admin.inspect import Candidate, CandidateError, read_candidate
from app.admin.validate import (
    SOURCE_ARCGIS_REST,
    SOURCE_GEOJSON,
    SOURCE_GEOPACKAGE,
    SOURCE_SHAPEFILE,
    validate_candidate,
)

# Illinois State Plane East (ftUS) — the CRS this service stores its own layers
# in, and a projected one, so "the CRS survived" is a real claim and not just
# "it came back 4326 like everything else does".
STATE_PLANE = "EPSG:3435"

SERVICE_BASE = "https://gis.example.gov/rest/services/wards/MapServer/2"
SERVICE_QUERY = f"{SERVICE_BASE}/query"

SHIPPED_LAYERS = Path(__file__).resolve().parent.parent / "data" / "layers.gpkg"


# --------------------------------------------------------------------------
# fixtures built on the spot
# --------------------------------------------------------------------------


def _square(offset: float, size: float = 1.0) -> Polygon:
    return Polygon(
        [
            (offset, offset),
            (offset, offset + size),
            (offset + size, offset + size),
            (offset + size, offset),
        ]
    )


def _frame(rows: int = 3, crs: str = STATE_PLANE, columns=None) -> gpd.GeoDataFrame:
    columns = columns or {"ward": [str(index + 1) for index in range(rows)]}
    data = dict(columns)
    data["geometry"] = [_square(1_100_000.0 + index * 10) for index in range(rows)]
    return gpd.GeoDataFrame(data, geometry="geometry", crs=crs)


def _write_shapefile(directory: Path, stem: str, **kwargs) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    shp = directory / f"{stem}.shp"
    _frame(**kwargs).to_file(shp)
    return shp


def _zip_directory(directory: Path, archive: Path, prefix: str = "") -> Path:
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for item in sorted(directory.iterdir()):
            bundle.write(item, f"{prefix}{item.name}")
    return archive


def _good_zip(tmp_path: Path, stem: str = "wards", **kwargs) -> Path:
    staging = tmp_path / f"staging-{stem}"
    _write_shapefile(staging, stem, **kwargs)
    return _zip_directory(staging, tmp_path / f"{stem}.zip")


def _feature(index: int) -> dict:
    return {
        "type": "Feature",
        "id": index,
        "properties": {"ward": str(index), "ward_preci": f"{index}-A"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [-87.6 + index * 0.01, 41.8],
                    [-87.6 + index * 0.01, 41.81],
                    [-87.59 + index * 0.01, 41.81],
                    [-87.59 + index * 0.01, 41.8],
                    [-87.6 + index * 0.01, 41.8],
                ]
            ],
        },
    }


def _paged_service(total: int, page_size: int, *, flag_full_pages: bool = True):
    """A stand-in ArcGIS layer that honours resultOffset/resultRecordCount.

    Records every (offset, count) it is asked for on `.calls` so a test can show
    the walk itself, not merely its result.
    """

    calls: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        offset = int(params.get("resultOffset", 0))
        count = int(params.get("resultRecordCount", page_size))
        count = min(count, page_size)
        calls.append((offset, count))
        window = [_feature(index) for index in range(offset, min(offset + count, total))]
        body: dict = {"type": "FeatureCollection", "features": window}
        if flag_full_pages and offset + len(window) < total:
            body["exceededTransferLimit"] = True
        return httpx.Response(200, json=body)

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


def _mock_service(router: respx.Router, handler, *, metadata: dict | None = None):
    router.get(SERVICE_QUERY).mock(side_effect=handler)
    router.get(SERVICE_BASE).mock(
        return_value=httpx.Response(200, json=metadata or {"name": "Wards"})
    )


def _codes(candidate: Candidate) -> list[str]:
    return [found.code for found in candidate.findings]


# --------------------------------------------------------------------------
# (1) zipped shapefile — the normal way a portal hands one out
# --------------------------------------------------------------------------


def test_zipped_shapefile_reads_and_keeps_its_crs(tmp_path):
    candidate = read_candidate(_good_zip(tmp_path))

    assert candidate.source_kind == SOURCE_SHAPEFILE
    assert len(candidate.frame) == 3
    # The whole point: what the .prj said is what the checks will see.
    assert candidate.frame.crs is not None
    assert candidate.frame.crs.to_epsg() == 3435
    assert candidate.facts["crs"]["epsg"] == 3435
    assert candidate.facts["crs"]["declared"] is True
    # Every piece that came out of the archive is named.
    assert set(candidate.source_files) >= {"wards.shp", "wards.dbf", "wards.shx",
                                           "wards.prj"}
    candidate.cleanup()


def test_zip_extracts_only_shapefile_pieces_and_nothing_else(tmp_path):
    staging = tmp_path / "staging"
    _write_shapefile(staging, "wards")
    (staging / "README.txt").write_text("please read me")
    (staging / "install.sh").write_text("#!/bin/sh\necho hello\n")
    (staging / "extra.gpkg").write_bytes(b"not really a geopackage")
    archive = _zip_directory(staging, tmp_path / "wards.zip")

    candidate = read_candidate(archive)
    written = sorted(item.name for item in candidate.workspace.iterdir())

    assert written == sorted(
        name for name in written if Path(name).suffix in reader.SHAPEFILE_EXTENSIONS
    )
    assert "README.txt" not in written
    assert "install.sh" not in written
    assert "extra.gpkg" not in written
    candidate.cleanup()


def test_cleanup_removes_the_workspace_and_can_be_called_twice(tmp_path):
    candidate = read_candidate(_good_zip(tmp_path))
    workspace = candidate.workspace
    assert workspace.exists()

    candidate.cleanup()
    assert not workspace.exists()
    candidate.cleanup()  # must not raise


def test_zip_with_a_member_escaping_its_directory_is_refused(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("wards.shp", b"harmless-looking")
        bundle.writestr("../../../../etc/cron.d/pwned", b"* * * * * root sh\n")

    with pytest.raises(CandidateError) as raised:
        read_candidate(archive)

    finding = raised.value.finding
    assert finding.code == "PIP-L013"
    assert finding.is_blocking
    assert finding.detail["reason"] == "parent_directory"
    # Refused from the directory alone — nothing was unpacked anywhere.
    assert not (tmp_path / "etc").exists()


@pytest.mark.parametrize(
    "member_name, expected_reason",
    [
        ("/etc/passwd", "absolute_path"),
        ("..\\..\\windows\\system32\\evil.dll", "windows_path_separator"),
        ("C:/windows/evil.dll", "windows_drive_letter"),
    ],
)
def test_zip_escape_variants_are_each_refused(tmp_path, member_name, expected_reason):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("wards.shp", b"harmless-looking")
        bundle.writestr(member_name, b"payload")

    with pytest.raises(CandidateError) as raised:
        read_candidate(archive)

    assert raised.value.finding.code == "PIP-L013"
    assert raised.value.finding.detail["reason"] == expected_reason


def test_zip_containing_a_symlink_member_is_refused(tmp_path):
    """A link escapes when it is followed, not when it is written — so it has to
    be refused on its recorded mode, before anything is unpacked at all."""
    archive = tmp_path / "linky.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("wards.shp", b"harmless-looking")
        info = zipfile.ZipInfo("wards.prj")
        info.external_attr = (0o120777 << 16) | 0o120000
        bundle.writestr(info, "/etc")

    with pytest.raises(CandidateError) as raised:
        read_candidate(archive)

    assert raised.value.finding.code == "PIP-L013"
    assert raised.value.finding.detail["reason"] == "symbolic_link"


def test_zip_with_an_absurd_compression_ratio_is_refused(tmp_path):
    """16 MiB of one repeated byte packs to a few kilobytes — roughly 1000:1,
    where a real shapefile set manages 3:1 to 8:1."""
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("wards.shp", b"\0" * (16 * 1024 * 1024))

    with pytest.raises(CandidateError) as raised:
        read_candidate(archive)

    finding = raised.value.finding
    assert finding.code == "PIP-L012"
    assert finding.detail["reason"] == "compression_ratio"
    assert finding.detail["ratio"] > reader.MAX_COMPRESSION_RATIO
    # The archive on disk is a rounding error next to what it claims to unpack to.
    assert archive.stat().st_size < 1024 * 1024


@pytest.mark.skipif(
    not SHIPPED_LAYERS.exists(), reason="data/layers.gpkg not in this checkout"
)
@pytest.mark.parametrize("layer", ["police_districts", "municipalities"])
def test_the_caps_leave_real_boundary_layers_far_below_them(tmp_path, layer):
    """The other half of a refusal is that it never fires on real data.

    The two layers this service actually ships, written out as shapefiles and
    zipped the way a portal would: this pins the headroom the cap comment in
    `app.admin.inspect` claims, so a later tightening of the numbers has to
    argue with a measurement rather than a memory.
    """
    staging = tmp_path / "shipped"
    staging.mkdir()
    gpd.read_file(SHIPPED_LAYERS, layer=layer).to_file(staging / f"{layer}.shp")
    archive = _zip_directory(staging, tmp_path / f"{layer}.zip")

    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
    total = sum(info.file_size for info in members)
    worst = max(info.file_size / max(info.compress_size, 1) for info in members)

    assert total < reader.MAX_TOTAL_UNCOMPRESSED_BYTES / 100
    assert worst < reader.MAX_COMPRESSION_RATIO / 10
    assert len(members) < reader.MAX_ARCHIVE_MEMBERS

    candidate = read_candidate(archive)
    assert len(candidate.frame) > 0
    assert candidate.frame.crs.to_epsg() == 3435  # the shipped CRS, untouched
    candidate.cleanup()


def test_zip_over_the_total_unpacked_cap_is_refused(tmp_path, monkeypatch):
    """The total cap, exercised at a size a test can build. 512 MiB is the real
    number and is deliberately unbuildable in a unit test; the code path is the
    same one."""
    monkeypatch.setattr(reader, "MAX_TOTAL_UNCOMPRESSED_BYTES", 200)
    archive = _good_zip(tmp_path)

    with pytest.raises(CandidateError) as raised:
        read_candidate(archive)

    finding = raised.value.finding
    assert finding.code == "PIP-L012"
    assert finding.detail["reason"] == "total_too_large"
    assert finding.detail["total_limit"] == 200
    assert finding.detail["total_bytes"] > 200


def test_zip_with_too_many_members_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, "MAX_ARCHIVE_MEMBERS", 3)
    archive = _good_zip(tmp_path)

    with pytest.raises(CandidateError) as raised:
        read_candidate(archive)

    assert raised.value.finding.detail["reason"] == "too_many_members"


def _never_parses(*args, **kwargs):
    """A `zipfile.ZipFile` that fails the test if anything opens an archive."""
    raise AssertionError(
        "zipfile.ZipFile() was called — the archive was parsed before it was "
        "bounded, which is the cost the bound exists to avoid"
    )


def test_too_many_members_is_refused_before_the_archive_is_parsed(
    tmp_path, monkeypatch
):
    """The member cap, enforced where it can still save anything.

    `zipfile.ZipFile()` builds a ZipInfo for every entry in the central
    directory inside its constructor, so a cap read off `infolist()` is a cap
    applied after the expense it exists to prevent: 300,000 empty members in a
    29.8 MB zip are refused correctly and cost ~175 MB of RSS to refuse, all of
    it spent before any code in this module runs. The count the archive declares
    in its own tail is 22 bytes away and answers the same question.
    """
    monkeypatch.setattr(reader, "MAX_ARCHIVE_MEMBERS", 3)
    archive = _good_zip(tmp_path)  # a full shapefile set: more than three
    monkeypatch.setattr(reader.zipfile, "ZipFile", _never_parses)

    with pytest.raises(CandidateError) as raised:
        read_candidate(archive)

    finding = raised.value.finding
    assert finding.code == "PIP-L012"
    assert finding.detail["reason"] == "too_many_members"
    assert finding.detail["member_count"] > 3


def test_an_archive_too_big_to_open_is_refused_on_one_stat(tmp_path, monkeypatch):
    """The outer bound: the file's own size, before the tail is even read."""
    archive = _good_zip(tmp_path)
    monkeypatch.setattr(reader, "MAX_ARCHIVE_BYTES", 100)
    monkeypatch.setattr(reader.zipfile, "ZipFile", _never_parses)

    with pytest.raises(CandidateError) as raised:
        read_candidate(archive)

    finding = raised.value.finding
    assert finding.code == "PIP-L012"
    assert finding.detail["reason"] == "archive_too_large"
    assert finding.detail["archive_limit"] == 100


def test_a_zip_that_is_not_one_still_gets_its_own_message(tmp_path):
    """The bound must not swallow the honest 'this does not open as a zip'.

    Nothing readable as an end-of-central-directory record means "no opinion",
    and `zipfile` is what tells the operator their download was cut short.
    """
    fake = tmp_path / "wards.zip"
    fake.write_bytes(b"not a zip at all")

    with pytest.raises(CandidateError) as raised:
        read_candidate(fake)

    assert raised.value.finding.detail["reason"] == "unreadable_archive"


def test_a_damaged_member_is_a_finding_and_not_a_zlib_traceback(tmp_path):
    """One flipped byte inside an otherwise valid archive.

    The directory parses, the caps pass, the escape check passes — and the
    member's compressed stream then raises `zlib.error` (or `BadZipFile`,
    'Bad CRC-32', when the flip happens to decompress to something) straight out
    of `read_candidate`. Both are ordinary: a download that stopped halfway
    produces them as readily as a hostile file does. The contract is a Finding,
    and F8-T4 has no traceback to show a volunteer.
    """
    archive = _good_zip(tmp_path)
    raw = bytearray(archive.read_bytes())
    with zipfile.ZipFile(archive) as bundle:
        info = bundle.getinfo("wards.shp")
    name_length, extra_length = struct.unpack(
        "<HH", raw[info.header_offset + 26 : info.header_offset + 30]
    )
    payload_at = info.header_offset + 30 + name_length + extra_length
    raw[payload_at + 4] ^= 0xFF  # one byte, in the member's own bytes
    archive.write_bytes(bytes(raw))

    with pytest.raises(CandidateError) as raised:
        read_candidate(archive)

    finding = raised.value.finding
    assert finding.code == "PIP-L001"
    assert finding.is_blocking
    assert finding.detail["reason"] == "unreadable_member"
    assert finding.detail["member"] == "wards.shp"
    assert "damaged" in finding.specifics


def test_zip_missing_the_dbf_names_exactly_what_is_missing(tmp_path):
    staging = tmp_path / "staging"
    _write_shapefile(staging, "wards")
    (staging / "wards.dbf").unlink()
    archive = _zip_directory(staging, tmp_path / "wards.zip")

    with pytest.raises(CandidateError) as raised:
        read_candidate(archive)

    finding = raised.value.finding
    assert finding.code == "PIP-L002"
    assert finding.detail["missing_extensions"] == [".dbf"]
    assert finding.specifics.startswith("The '.dbf' piece is missing.")
    # The .prj is present here, but the point is that its absence would NOT be
    # reported under this code either — that is PIP-L003's, and only PIP-L003's.
    assert ".prj" not in finding.detail["missing_extensions"]
    assert "wards.shx" in finding.specifics  # what did arrive is named


def test_zip_with_two_shapefiles_refuses_to_guess_and_offers_the_choice(tmp_path):
    staging = tmp_path / "staging"
    _write_shapefile(staging, "wards")
    _write_shapefile(staging, "precincts", rows=2)
    archive = _zip_directory(staging, tmp_path / "both.zip")

    with pytest.raises(CandidateError) as raised:
        read_candidate(archive)

    finding = raised.value.finding
    assert finding.code == "PIP-L001"
    assert finding.detail["reason"] == "several_shapefiles"
    assert finding.detail["shapefiles"] == ["precincts", "wards"]
    assert "precincts" in finding.specifics and "wards" in finding.specifics

    # And the choice, once made, is honoured — 2 rows, not the 3 in `wards`.
    chosen = read_candidate(archive, select="precincts")
    assert len(chosen.frame) == 2
    chosen.cleanup()

    with pytest.raises(CandidateError) as bad_choice:
        read_candidate(archive, select="boroughs")
    assert bad_choice.value.finding.detail["reason"] == "unknown_selection"


def test_zip_with_no_shapefile_in_it_at_all(tmp_path):
    archive = tmp_path / "docs.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("notes.txt", "no map data here")

    with pytest.raises(CandidateError) as raised:
        read_candidate(archive)

    assert raised.value.finding.code == "PIP-L001"
    assert raised.value.finding.detail["reason"] == "no_shapefile_in_archive"


def test_a_file_that_is_not_a_zip_at_all(tmp_path):
    fake = tmp_path / "wards.zip"
    fake.write_bytes(b"this is not a zip file")

    with pytest.raises(CandidateError) as raised:
        read_candidate(fake)

    assert raised.value.finding.code == "PIP-L001"
    assert raised.value.finding.detail["reason"] == "unreadable_archive"


def test_two_folders_holding_the_same_stem_are_two_shapefiles_not_one(tmp_path):
    """A zip can carry two different layers under one filename.

    Reproduced against the pre-fix reader: `2024_official/wards.*` and
    `zzz_attacker/wards.*` collapsed to the single stem `wards`, so the
    ambiguity refusal was never reached, the member sorting later overwrote the
    member sorting earlier, and the attacker's layer installed with no finding
    of any kind. Identity is the folder as well as the stem.
    """
    official = tmp_path / "2024_official"
    attacker = tmp_path / "zzz_attacker"
    _write_shapefile(official, "wards", columns={"ward": ["1", "2", "3"]})
    _write_shapefile(attacker, "wards", columns={"ward": ["x", "y", "z"]})

    archive = tmp_path / "wards.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for folder in (official, attacker):
            for item in sorted(folder.iterdir()):
                bundle.write(item, f"{folder.name}/{item.name}")

    with pytest.raises(CandidateError) as raised:
        read_candidate(archive)

    finding = raised.value.finding
    assert finding.code == "PIP-L001"
    assert finding.detail["reason"] == "several_shapefiles"
    # Both are offered, each named where it actually sits, so F8-T4 can render a
    # picker that distinguishes them at all.
    assert finding.detail["shapefiles"] == [
        "2024_official/wards",
        "zzz_attacker/wards",
    ]

    chosen = read_candidate(archive, select="2024_official/wards")
    assert sorted(chosen.frame["ward"]) == ["1", "2", "3"]
    chosen.cleanup()


def test_a_shapefile_set_is_never_assembled_out_of_two_folders(tmp_path):
    """The nastier half: not a whole layer swapped, a piece of one.

    Real outlines from one folder plus somebody else's table of names from
    another used to merge into a single set, because the pieces were gathered by
    bare filename one extension at a time. The result opens, validates, and
    draws a correct map of Chicago carrying the attacker's ward labels — the
    operator's only real defence, looking at the shape on screen, shows
    something perfectly plausible. Pieces never cross a folder boundary, so what
    is left is an incomplete set and PIP-L002 says which piece is missing.
    """
    real = tmp_path / "a_real"
    forged = tmp_path / "z_forged"
    _write_shapefile(real, "wards", columns={"ward": ["1", "2", "3"]})
    _write_shapefile(forged, "wards", columns={"ward": ["x", "y", "z"]})

    archive = tmp_path / "wards.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for item in sorted(real.iterdir()):
            if item.suffix != ".dbf":  # the real set, minus its table of names
                bundle.write(item, f"a_real/{item.name}")
        bundle.write(forged / "wards.dbf", "z_forged/wards.dbf")

    with pytest.raises(CandidateError) as raised:
        read_candidate(archive)

    finding = raised.value.finding
    assert finding.code == "PIP-L002"
    assert finding.detail["missing_extensions"] == [".dbf"]


def test_two_members_recorded_under_one_name_are_refused(tmp_path):
    """A zip may hold the same name twice. Whichever was written second would
    replace the first on disk, which is the same swap by another route."""
    staging = tmp_path / "staging"
    _write_shapefile(staging, "wards")

    archive = tmp_path / "wards.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for item in sorted(staging.iterdir()):
            bundle.write(item, item.name)
        bundle.writestr("wards.dbf", b"a second table of names entirely")

    with pytest.raises(CandidateError) as raised:
        read_candidate(archive)

    finding = raised.value.finding
    assert finding.code == "PIP-L001"
    assert finding.detail["reason"] == "duplicate_member"
    assert finding.detail["extension"] == ".dbf"


def test_two_uploads_claiming_the_same_piece_are_refused(tmp_path):
    """The loose-file version of the same swap: two files both sent as
    `wards.dbf`, one of which would silently replace the other while staging."""
    source = tmp_path / "source"
    _write_shapefile(source, "wards")
    parts = sorted(source.iterdir())

    with pytest.raises(CandidateError) as raised:
        read_candidate(
            list(parts) + [source / "wards.dbf"],
            source_files=[item.name for item in parts] + ["wards.dbf"],
        )

    assert raised.value.finding.detail["reason"] == "duplicate_shapefile_part"


def test_zip_members_inside_a_folder_are_still_found(tmp_path):
    staging = tmp_path / "staging"
    _write_shapefile(staging, "wards")
    archive = _zip_directory(staging, tmp_path / "wards.zip", prefix="Wards_2026/")

    candidate = read_candidate(archive)
    assert len(candidate.frame) == 3
    candidate.cleanup()


# --------------------------------------------------------------------------
# (2) loose shapefile parts
# --------------------------------------------------------------------------


def test_loose_parts_read_together(tmp_path):
    _write_shapefile(tmp_path / "loose", "wards")
    parts = sorted((tmp_path / "loose").iterdir())

    candidate = read_candidate(parts)

    assert candidate.source_kind == SOURCE_SHAPEFILE
    assert candidate.frame.crs.to_epsg() == 3435
    assert len(candidate.frame) == 3


def test_pointing_at_the_shp_alone_collects_its_companions(tmp_path):
    shp = _write_shapefile(tmp_path / "loose", "wards")

    candidate = read_candidate(shp)

    assert len(candidate.frame) == 3
    assert "wards.dbf" in candidate.source_files


def test_loose_parts_without_a_prj_read_fine_and_are_not_reported_as_missing(tmp_path):
    """A missing .prj is PIP-L003's business, not PIP-L002's. The file still
    opens; it just comes back saying nothing about where on Earth it sits, and
    reporting that under two codes would have the operator chasing one problem
    twice."""
    folder = tmp_path / "loose"
    _write_shapefile(folder, "wards")
    (folder / "wards.prj").unlink()

    candidate = read_candidate(sorted(folder.iterdir()))

    assert candidate.frame.crs is None  # untouched, not guessed
    assert candidate.facts["crs"]["declared"] is False
    assert "PIP-L002" not in _codes(candidate)

    # ...and the validator is the one that raises it, exactly once.
    findings = validate_candidate(
        candidate.frame,
        context=candidate.to_context(layer_id="wards", display_name="Wards"),
    )
    assert [found.code for found in findings if found.code == "PIP-L003"] == [
        "PIP-L003"
    ]


@pytest.mark.parametrize("dropped", [".dbf", ".shx"])
def test_loose_parts_missing_a_required_piece_name_it(tmp_path, dropped):
    folder = tmp_path / "loose"
    _write_shapefile(folder, "wards")
    (folder / f"wards{dropped}").unlink()

    with pytest.raises(CandidateError) as raised:
        read_candidate(sorted(folder.iterdir()))

    finding = raised.value.finding
    assert finding.code == "PIP-L002"
    assert finding.detail["missing_extensions"] == [dropped]
    assert dropped in finding.specifics


def test_loose_parts_missing_two_pieces_name_both(tmp_path):
    folder = tmp_path / "loose"
    _write_shapefile(folder, "wards")
    (folder / "wards.dbf").unlink()
    (folder / "wards.shx").unlink()

    with pytest.raises(CandidateError) as raised:
        read_candidate(sorted(folder.iterdir()))

    assert raised.value.finding.detail["missing_extensions"] == [".dbf", ".shx"]


def test_uploaded_parts_in_separate_folders_are_gathered_and_named_properly(tmp_path):
    """What a browser upload actually looks like: each piece in its own temporary
    file, under a name nothing should ever show the operator."""
    source = tmp_path / "source"
    _write_shapefile(source, "wards")

    scattered: list[Path] = []
    names: list[str] = []
    for index, piece in enumerate(sorted(source.iterdir())):
        holding = tmp_path / f"upload{index}"
        holding.mkdir()
        temp_file = holding / f"tmp{index}abcdef"
        temp_file.write_bytes(piece.read_bytes())
        scattered.append(temp_file)
        names.append(piece.name)

    candidate = read_candidate(scattered, source_files=names)

    assert len(candidate.frame) == 3
    assert candidate.frame.crs.to_epsg() == 3435
    assert candidate.source_files == tuple(sorted(names))
    assert all("tmp" not in name for name in candidate.source_files)
    candidate.cleanup()


def test_a_failed_upload_read_leaves_no_workspace_behind(tmp_path, monkeypatch):
    """The staging folder is this reader's to release when the read fails.

    Every browser upload takes the staging path — the pieces arrive in separate
    temporary files under names nothing should show — so this folder is created
    on the ordinary route, not an exotic one. When `gpd.read_file` then refuses
    the .shp, nothing had released it: three attempts left three folders in
    $TMPDIR, each holding a full copy of the operator's data, permanently. The
    zip route only looked safe because `_read_zip` wraps its own call.
    """
    holding = tmp_path / "tmp"
    holding.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(holding))

    source = tmp_path / "source"
    _write_shapefile(source, "wards")
    scattered: list[Path] = []
    names: list[str] = []
    for index, piece in enumerate(sorted(source.iterdir())):
        if piece.suffix not in (".shp", ".dbf", ".shx"):
            continue
        upload = tmp_path / f"upload{index}"
        upload.mkdir()
        temp_file = upload / f"tmp{index}abcdef"
        # The .shp arrives damaged — the case the operator hits when a download
        # was cut short, and the one that used to leak.
        temp_file.write_bytes(
            b"not a shapefile" if piece.suffix == ".shp" else piece.read_bytes()
        )
        scattered.append(temp_file)
        names.append(piece.name)

    for _ in range(3):
        with pytest.raises(CandidateError) as raised:
            read_candidate(scattered, source_files=names)
        assert raised.value.finding.detail["reason"] == "unreadable_shapefile"

    assert list(holding.glob("pip-layer-*")) == []


def test_a_stray_file_among_the_parts_is_called_out(tmp_path):
    folder = tmp_path / "loose"
    _write_shapefile(folder, "wards")
    stray = folder / "notes.txt"
    stray.write_text("hello")

    with pytest.raises(CandidateError) as raised:
        read_candidate(sorted(folder.iterdir()))

    assert raised.value.finding.detail["reason"] == "mixed_input"
    assert "notes.txt" in raised.value.finding.specifics


def test_a_shp_xml_alongside_is_noted_rather_than_treated_as_a_stray(tmp_path):
    folder = tmp_path / "loose"
    _write_shapefile(folder, "wards")
    (folder / "wards.shp.xml").write_text("<metadata><pubdate>20240115</pubdate></metadata>")

    candidate = read_candidate(sorted(folder.iterdir()))

    assert candidate.facts["shapefile_metadata_file"] == "wards.shp.xml"
    vintage_finding = next(f for f in candidate.findings if f.code == "PIP-L017")
    assert "wards.shp.xml" in vintage_finding.specifics


def test_a_missing_file_is_reported_by_the_name_the_operator_used(tmp_path):
    with pytest.raises(CandidateError) as raised:
        read_candidate(tmp_path / "nope.shp", source_files=["Wards 2026.shp"])

    assert raised.value.finding.code == "PIP-L001"
    assert "Wards 2026.shp" in raised.value.finding.specifics


def test_a_single_browser_upload_is_read_by_the_name_the_operator_sent(tmp_path):
    """Every single-file upload was falsely refused.

    Dispatch read the on-disk suffix, and an upload lands as `tmp0abcdef` with
    no extension at all — so a real 17-feature GeoJSON came back PIP-L001
    `unsupported_extension`, with a message that then contradicted itself by
    naming the operator's own `.geojson` file. The multi-file branch ten lines
    below had already been root-caused: key on the name the operator supplied,
    with the on-disk suffix as the fallback.
    """
    payload = {
        "type": "FeatureCollection",
        "features": [_feature(index) for index in range(17)],
    }
    upload = tmp_path / "tmp0abcdef"  # what a browser upload actually looks like
    upload.write_text(json.dumps(payload))

    candidate = read_candidate(upload, source_files=["ward25_precincts.geojson"])

    assert candidate.source_kind == SOURCE_GEOJSON
    assert len(candidate.frame) == 17
    assert candidate.source_files == ("ward25_precincts.geojson",)
    assert "tmp0abcdef" not in json.dumps(candidate.facts)


def test_a_single_uploaded_zip_and_gpkg_are_read_the_same_way(tmp_path):
    """The same defect broke .zip and .gpkg, which are the other two things
    F8-T4 and F8-T5 will hand this reader as one nameless temporary file."""
    zipped = (_good_zip(tmp_path)).read_bytes()
    zip_upload = tmp_path / "tmp1abcdef"
    zip_upload.write_bytes(zipped)

    gpkg = tmp_path / "layers.gpkg"
    _frame(rows=4).to_file(gpkg, layer="wards", driver="GPKG")
    gpkg_upload = tmp_path / "tmp2abcdef"
    gpkg_upload.write_bytes(gpkg.read_bytes())

    from_zip = read_candidate(zip_upload, source_files=["Wards 2026.zip"])
    assert from_zip.source_kind == SOURCE_SHAPEFILE
    assert len(from_zip.frame) == 3
    assert from_zip.facts["unpacked_from"] == "Wards 2026.zip"
    from_zip.cleanup()

    from_gpkg = read_candidate(gpkg_upload, source_files=["wards.gpkg"])
    assert from_gpkg.source_kind == SOURCE_GEOPACKAGE
    assert len(from_gpkg.frame) == 4


def test_a_lone_uploaded_shp_asks_for_the_rest_of_its_set(tmp_path):
    """A .shp uploaded on its own has no companions anywhere — the temporary
    folder it landed in is not a shapefile workspace. The honest answer is which
    pieces are missing, not that a .shp is an unsupported kind of file."""
    source = tmp_path / "source"
    _write_shapefile(source, "wards")
    upload = tmp_path / "tmp3abcdef"
    upload.write_bytes((source / "wards.shp").read_bytes())

    with pytest.raises(CandidateError) as raised:
        read_candidate(upload, source_files=["wards.shp"])

    finding = raised.value.finding
    assert finding.code == "PIP-L002"
    assert finding.detail["missing_extensions"] == [".dbf", ".shx"]
    assert "tmp3abcdef" not in finding.specifics


def test_an_unreadable_kind_of_file(tmp_path):
    spreadsheet = tmp_path / "wards.xlsx"
    spreadsheet.write_bytes(b"PK\x03\x04 not really")

    with pytest.raises(CandidateError) as raised:
        read_candidate(spreadsheet)

    assert raised.value.finding.code == "PIP-L001"
    assert raised.value.finding.detail["reason"] == "unsupported_extension"


# --------------------------------------------------------------------------
# vintage, and the two warnings the reader itself raises
# --------------------------------------------------------------------------


def test_a_shapefile_has_no_vintage_and_says_so_with_its_write_date(tmp_path):
    """The .shp header's version field is the constant 1000 and the .dbf header's
    date is when the export ran — neither is how old the boundaries are. So the
    vintage stays None, PIP-L017 fires, and the write date is offered as what it
    actually is."""
    shp = _write_shapefile(tmp_path / "loose", "wards")

    candidate = read_candidate(shp)

    assert candidate.vintage is None
    assert "PIP-L017" in _codes(candidate)
    written_on = candidate.facts["shapefile_written_on"]
    assert written_on is not None
    # A real date, and today's, since GDAL wrote the file a moment ago.
    from datetime import date

    assert written_on == date.today().isoformat()
    specifics = next(f for f in candidate.findings if f.code == "PIP-L017").specifics
    assert written_on in specifics
    assert "written out" in specifics

    # And the .shp header really does carry the 1998 format number, not a date.
    header = (tmp_path / "loose" / "wards.shp").read_bytes()
    assert int.from_bytes(header[28:32], "little") == 1000


def test_a_ten_letter_column_name_raises_the_truncation_warning(tmp_path):
    shp = _write_shapefile(
        tmp_path / "loose",
        "wards",
        columns={"ward_preci": ["1", "2", "3"], "name": ["a", "b", "c"]},
    )

    candidate = read_candidate(shp)

    truncated = next(f for f in candidate.findings if f.code == "PIP-L018")
    assert truncated.detail["columns_at_limit"] == ["ward_preci"]
    assert "ward_preci" in truncated.specifics
    assert "name" not in truncated.detail["columns_at_limit"]


def test_no_truncation_warning_when_no_name_sits_on_the_limit(tmp_path):
    shp = _write_shapefile(tmp_path / "loose", "wards", columns={"ward": ["1", "2", "3"]})
    assert "PIP-L018" not in _codes(read_candidate(shp))


# --------------------------------------------------------------------------
# (3) GeoJSON
# --------------------------------------------------------------------------


def test_geojson_reads_and_has_no_vintage(tmp_path):
    path = tmp_path / "wards.geojson"
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": [_feature(1), _feature(2)]})
    )

    candidate = read_candidate(path)

    assert candidate.source_kind == SOURCE_GEOJSON
    assert len(candidate.frame) == 2
    assert candidate.frame.crs.to_epsg() == 4326  # RFC 7946's fixed system
    assert candidate.vintage is None
    assert _codes(candidate) == ["PIP-L017"]
    assert "GeoJSON" in candidate.findings[0].specifics
    assert candidate.workspace is None


def test_geojson_keeps_a_declared_crs_rather_than_assuming_wgs84(tmp_path):
    """A pre-RFC-7946 file with the old `crs` member says something other than
    4326, and that claim is exactly what PIP-L004 exists to test. It must arrive
    at the checks unedited."""
    path = tmp_path / "wards.geojson"
    body = json.loads(_frame(rows=2).to_json())
    body["crs"] = {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3435"}}
    path.write_text(json.dumps(body))

    candidate = read_candidate(path)

    assert candidate.frame.crs.to_epsg() == 3435


def test_a_json_file_that_is_not_geojson(tmp_path):
    path = tmp_path / "wards.json"
    path.write_text(json.dumps({"records": [{"ward": 1}]}))

    with pytest.raises(CandidateError) as raised:
        read_candidate(path)

    assert raised.value.finding.code == "PIP-L001"
    assert raised.value.finding.detail["reason"] == "unreadable_geojson"


# --------------------------------------------------------------------------
# GeoPackage — the one format carrying a real date
# --------------------------------------------------------------------------


def test_geopackage_round_trip_picks_up_its_last_change_stamp(tmp_path):
    path = tmp_path / "layers.gpkg"
    _frame(rows=4).to_file(path, layer="wards", driver="GPKG")

    stored = sqlite3.connect(path).execute(
        "SELECT last_change FROM gpkg_contents WHERE table_name = 'wards'"
    ).fetchone()[0]

    candidate = read_candidate(path)

    assert candidate.source_kind == SOURCE_GEOPACKAGE
    assert len(candidate.frame) == 4
    assert candidate.frame.crs.to_epsg() == 3435
    assert candidate.vintage is not None
    assert stored in candidate.vintage
    assert candidate.facts["geopackage_last_change"] == stored
    assert candidate.facts["geopackage_layer"] == "wards"
    # A real date was found, so the operator is not warned that none was.
    assert "PIP-L017" not in _codes(candidate)
    # ...and the validator agrees, because the vintage rides through the context.
    context = candidate.to_context(layer_id="wards", display_name="Wards")
    assert context.vintage == candidate.vintage
    assert "PIP-L017" not in [
        found.code for found in validate_candidate(candidate.frame, context=context)
    ]


def test_geopackage_with_two_layers_refuses_to_guess(tmp_path):
    path = tmp_path / "layers.gpkg"
    _frame(rows=4).to_file(path, layer="wards", driver="GPKG")
    _frame(rows=2).to_file(path, layer="precincts", driver="GPKG")

    with pytest.raises(CandidateError) as raised:
        read_candidate(path)
    assert raised.value.finding.detail["reason"] == "several_layers"
    assert raised.value.finding.detail["layers"] == ["precincts", "wards"]

    chosen = read_candidate(path, select="precincts")
    assert len(chosen.frame) == 2


def test_a_file_that_is_not_a_geopackage(tmp_path):
    path = tmp_path / "wards.gpkg"
    path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)

    with pytest.raises(CandidateError) as raised:
        read_candidate(path)

    assert raised.value.finding.code == "PIP-L001"
    assert raised.value.finding.detail["reason"] in (
        "not_a_geopackage",
        "unreadable_geopackage",
    )


# --------------------------------------------------------------------------
# (4) ArcGIS REST — all mocked, never a socket
# --------------------------------------------------------------------------


def test_arcgis_layer_url_is_turned_into_the_pipeline_s_geojson_query():
    with respx.mock(assert_all_called=False) as router:
        _mock_service(router, _paged_service(total=3, page_size=1000))
        candidate = read_candidate(SERVICE_BASE)

        request = router.calls[-1].request
        assert request.url.path.endswith("/MapServer/2/query")
        assert request.url.params["where"] == "1=1"
        assert request.url.params["outFields"] == "*"
        assert request.url.params["f"] == "geojson"
        assert "point-in-polygon-service" in request.headers["user-agent"]

    assert candidate.source_kind == SOURCE_ARCGIS_REST
    assert len(candidate.frame) == 3
    assert candidate.frame.crs.to_epsg() == 4326
    # Two requests for three features, and deliberately so: this service
    # publishes no maxRecordCount, so the 1,000 asked for was this reader's own
    # guess and a short page proves nothing about the layer. The walk confirms
    # with an empty page. See the truncation test below for what the one-request
    # version costs.
    assert candidate.facts["pages_fetched"] == 2
    assert candidate.facts["max_record_count"] is None
    assert candidate.source_files == ()


def test_arcgis_paging_fetches_every_feature_exactly_once():
    """The trap this whole path exists for.

    A service that caps a response and sets exceededTransferLimit will hand back
    a perfectly valid half-county to a reader that takes the first page — and
    every check downstream passes on it, because half a county is valid data.
    So the assertion is not "we got some features": it is that the set of ward
    numbers that came back is exactly 0..249 with no gap and no repeat, and that
    the walk really did take three pages to get there.
    """
    handler = _paged_service(total=250, page_size=100)
    with respx.mock(assert_all_called=False) as router:
        _mock_service(router, handler, metadata={"name": "Wards", "maxRecordCount": 100})
        candidate = read_candidate(SERVICE_BASE)

    wards = [int(value) for value in candidate.frame["ward"]]
    assert len(wards) == 250
    assert sorted(wards) == list(range(250))  # nothing dropped
    assert len(set(wards)) == 250  # nothing duplicated
    assert wards == list(range(250))  # and in order, page after page

    # The walk itself: three requests, each offset stepping by the number of
    # features actually received (not by the size asked for), stopping on the
    # short final page that did not claim there was more.
    assert handler.calls == [(0, 100), (100, 100), (200, 100)]
    assert candidate.facts["pages_fetched"] == 3
    assert candidate.facts["max_record_count"] == 100


def test_arcgis_stops_on_a_short_final_page_without_a_needless_extra_request():
    handler = _paged_service(total=150, page_size=100)
    with respx.mock(assert_all_called=False) as router:
        _mock_service(router, handler, metadata={"maxRecordCount": 100})
        candidate = read_candidate(SERVICE_BASE)

    assert len(candidate.frame) == 150
    assert handler.calls == [(0, 100), (100, 100)]


def test_exceeded_transfer_limit_on_a_short_page_still_asks_for_more():
    """Some services set the flag on a page that is also short. Length alone
    would call that the end and quietly truncate the layer."""
    pages = [
        {
            "type": "FeatureCollection",
            "features": [_feature(index) for index in range(0, 40)],
            "exceededTransferLimit": True,
        },
        {
            "type": "FeatureCollection",
            "features": [_feature(index) for index in range(40, 55)],
        },
    ]
    served = iter(pages)

    with respx.mock(assert_all_called=False) as router:
        _mock_service(
            router,
            lambda request: httpx.Response(200, json=next(served)),
            metadata={"maxRecordCount": 100},
        )
        candidate = read_candidate(SERVICE_BASE)

    assert len(candidate.frame) == 55
    assert sorted(int(value) for value in candidate.frame["ward"]) == list(range(55))


def test_the_transfer_limit_flag_is_honoured_when_it_is_nested_in_properties():
    """Some ArcGIS versions put the flag inside `properties` rather than at the
    top level. Missing it there truncates the layer just as completely."""
    pages = [
        {
            "type": "FeatureCollection",
            "features": [_feature(index) for index in range(0, 30)],
            "properties": {"exceededTransferLimit": True},
        },
        {
            "type": "FeatureCollection",
            "features": [_feature(index) for index in range(30, 45)],
        },
    ]
    served = iter(pages)
    with respx.mock(assert_all_called=False) as router:
        _mock_service(
            router,
            lambda request: httpx.Response(200, json=next(served)),
            metadata={"maxRecordCount": 100},
        )
        candidate = read_candidate(SERVICE_BASE)

    assert len(candidate.frame) == 45


def test_a_service_capping_below_the_guessed_page_size_is_still_walked_whole():
    """The truncation this reader's own docstring promises cannot happen.

    A service that publishes no maxRecordCount, caps its answers well below the
    1,000 this reader guesses, and sets no exceededTransferLimit — all three of
    which real services do — used to end the walk on its first answer: one
    request, a fraction of the layer, findings ['PIP-L017'], no block and no
    warning. Every check downstream then passed, because a fifth of a county is
    made of perfectly valid polygons.

    The root cause is that the terminator treated the reader's own guess as a
    fact about the remote layer. A short page may only end the walk when the
    size asked for came from the service or the operator.
    """
    handler = _paged_service(total=120, page_size=50, flag_full_pages=False)
    with respx.mock(assert_all_called=False) as router:
        _mock_service(router, handler, metadata={"name": "Wards"})  # no maxRecordCount
        candidate = read_candidate(SERVICE_BASE)

    wards = [int(value) for value in candidate.frame["ward"]]
    assert wards == list(range(120))  # the whole layer, no gap, no repeat
    # Advanced by what actually arrived — 50 at a time, not the 1,000 asked for
    # — and stopped only on the empty page that proves there is no more.
    assert [offset for offset, _ in handler.calls] == [0, 50, 100, 120]
    assert candidate.facts["pages_fetched"] == 4


def test_a_failed_metadata_request_cannot_shrink_the_layer():
    """The same defect through a second door.

    `_arcgis_layer_metadata` is best-effort and answers {} on any HTTP error, so
    a service that *does* publish maxRecordCount dropped into the truncating
    regime whenever that one request happened to fail. A transient 500 on a
    best-effort request must not change how much of a layer gets installed.
    """
    handler = _paged_service(total=120, page_size=50, flag_full_pages=False)
    with respx.mock(assert_all_called=False) as router:
        router.get(SERVICE_QUERY).mock(side_effect=handler)
        router.get(SERVICE_BASE).mock(return_value=httpx.Response(500, text="oops"))
        candidate = read_candidate(SERVICE_BASE)

    assert [int(value) for value in candidate.frame["ward"]] == list(range(120))
    assert candidate.facts["max_record_count"] is None


def test_a_crs_the_service_declared_survives_being_reassembled():
    """The pages are reassembled into one FeatureCollection, and a top-level
    `crs` member the service emitted used to be dropped on the way. GDAL then
    stamps EPSG:4326 on the result, so a service declaring 3435 and sending
    State Plane feet came back *labelled* 4326 — PIP-L004 fires, but tells the
    operator their data claims degrees when the service was right all along."""
    body = json.loads(_frame(rows=2).to_json())  # State Plane coordinates
    body["crs"] = {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3435"}}
    served = iter([body, {"type": "FeatureCollection", "features": []}])

    with respx.mock(assert_all_called=False) as router:
        _mock_service(
            router,
            lambda request: httpx.Response(200, json=next(served)),
            metadata={"name": "Wards"},
        )
        candidate = read_candidate(SERVICE_BASE)

    assert candidate.frame.crs.to_epsg() == 3435
    assert candidate.facts["crs"]["epsg"] == 3435


def test_a_requested_out_sr_is_recorded_as_what_the_coordinates_are():
    """`outSR` was forwarded to the service and never written down.

    With `outSR=4269` the service reprojects to NAD83 — a geographic system, so
    degrees in and degrees out — the payload carries no crs member, GDAL stamps
    4326, and nothing fires: PIP-L004 compares a declaration against the numbers
    and degrees look like degrees. The frame installs about a metre out of
    place, invisibly on a preview map and permanently at every boundary.
    """
    with respx.mock(assert_all_called=False) as router:
        _mock_service(router, _paged_service(total=2, page_size=1000))
        candidate = read_candidate(f"{SERVICE_QUERY}?outSR=4269")

        asked = next(
            call.request for call in router.calls
            if call.request.url.path.endswith("/query")
        )
        assert asked.url.params["outSR"] == "4269"  # still forwarded

    assert candidate.frame.crs.to_epsg() == 4269  # and now also recorded
    assert candidate.facts["crs"]["epsg"] == 4269
    assert candidate.facts["requested_out_sr"] == 4269


def test_an_out_sr_that_cannot_be_recorded_is_refused_rather_than_assumed():
    """An outSR given as a JSON spatial reference cannot be turned into a label,
    and a layer whose coordinate system is unknown is not safe to install."""
    with respx.mock(assert_all_called=False) as router:
        _mock_service(router, _paged_service(total=2, page_size=1000))
        with pytest.raises(CandidateError) as raised:
            read_candidate(f'{SERVICE_QUERY}?outSR={{"wkt":"PROJCS[...]"}}')

    finding = raised.value.finding
    assert finding.code == "PIP-L014"
    assert finding.detail["reason"] == "unrecordable_out_sr"
    # SPEC §9: the query string never reaches a message or the detail.
    assert "PROJCS" not in finding.specifics
    assert "outSR" not in str(finding.detail)


def test_an_out_sr_disagreeing_with_the_services_own_label_is_refused():
    body = json.loads(_frame(rows=2).to_json())
    body["crs"] = {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3435"}}

    with respx.mock(assert_all_called=False) as router:
        _mock_service(
            router,
            lambda request: httpx.Response(200, json=body),
            metadata={"name": "Wards", "maxRecordCount": 1000},
        )
        with pytest.raises(CandidateError) as raised:
            read_candidate(f"{SERVICE_QUERY}?outSR=4269")

    finding = raised.value.finding
    assert finding.detail["reason"] == "out_sr_conflict"
    assert finding.detail["out_sr"] == 4269
    assert finding.detail["declared_epsg"] == 3435


def test_a_service_that_ignores_paging_is_a_refusal_not_a_partial_layer():
    """The worst case: the offset is ignored, page one comes back forever. Taking
    what arrived would install the same 100 areas twice and call it a county."""
    page = {
        "type": "FeatureCollection",
        "features": [_feature(index) for index in range(100)],
        "exceededTransferLimit": True,
    }
    with respx.mock(assert_all_called=False) as router:
        _mock_service(
            router,
            lambda request: httpx.Response(200, json=page),
            metadata={"maxRecordCount": 100},
        )
        with pytest.raises(CandidateError) as raised:
            read_candidate(SERVICE_BASE)

    finding = raised.value.finding
    assert finding.code == "PIP-L014"
    assert finding.is_blocking
    assert finding.detail["reason"] == "paging_ignored"


def test_a_layer_larger_than_the_service_can_hold_is_a_refusal(monkeypatch):
    monkeypatch.setattr(reader, "MAX_ARCGIS_FEATURES", 150)
    handler = _paged_service(total=1_000, page_size=100)
    with respx.mock(assert_all_called=False) as router:
        _mock_service(router, handler, metadata={"maxRecordCount": 100})
        with pytest.raises(CandidateError) as raised:
            read_candidate(SERVICE_BASE)

    assert raised.value.finding.detail["reason"] == "too_many_features"


def test_one_oversized_page_is_refused_by_the_cap_it_used_to_step_over(monkeypatch):
    """The cap has to hold on every way out of the walk, not only on the loop.

    It used to be tested after `offset += ...`, which the two breaks above reach
    first — so a service that answered the whole layer in one page was never
    judged by it at all. A service advertising a huge maxRecordCount and
    answering with 600,000 features against a 500,000 limit was accepted
    outright (RSS 820 MB to 1.37 GB). The far end chooses both the size of a
    page and what is in it, so a limit it can skip by answering in one go
    constrains only the services that were never a problem.
    """
    monkeypatch.setattr(reader, "MAX_ARCGIS_FEATURES", 150)
    page = {
        "type": "FeatureCollection",
        "features": [_feature(index) for index in range(200)],
    }
    with respx.mock(assert_all_called=False) as router:
        _mock_service(
            router,
            lambda request: httpx.Response(200, json=page),
            metadata={"maxRecordCount": 1000},  # one short page ends the walk
        )
        with pytest.raises(CandidateError) as raised:
            read_candidate(SERVICE_BASE)

    finding = raised.value.finding
    assert finding.code == "PIP-L014"
    assert finding.detail["reason"] == "too_many_features"
    assert finding.detail["fetched"] == 200
    assert finding.detail["pages"] == 1


def test_a_published_max_record_count_cannot_choose_the_page_size():
    """`maxRecordCount` is a claim by the far end, not an instruction.

    Taken verbatim, it lets a service ask this process to hold a page of a
    billion features — it picks the number, then picks what to put in it. The
    number asked for is bounded here, and because the bounded number is one
    neither the operator nor the service named, it stops being a fact about the
    layer: a short page can no longer end the walk (hence the confirming second
    request below).
    """
    handler = _paged_service(total=3, page_size=1000)
    with respx.mock(assert_all_called=False) as router:
        _mock_service(
            router, handler, metadata={"name": "Wards", "maxRecordCount": 1_000_000_000}
        )
        candidate = read_candidate(SERVICE_BASE)
        asked = [
            int(call.request.url.params["resultRecordCount"])
            for call in router.calls
            if call.request.url.path.endswith("/query")
        ]

    assert asked and set(asked) == {reader.MAX_ARCGIS_PAGE_SIZE}
    assert len(candidate.frame) == 3
    assert candidate.facts["pages_fetched"] == 2


def _flood(counter: dict, *, content_type: str):
    """A far end that keeps talking: 4 MB in 1 KB chunks, counted as it goes."""

    def handler(request: httpx.Request) -> httpx.Response:
        def chunks():
            for _ in range(4_000):
                counter["bytes"] += 1024
                yield b"x" * 1024

        return httpx.Response(
            200, content=chunks(), headers={"content-type": content_type}
        )

    return handler


def test_a_giant_answer_is_cut_off_rather_than_buffered_whole(monkeypatch):
    """`client.get()` reads the whole body and `.text` decodes all of it.

    A 314 MB text/html answer was therefore buffered and decoded in full on the
    way to a refusal that only ever quotes its first 200 characters — RSS 420 MB
    to 1.32 GB. The refusal was right; the memory was spent anyway, and how much
    is spent is chosen entirely by the stranger at the other end.
    """
    monkeypatch.setattr(reader, "MAX_RESPONSE_BYTES", 64 * 1024)
    counter = {"bytes": 0}
    with respx.mock(assert_all_called=False) as router:
        router.get(SERVICE_BASE).mock(return_value=httpx.Response(200, json={}))
        router.get(SERVICE_QUERY).mock(
            side_effect=_flood(counter, content_type="text/html")
        )
        with pytest.raises(CandidateError) as raised:
            read_candidate(SERVICE_BASE)

    finding = raised.value.finding
    assert finding.code == "PIP-L014"
    assert finding.detail["reason"] == "response_too_large"
    # Stopped listening near the bound rather than at the end of the flood.
    assert counter["bytes"] < 4 * 64 * 1024


def test_a_giant_layer_description_is_dropped_rather_than_buffered_whole(monkeypatch):
    """The same exposure through the best-effort metadata request, which cannot
    fail loudly — so it stops reading and answers `{}` like any other failure."""
    monkeypatch.setattr(reader, "MAX_METADATA_BYTES", 64 * 1024)
    counter = {"bytes": 0}
    with respx.mock(assert_all_called=False) as router:
        router.get(SERVICE_BASE).mock(
            side_effect=_flood(counter, content_type="application/json")
        )
        router.get(SERVICE_QUERY).mock(side_effect=_paged_service(total=3, page_size=1000))
        candidate = read_candidate(SERVICE_BASE)

    assert len(candidate.frame) == 3
    assert candidate.facts["max_record_count"] is None
    assert counter["bytes"] < 4 * 64 * 1024


def test_arcgis_last_edit_date_becomes_the_vintage():
    # 2026-06-30T00:00:00Z in epoch milliseconds.
    with respx.mock(assert_all_called=False) as router:
        _mock_service(
            router,
            _paged_service(total=2, page_size=1000),
            metadata={"name": "Wards", "editingInfo": {"lastEditDate": 1782777600000}},
        )
        candidate = read_candidate(SERVICE_BASE)

    assert candidate.vintage is not None
    assert "2026-06-30" in candidate.vintage
    assert "PIP-L017" not in _codes(candidate)


def test_a_service_with_no_editing_info_gets_the_no_vintage_warning():
    """Verified against Cook County's own politicalBoundary/MapServer/2, which
    publishes serviceItemId and currentVersion and no editingInfo at all."""
    with respx.mock(assert_all_called=False) as router:
        _mock_service(
            router,
            _paged_service(total=2, page_size=1000),
            metadata={"currentVersion": 10.91, "serviceItemId": "abc123",
                      "name": "Municipality"},
        )
        candidate = read_candidate(SERVICE_BASE)

    assert candidate.vintage is None
    assert "PIP-L017" in _codes(candidate)


def test_a_login_page_is_told_apart_from_map_data():
    html = (
        "<!DOCTYPE html><html><head><title>Sign In</title></head>"
        "<body><form>Password: <input type=password></form></body></html>"
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(SERVICE_BASE).mock(return_value=httpx.Response(200, json={}))
        router.get(SERVICE_QUERY).mock(
            return_value=httpx.Response(
                200, text=html, headers={"content-type": "text/html"}
            )
        )
        with pytest.raises(CandidateError) as raised:
            read_candidate(SERVICE_BASE)

    finding = raised.value.finding
    assert finding.code == "PIP-L014"
    assert finding.detail["reason"] == "sign_in_page"
    assert "not public" in finding.specifics


def test_an_ordinary_web_page_is_told_apart_from_a_network_failure():
    with respx.mock(assert_all_called=False) as router:
        router.get(SERVICE_BASE).mock(return_value=httpx.Response(200, json={}))
        router.get(SERVICE_QUERY).mock(
            return_value=httpx.Response(
                200,
                text="<html><body><h1>Open Data Portal</h1></body></html>",
                headers={"content-type": "text/html"},
            )
        )
        with pytest.raises(CandidateError) as raised:
            read_candidate(SERVICE_BASE)

    finding = raised.value.finding
    assert finding.detail["reason"] == "not_a_map_service"
    assert "the page describing the data" in finding.specifics
    assert "network" not in finding.specifics


def test_a_server_error_says_the_far_end_failed():
    with respx.mock(assert_all_called=False) as router:
        router.get(SERVICE_BASE).mock(return_value=httpx.Response(200, json={}))
        router.get(SERVICE_QUERY).mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        with pytest.raises(CandidateError) as raised:
            read_candidate(SERVICE_BASE)

    finding = raised.value.finding
    assert finding.code == "PIP-L014"
    assert finding.detail["reason"] == "http_error"
    assert finding.detail["status"] == 500


def test_a_transport_failure_is_named_as_the_network_not_the_address():
    with respx.mock(assert_all_called=False) as router:
        router.get(SERVICE_BASE).mock(return_value=httpx.Response(200, json={}))
        router.get(SERVICE_QUERY).mock(side_effect=httpx.ConnectTimeout("no route"))
        with pytest.raises(CandidateError) as raised:
            read_candidate(SERVICE_BASE)

    finding = raised.value.finding
    assert finding.detail["reason"] == "network_failed"
    assert "network" in finding.specifics
    assert "not with the address itself" in finding.specifics


def test_an_arcgis_error_payload_arriving_as_http_200():
    """ArcGIS reports its own failures with a 200 and an {"error": ...} body —
    the same habit app.geocoding.arcgis guards against."""
    with respx.mock(assert_all_called=False) as router:
        router.get(SERVICE_BASE).mock(return_value=httpx.Response(200, json={}))
        router.get(SERVICE_QUERY).mock(
            return_value=httpx.Response(
                200,
                json={
                    "error": {
                        "code": 400,
                        "message": "Invalid or missing input parameters.",
                        "details": [],
                    }
                },
            )
        )
        with pytest.raises(CandidateError) as raised:
            read_candidate(SERVICE_BASE)

    finding = raised.value.finding
    assert finding.code == "PIP-L014"
    assert finding.detail["reason"] == "service_error"
    assert finding.detail["service_code"] == 400
    assert "Invalid or missing input parameters." in finding.specifics


def test_a_service_address_without_a_layer_number_is_refused_before_any_request():
    with respx.mock(assert_all_called=False) as router:
        with pytest.raises(CandidateError) as raised:
            read_candidate("https://gis.example.gov/rest/services/wards/MapServer")
        assert not router.calls  # refused on the address alone

    assert raised.value.finding.detail["reason"] == "service_not_layer"


def test_a_token_in_the_address_never_reaches_the_facts():
    """SPEC §9: nothing may carry a token into anything that is shown.

    `facts` is documented as going to the browser, and it held the operator's
    pasted address verbatim — `?token=...` and all — while every other field on
    this path was already careful. The token still goes out with the request,
    because the far end asked for it; it does not come back in anything
    rendered, logged or pasted into a bug report.
    """
    handler = _paged_service(total=2, page_size=1000)
    with respx.mock(assert_all_called=False) as router:
        _mock_service(router, handler)
        candidate = read_candidate(
            f"{SERVICE_BASE}?token=SUPER-SECRET-TOKEN-123&where=1%3D1"
        )
        request = next(
            call.request
            for call in router.calls
            if call.request.url.path.endswith("/query")
        )
        assert request.url.params["token"] == "SUPER-SECRET-TOKEN-123"  # still sent

    shown = json.dumps(candidate.facts)
    assert "SUPER-SECRET-TOKEN-123" not in shown
    assert "token" not in shown
    assert candidate.facts["source_url"] == SERVICE_BASE


def test_a_refusal_never_echoes_the_query_string_or_a_password():
    """The other half of the same leak: `detail["url"]` on the service_not_layer
    refusal was the raw address too, and an address can carry a password in its
    userinfo as well as a token in its query."""
    with respx.mock(assert_all_called=False) as router:
        with pytest.raises(CandidateError) as raised:
            read_candidate(
                "https://operator:hunter2@gis.example.gov/rest/services/wards"
                "/MapServer?token=SUPER-SECRET-TOKEN-123"
            )
        assert not router.calls

    finding = raised.value.finding
    shown = json.dumps(finding.to_dict())
    assert "hunter2" not in shown
    assert "SUPER-SECRET-TOKEN-123" not in shown
    assert (
        finding.detail["url"]
        == "https://gis.example.gov/rest/services/wards/MapServer"
    )


def test_a_full_query_address_is_accepted_and_forced_to_geojson():
    handler = _paged_service(total=2, page_size=1000)
    with respx.mock(assert_all_called=False) as router:
        _mock_service(router, handler)
        candidate = read_candidate(
            f"{SERVICE_QUERY}?where=WARD%3E5&outFields=WARD&f=json"
        )
        request = next(
            call.request
            for call in router.calls
            if call.request.url.path.endswith("/query")
        )
        assert request.url.params["where"] == "WARD>5"  # the operator's filter kept
        assert request.url.params["outFields"] == "WARD"
        assert request.url.params["f"] == "geojson"  # but the format is ours

    assert len(candidate.frame) == 2


@pytest.mark.parametrize(
    "address",
    [
        "ftp://gis.example.gov/wards.zip",
        "file:///etc/passwd",
        "gopher://example.org/1/wards",
    ],
)
def test_only_http_and_https_addresses_are_fetched(address):
    with respx.mock(assert_all_called=False) as router:
        with pytest.raises(CandidateError) as raised:
            read_candidate(address)
        assert not router.calls

    finding = raised.value.finding
    assert finding.code == "PIP-L014"
    assert finding.detail["reason"] == "unsupported_scheme"
    assert finding.detail["scheme"] == address.split(":", 1)[0]


def test_a_service_returning_no_features_still_yields_a_frame_for_the_checks():
    """Zero areas is PIP-L005's business, and PIP-L005 needs a frame to say it
    about. An empty answer is not a read failure."""
    with respx.mock(assert_all_called=False) as router:
        _mock_service(router, _paged_service(total=0, page_size=1000))
        candidate = read_candidate(SERVICE_BASE)

    assert len(candidate.frame) == 0
    codes = [
        found.code
        for found in validate_candidate(
            candidate.frame,
            context=candidate.to_context(layer_id="wards", display_name="Wards"),
        )
    ]
    assert "PIP-L005" in codes


# --------------------------------------------------------------------------
# facts, and the handover to F8-T4
# --------------------------------------------------------------------------


def test_facts_are_json_serializable_on_every_path(tmp_path):
    """`facts` is rendered in a browser. A numpy int64 that survives this far
    blows up at encoding time, a long way from whatever produced it."""
    geojson = tmp_path / "wards.geojson"
    geojson.write_text(
        json.dumps({"type": "FeatureCollection", "features": [_feature(1)]})
    )
    gpkg = tmp_path / "layers.gpkg"
    _frame(rows=2, columns={"ward": [1, 2]}).to_file(
        gpkg, layer="wards", driver="GPKG"
    )

    candidates = [
        read_candidate(_good_zip(tmp_path)),
        read_candidate(geojson),
        read_candidate(gpkg),
    ]
    with respx.mock(assert_all_called=False) as router:
        _mock_service(router, _paged_service(total=2, page_size=1000))
        candidates.append(read_candidate(SERVICE_BASE))

    for candidate in candidates:
        encoded = json.dumps(candidate.facts)  # must not raise
        assert json.loads(encoded)["source_kind"] == candidate.source_kind
        # And every finding travels the same way.
        json.dumps([found.to_dict() for found in candidate.findings])
        candidate.cleanup()


def test_facts_describe_the_columns_with_real_sample_values(tmp_path):
    shp = _write_shapefile(
        tmp_path / "loose",
        "wards",
        columns={"ward": ["1", "2", "3"], "empty": [None, None, None]},
    )

    candidate = read_candidate(shp)
    by_name = {column["name"]: column for column in candidate.facts["columns"]}

    assert by_name["ward"]["samples"] == ["1", "2", "3"]
    assert by_name["ward"]["filled_count"] == 3
    assert by_name["empty"]["samples"] == []
    assert "geometry" not in by_name
    assert candidate.facts["feature_count"] == 3
    assert candidate.facts["geometry_types"] == {"Polygon": 3}
    assert len(candidate.facts["bounds"]) == 4


def test_to_context_carries_what_only_the_reader_knows(tmp_path):
    candidate = read_candidate(_good_zip(tmp_path))

    context = candidate.to_context(
        layer_id="wards_2026",
        display_name="Wards 2026",
        attribute_columns=("ward",),
    )

    assert context.source_kind == SOURCE_SHAPEFILE
    assert context.vintage is None
    assert context.source_files == candidate.source_files
    assert context.attribute_columns == ("ward",)
    candidate.cleanup()


def test_nothing_at_all_was_sent():
    with pytest.raises(CandidateError) as raised:
        read_candidate([])
    assert raised.value.finding.detail["reason"] == "no_input"
