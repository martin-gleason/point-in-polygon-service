"""F8-T2 — the reader: whatever the operator supplied, turned into one frame of
shapes the checks in `app.admin.validate` can look at.

`read_candidate` is the only thing in the installer that touches bytes, zip
archives and web addresses. It accepts the four things a volunteer actually
arrives with — a zipped shapefile, a handful of loose shapefile pieces, a
.geojson, or the web address of a published map service — plus a .gpkg, which
this service already stores its own layers in. It hands back a `Candidate`: the
frame exactly as it was found, where it came from, what could be learned about
how old it is, the observations the reader itself made, and a plain bag of
`facts` a browser can render.

Blocking versus riding along
    A blocking failure means there is no frame at all, so there is nothing for
    the checks to look at and nothing to draw on a preview map. Those raise
    `CandidateError`, which carries the `Finding` — PIP-L001, PIP-L002,
    PIP-L012, PIP-L013, PIP-L014, exactly the five codes `app.admin.validate`
    leaves to this module. Everything the reader notices that still leaves a
    usable frame (PIP-L017, PIP-L018) rides along in `Candidate.findings` for
    F8-T4 to merge with the validator's list.

    `validate.validate_candidate` fires PIP-L017 too, from the same absence of a
    vintage. That is deliberate duplication of a *code*, not of a problem: the
    reader's copy is the better one — it knows the .dbf's write date and whether
    a .shp.xml was sitting beside the file — so when merging, keep the reader's
    finding for a code both produced.

The CRS is passed through untouched
    Nothing here reprojects, and nothing here guesses. The validator's two
    hardest checks (PIP-L003, PIP-L004) work by comparing what the source
    *declares* against the numbers it *holds*, and both are destroyed by a
    helpful reader that quietly fills in EPSG:4326. What the source said is what
    reaches the checks — including "nothing at all", which is PIP-L003's whole
    subject.

    The one place a declaration is written down rather than read is an ArcGIS
    address carrying `outSR=`. That parameter tells the service to reproject
    before answering, so it is a statement about what the numbers coming back
    are; GeoJSON has nowhere to carry it, and GDAL reads a GeoJSON that declares
    nothing as EPSG:4326. Recording it is therefore the opposite of guessing —
    see `_declare_requested_out_sr`, which refuses any `outSR` it cannot record
    faithfully rather than approximating one.

Nothing from an untrusted archive is executed, and almost nothing is written
    A zip is bounded by its own size and the entry count in its tail before it
    is so much as opened (`_bound_archive_before_opening`, because parsing a
    zip's directory is itself an expense a stranger chooses), measured from that
    directory before a single byte is decompressed (`_enforce_archive_caps`),
    members that address anywhere
    but the extraction folder are refused outright (`_reject_escaping_members`),
    and only the five shapefile extensions are ever written to disk. No member
    is opened, imported, or run.

ArcGIS / ArcPy equivalent
    This is the open-source stand-in for the "add data" half of ArcGIS Pro:
    `arcpy.conversion.JSONToFeatures` for a GeoJSON payload, the Catalog pane's
    shapefile reader (which likewise refuses a .shp whose .dbf or .shx is
    absent), `arcpy.Describe(...).dateModified` and a GeoPackage's
    `gpkg_contents.last_change` for provenance, and
    `arcpy.FeatureSet.load(url)` / "Add Data From Path" against an ArcGIS REST
    layer — including the paging that `arcpy` hides and that this module has to
    do by hand, because a truncated boundary layer validates perfectly.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import stat
import struct
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qsl, urlparse, urlunparse

import geopandas as gpd
import httpx

from app.admin.codes import Finding, build_finding, sort_findings

# Reducing a value to something `json.dumps` accepts is a decision — which
# numpy scalar becomes which Python scalar — and there should be exactly one of
# it in the installer. `facts` travels to the same browser the findings do, out
# of the same pandas objects, so it goes through the same reduction rather than
# a second one that could disagree.
from app.admin.codes import _json_safe as _json_safe
from app.admin.validate import (
    SOURCE_ARCGIS_REST,
    SOURCE_GEOJSON,
    SOURCE_GEOPACKAGE,
    SOURCE_SHAPEFILE,
    CandidateContext,
    InstalledLayer,
)

# --------------------------------------------------------------------------
# what a shapefile is made of
# --------------------------------------------------------------------------

# The only extensions ever written out of an archive. Everything else in a zip
# — a readme, a spreadsheet, a script, a nested archive — is left inside it.
SHAPEFILE_EXTENSIONS = (".shp", ".dbf", ".shx", ".prj", ".cpg")

# Without these three a shapefile does not open: .shp holds the outlines, .dbf
# the table of names and numbers, .shx the index that ties one to the other.
# .prj is NOT here on purpose — a shapefile with no .prj reads perfectly well
# and simply comes back with no CRS, which is PIP-L003's business. Reporting it
# here as well would tell the operator the same problem twice under two codes.
REQUIRED_SHAPEFILE_EXTENSIONS = (".shp", ".dbf", ".shx")

# A sibling ESRI metadata document. It is the one place a shapefile set can
# carry a real publication date, so its presence is worth telling the operator
# about even though this module does not parse it.
SHAPEFILE_METADATA_SUFFIX = ".shp.xml"

GEOJSON_SUFFIXES = (".geojson", ".json")
GEOPACKAGE_SUFFIXES = (".gpkg",)

# --------------------------------------------------------------------------
# archive caps — measured from the zip directory, before anything is unpacked
# --------------------------------------------------------------------------
#
# Why these numbers, and why a real boundary file never reaches them.
#
# `validate.LARGE_FEATURE_COUNT` / `LARGE_VERTEX_COUNT` already say what a large
# layer looks like to this service: 20,000 areas, 1,000,000 corner points. A
# million corner points is 16 MB of coordinates in a .shp (two 8-byte doubles
# each), and 20,000 rows of a generously wide .dbf is a few MB more. Cook
# County's municipalities layer — the biggest thing this service ships — is
# under 10 MB unzipped. So:
#
#   * 512 MiB total is roughly thirty times the largest layer the validator will
#     accept without even a warning. A zip that unpacks past it is not a
#     boundary layer that happens to be big; it is a whole state's parcel
#     fabric, an entire portal dump, or something built to fill a disk.
#   * 256 MiB for a single member is the same argument applied to the one file
#     that could plausibly be large on its own (the .shp).
#   * 200:1 compression, judged per member. Measured on the two layers this
#     service actually ships, written out as shapefiles and deflated: Chicago
#     police districts came to 0.45 MB unpacked at 1.4:1 overall with a worst
#     single member of 18.9:1, and Cook County municipalities to 2.94 MB at
#     1.3:1 overall with a worst member of 10.9:1. Packed binary coordinates
#     barely compress at all; the worst member in both cases is the .prj or the
#     .dbf, whose fixed-width padding is the only compressible thing in the set.
#     200:1 is therefore an order of magnitude clear of the worst real member,
#     while a decompression bomb — long runs of a single byte — starts around
#     1000:1 for one pass and goes up from there.
#
# The ratio is only judged on members big enough for the number to mean
# anything: a 300-byte .prj that deflates to 120 bytes has a meaningless ratio,
# and a small member cannot fill a disk however well it compresses.
MAX_TOTAL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_MEMBER_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200.0
RATIO_CHECK_FLOOR_BYTES = 64 * 1024
# A shapefile set is five files. A few hundred allows a portal's habit of
# zipping a whole folder of documentation alongside; tens of thousands of
# entries is a directory bomb — cheap to build, and expensive to so much as
# look at, because `zipfile.ZipFile()` turns every entry in the central
# directory into a Python object before any code here is reached. 300,000 empty
# members fit in a 30 MB zip and cost about 175 MB of objects to parse; the cap
# is therefore judged on the count the archive *declares* in its end-of-central
# -directory record, before the parse — see `_declared_member_count`.
MAX_ARCHIVE_MEMBERS = 2_000
# The outer bound on the file itself, checked with one stat() before the archive
# is opened at all. Nothing packed larger than this can unpack to less than
# `MAX_TOTAL_UNCOMPRESSED_BYTES` — compression does not make data bigger, and a
# stored member is 1:1 — so an archive over this size is already refused by the
# total cap, and refusing it here costs a stat instead of a parse. It also
# bounds the tail scan `_declared_member_count` does.
MAX_ARCHIVE_BYTES = MAX_TOTAL_UNCOMPRESSED_BYTES
# Extraction is streamed and counted rather than trusted, because every size in
# a zip directory is written by whoever built the zip. The header is what we
# refuse on; this is what we stop at if the header lied.
EXTRACT_CHUNK_BYTES = 1024 * 1024

# --------------------------------------------------------------------------
# ArcGIS REST
# --------------------------------------------------------------------------

# Mirrors `scripts/build_data.py`'s USER_AGENT — same project, same courtesy,
# one recognisable string for a portal operator reading their access log. Only
# the parenthetical differs, to say which part of the tool is calling.
USER_AGENT = "point-in-polygon-service/0.1 (layer installer; AGPLv3 FOSS)"

# The query `scripts/build_data.py` already uses against these services.
ARCGIS_BASE_QUERY = {"where": "1=1", "outFields": "*", "f": "geojson"}

# Asked for when the service does not publish its own maxRecordCount. 1000 is
# what `build_data.AddressPointSource` uses against this same county's server.
ARCGIS_PAGE_SIZE = 1000

# The most this reader will ever ask for in one request, whoever named the
# number. A published `maxRecordCount` is a claim by the far end, and a service
# is free to publish 1,000,000,000: the page it then answers with is held whole
# in memory, fingerprinted for the repeat-page guard, and accumulated — so a
# number taken on trust here is the far end choosing this process's memory
# ceiling. Ten thousand is ten times what `build_data` asks Cook County for and
# half of `validate.LARGE_FEATURE_COUNT`, so a real layer still arrives in a
# handful of requests. Asking for less than was published costs round trips, not
# features: the walk only ever ends on an empty page, the flag, or a short page
# whose size this reader did not choose (see `_arcgis_page_size`).
MAX_ARCGIS_PAGE_SIZE = 10_000

# The most of any single HTTP answer this reader will hold. `client.get` reads
# the whole body before returning, so without this a 300 MB sign-in page is
# fully buffered and decoded in order to be described by its first 200
# characters. 64 MiB is past any honest answer: a whole layer at
# `validate.LARGE_VERTEX_COUNT` — a million corner points — is about 40 MB of
# GeoJSON text, and that is the *entire* layer rather than one page of it.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
# A layer's own description is a few kilobytes of JSON. It is fetched
# best-effort and any failure means `{}`, so the bound can be tight.
MAX_METADATA_BYTES = 4 * 1024 * 1024
RESPONSE_CHUNK_BYTES = 64 * 1024

# Stops on a service that answers but never finishes: a layer far larger than
# anything this service can hold in memory, or one that ignores resultOffset in
# a way the repeat-page guard somehow misses. Both are refusals, never a partial
# install. 500,000 areas is twenty-five times `validate.LARGE_FEATURE_COUNT`.
MAX_ARCGIS_FEATURES = 500_000
MAX_ARCGIS_PAGES = 1_000

ARCGIS_TIMEOUT_SECONDS = 60.0

# Recognises `.../MapServer/2` and `.../FeatureServer/0` — a service plus a
# layer index, which is what an operator copies out of a portal's page.
_LAYER_URL_PATTERN = re.compile(r"/(?:map|feature)server/\d+$", re.IGNORECASE)
_SERVICE_URL_PATTERN = re.compile(r"/(?:map|feature)server$", re.IGNORECASE)

# --------------------------------------------------------------------------
# facts
# --------------------------------------------------------------------------

# How many values to show per column beside the preview. Enough to recognise a
# ward number or a district name; few enough that a wide file stays readable.
MAX_COLUMN_SAMPLES = 3

# The shapefile format stores every field name in a fixed 10-byte slot. A name
# sitting exactly on that limit is the fingerprint of a cut (PIP-L018).
SHAPEFILE_NAME_LIMIT = 10


class CandidateError(Exception):
    """A candidate that could not be read at all, carrying the operator's finding.

    `finding` is always blocking. There is no frame behind this exception — that
    is exactly what makes it blocking rather than a warning riding along in
    `Candidate.findings`.
    """

    def __init__(self, finding: Finding):
        super().__init__(finding.message)
        self.finding = finding


def _blocking(code: str, specifics: str, **detail: Any) -> CandidateError:
    return CandidateError(build_finding(code, specifics=specifics, detail=detail))


@dataclass(frozen=True)
class Candidate:
    """One readable layer, as the reader found it.

    `frame` carries whatever coordinate reference system the source declared,
    including none at all — see the module docstring.

    `workspace` is the temporary folder anything unpacked from a zip was written
    to, or None when nothing was written. The caller owns it: F8-T4 keeps it
    alive while the operator looks at the preview and calls `cleanup()` when
    they either install the layer or walk away. `cleanup()` is safe to call more
    than once and never raises.
    """

    frame: gpd.GeoDataFrame
    source_kind: str
    source_files: tuple[str, ...] = ()
    vintage: str | None = None
    findings: tuple[Finding, ...] = ()
    facts: dict[str, Any] = field(default_factory=dict)
    workspace: Path | None = None

    def cleanup(self) -> None:
        """Delete anything this read unpacked. Idempotent, and never raises."""
        if self.workspace is not None:
            shutil.rmtree(self.workspace, ignore_errors=True)

    def to_context(
        self,
        *,
        layer_id: str,
        display_name: str,
        attribute_columns: Sequence[str] = (),
        installed_layers: Sequence[InstalledLayer] = (),
    ) -> CandidateContext:
        """The `CandidateContext` for this candidate, with what the reader learned
        already filled in.

        The three fields the validator cannot work out for itself — where the
        file came from, how old it says it is, which pieces arrived — are
        exactly the three the reader knows. Handing them over through this method
        rather than by hand is what stops F8-T4 from constructing a context that
        leaves `source_kind` at `SOURCE_UNKNOWN`, which would silently disable
        PIP-L018 and reword PIP-L003 for a format this is not.
        """
        return CandidateContext(
            layer_id=layer_id,
            display_name=display_name,
            attribute_columns=tuple(attribute_columns),
            installed_layers=tuple(installed_layers),
            source_kind=self.source_kind,
            vintage=self.vintage,
            source_files=self.source_files,
        )


def read_candidate(
    source: str | Path | Sequence[str | Path],
    *,
    source_files: Sequence[str] | None = None,
    select: str | None = None,
) -> Candidate:
    """Read whatever the operator supplied into one `Candidate`.

    `source` is one of:

    * a path to a .zip holding a shapefile set,
    * a path to a .shp (its companions are collected from beside it), a
      .geojson, or a .gpkg,
    * a sequence of paths — the loose shapefile pieces an operator selects or
      drags in together, which need not sit in the same folder,
    * an http or https address of a published ArcGIS REST layer.

    `source_files` overrides the names used when talking to the operator. An
    upload arrives on disk as something like `tmp8f2a1c`, and no message should
    ever say that; pass the names the operator actually sent.

    `select` names one shapefile stem when the source holds several. Reading is
    refused rather than guessed in that case (see `_choose_shapefile`), and
    `select` is how the caller answers the question the refusal asked.

    Raises `CandidateError` when there is no frame to hand back.
    """
    if isinstance(source, (list, tuple, set, frozenset)):
        paths = [Path(item) for item in source]
        if not paths:
            raise _blocking(
                "PIP-L001",
                "No file arrived at all — nothing was sent to read.",
                reason="no_input",
            )
        return _read_paths(paths, source_files=source_files, select=select)

    if isinstance(source, str):
        scheme = _url_scheme(source)
        if scheme is not None:
            if scheme not in ("http", "https"):
                raise _blocking(
                    "PIP-L014",
                    f"The address you gave starts with {scheme}:, and this tool "
                    f"only fetches addresses starting with http: or https:.",
                    reason="unsupported_scheme",
                    scheme=scheme,
                )
            return _read_arcgis(source)

    return _read_paths([Path(source)], source_files=source_files, select=select)


# --------------------------------------------------------------------------
# dispatch over local files
# --------------------------------------------------------------------------


def _read_paths(
    paths: list[Path],
    *,
    source_files: Sequence[str] | None,
    select: str | None,
) -> Candidate:
    """Send a set of local paths to the reader for the format they are."""
    names = _display_names(paths, source_files)
    for path, name in zip(paths, names):
        if not path.exists():
            raise _blocking(
                "PIP-L001",
                f"The file {name!r} is not there to read.",
                reason="missing_file",
                name=name,
            )

    # Judged on the name the operator's file had, never on the path on disk: an
    # upload arrives as `tmp8f2a1c` with no extension at all, so the on-disk
    # suffix answers "what kind of file is this?" with silence. The supplied
    # name is the operator's own answer to that question; the on-disk suffix is
    # only the fallback for a path this process chose itself.
    suffixes = {
        Path(name).suffix.lower() or path.suffix.lower()
        for path, name in zip(paths, names)
    }

    if len(paths) == 1:
        only = paths[0]
        suffix = Path(names[0]).suffix.lower() or only.suffix.lower()
        if suffix == ".zip":
            return _read_zip(only, archive_name=names[0], select=select)
        if suffix in GEOJSON_SUFFIXES:
            return _read_geojson(only, name=names[0])
        if suffix in GEOPACKAGE_SUFFIXES:
            return _read_geopackage(only, name=names[0], select=select)
        if suffix in SHAPEFILE_EXTENSIONS:
            # One piece of a shapefile set was named; the rest of the set is
            # normally sitting right beside it. Collecting the siblings here is
            # what makes "drag the .shp in" work, and if they are genuinely not
            # there PIP-L002 says exactly which are missing. The companions are
            # named by their own on-disk names — the caller only told us about
            # the one file it knew about.
            #
            # Only when the file on disk really is that piece, though. A single
            # browser upload is one anonymous temporary file with no companions
            # anywhere near it; the honest answer there is the set it is missing
            # (PIP-L002), not whatever else happens to share its temporary
            # folder.
            if only.suffix.lower() == suffix:
                siblings = _siblings_of(only)
                return _read_shapefile_parts(
                    siblings,
                    supplied_names=[path.name for path in siblings],
                    select=select,
                )
            return _read_shapefile_parts(
                [only], supplied_names=[names[0]], select=select
            )
        raise _blocking(
            "PIP-L001",
            f"The file {names[0]!r} is not one of the kinds of map file this "
            f"tool reads: a shapefile set, a .geojson, a .gpkg, or a .zip "
            f"holding a shapefile set.",
            reason="unsupported_extension",
            name=names[0],
            suffix=suffix,
        )

    # Several paths. A shapefile set is the only format that arrives as more
    # than one file, so anything else in the pile is a mistake worth naming.
    #
    # Judged on the name the operator's file had, never on the path on disk: an
    # upload arrives as `tmp8f2a1c` with no extension at all, and reading that
    # as "not a shapefile piece" would reject every browser upload there is.
    stray = sorted(
        name
        for name in names
        if Path(name).suffix.lower() not in SHAPEFILE_EXTENSIONS
        and not name.lower().endswith(SHAPEFILE_METADATA_SUFFIX)
    )
    if stray:
        raise _blocking(
            "PIP-L001",
            f"More than one file arrived, which only makes sense for a shapefile "
            f"set, and {_join_names(stray)} "
            f"{'are' if len(stray) > 1 else 'is'} not part of one. Send a single "
            f".geojson or .gpkg on its own, or send the shapefile pieces "
            f"together.",
            reason="mixed_input",
            unexpected=stray,
            suffixes=sorted(suffixes),
        )
    return _read_shapefile_parts(
        paths,
        supplied_names=names,
        select=select,
        metadata_names=[
            name for name in names if name.lower().endswith(SHAPEFILE_METADATA_SUFFIX)
        ],
    )


def _siblings_of(part: Path) -> list[Path]:
    """Every shapefile piece sitting beside `part` under the same stem."""
    stem = part.name[: -len(part.suffix)] if part.suffix else part.name
    found = [
        candidate
        for candidate in sorted(part.parent.iterdir())
        if candidate.is_file()
        and candidate.suffix.lower() in SHAPEFILE_EXTENSIONS
        and candidate.name[: -len(candidate.suffix)] == stem
    ]
    # If the folder cannot be listed for any reason, the named piece alone is
    # still the honest answer — PIP-L002 then says what is missing.
    return found or [part]


# --------------------------------------------------------------------------
# (1) zipped shapefile
# --------------------------------------------------------------------------


def _read_zip(zip_path: Path, *, archive_name: str, select: str | None) -> Candidate:
    """A .zip holding a shapefile set — the way portals hand these out.

    The order here is the whole safety argument: bound the file and the number
    of entries it declares *before* it is opened, measure from the directory,
    refuse anything addressed outside the extraction folder, then write only the
    five shapefile extensions. Nothing is decompressed until all of that has
    passed, and nothing outside the whitelist is decompressed at all.

    The first step is not ceremony. `zipfile.ZipFile()` builds a `ZipInfo` for
    every entry in the central directory inside its constructor, so a cap
    enforced on `infolist()` is enforced after the cost it exists to prevent —
    300,000 empty members in a 30 MB zip are refused correctly and cost about
    175 MB of Python objects to refuse. `_enforce_archive_caps` still judges the
    parsed directory (it is what the sizes and ratios come from); what it can no
    longer be asked to do is be the first line of defence against the count.

    A shapefile set inside an archive is identified by the folder it sits in as
    well as its stem — see `_shapefile_sets`. Judging on the bare filename
    instead lets one zip hold `2024_official/wards.shp` and `zzz_extra/wards.shp`
    and have them read as a single set: no ambiguity is reported, the later
    member wins, and a layer nobody chose installs without a word. Worse, a set
    can be assembled from pieces of two: real outlines from one folder, a table
    of names from another, which draws a correct-looking map carrying somebody
    else's labels.
    """
    _bound_archive_before_opening(zip_path, archive_name=archive_name)
    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as error:
        raise _blocking(
            "PIP-L001",
            f"The file {archive_name!r} ends in .zip but does not open as one — "
            f"it may have been cut short while downloading.",
            reason="unreadable_archive",
            name=archive_name,
            error=type(error).__name__,
        ) from error

    with archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        _enforce_archive_caps(members, archive_name=archive_name)
        _reject_escaping_members(members, archive_name=archive_name)

        sets = _shapefile_sets(members, archive_name=archive_name)
        shapefiles = sorted(key for key, pieces in sets.items() if ".shp" in pieces)
        if not shapefiles:
            inside = sorted(
                {
                    PurePosixPath(info.filename).suffix.lower() or "(no extension)"
                    for info in members
                }
            )
            raise _blocking(
                "PIP-L001",
                f"There is no .shp file inside {archive_name!r}, so there is no "
                f"shapefile in it to read. What it holds is "
                f"{_join_names(inside)}.",
                reason="no_shapefile_in_archive",
                name=archive_name,
                suffixes_inside=inside,
            )
        chosen = _choose_shapefile(shapefiles, select=select, where=archive_name)
        pieces = sets[chosen]
        folder = PurePosixPath(chosen).parent
        stem = PurePosixPath(chosen).name
        # Noted from the archive's directory, never unpacked: a .shp.xml is
        # untrusted XML and is outside the extraction whitelist. Its presence is
        # all PIP-L017 needs. Only the one sitting in the chosen set's own
        # folder counts — a document beside a different `wards.shp` describes
        # that one, not this one.
        metadata_names = [
            PurePosixPath(info.filename).name
            for info in members
            if info.filename.lower().endswith(SHAPEFILE_METADATA_SUFFIX)
            and PurePosixPath(info.filename).parent == folder
        ]

        workspace = Path(tempfile.mkdtemp(prefix="pip-layer-"))
        try:
            written = _extract_shapefile(
                archive, pieces, stem=stem, into=workspace, archive_name=archive_name
            )
        except BaseException:
            shutil.rmtree(workspace, ignore_errors=True)
            raise

    try:
        return _read_shapefile_parts(
            written,
            supplied_names=[path.name for path in written],
            select=None,
            workspace=workspace,
            container=archive_name,
            metadata_names=metadata_names,
        )
    except BaseException:
        shutil.rmtree(workspace, ignore_errors=True)
        raise


def _shapefile_sets(
    members: list[zipfile.ZipInfo], *, archive_name: str
) -> dict[str, dict[str, zipfile.ZipInfo]]:
    """Every shapefile set in the archive, keyed by folder-and-stem.

    A shapefile set is the run of files that share a stem *inside one folder*.
    That is the whole identity, and both halves are load-bearing: a zip is free
    to hold `2024_official/wards.shp` and `zzz_extra/wards.shp`, and those are
    two different layers by every meaning the word has. Keying on the bare
    filename merges them, which is not a cosmetic mistake — it means
    `_choose_shapefile` is never asked the question, one of the two silently
    wins, and (because the pieces merge one extension at a time) the winner can
    be a chimera: outlines from the folder the operator meant, table of names
    from the folder they did not. That set opens, validates, previews as a
    correct map, and is mislabeled forever.

    So members are never merged across folders, and a stem that appears twice
    *within* one folder — a zip may carry two entries under one name — is
    refused rather than resolved, because there is no honest way to say which
    one the operator asked for.

    The key is the archive-relative path without its extension (`wards`,
    `2024_official/wards`), which is what `_choose_shapefile` shows the operator
    and what `select=` names.

    ArcGIS / ArcPy equivalent
        The Catalog pane lists a workspace's shapefiles by their full catalog
        path for exactly this reason: two `wards.shp` in two folders are two
        entries in the tree, never one.
    """
    sets: dict[str, dict[str, zipfile.ZipInfo]] = {}
    for info in members:
        member_path = PurePosixPath(info.filename)
        suffix = member_path.suffix.lower()
        if suffix not in SHAPEFILE_EXTENSIONS:
            continue
        stem = member_path.name[: -len(suffix)]
        if not stem:
            continue
        folder = member_path.parent
        key = stem if str(folder) == "." else f"{folder}/{stem}"
        pieces = sets.setdefault(key, {})
        if suffix in pieces:
            raise _blocking(
                "PIP-L001",
                f"Inside {archive_name!r} there are two items both recorded as "
                f"{info.filename!r}. A shapefile set has one of each piece, and "
                f"there is no way to tell which of these two was meant.",
                reason="duplicate_member",
                member=info.filename,
                shapefile=key,
                extension=suffix,
            )
        pieces[suffix] = info
    return sets


def _bound_archive_before_opening(zip_path: Path, *, archive_name: str) -> None:
    """PIP-L012 — refuse an archive on its size and its declared entry count,
    before `zipfile.ZipFile()` parses anything.

    Two numbers, both read without decompressing a byte and without building a
    single `ZipInfo`:

    * the file's own size, one stat();
    * the number of entries the archive says it has, from the end-of-central
      -directory record at its tail — 22 bytes, or the Zip64 record it points at
      when there are more than 65,535 entries.

    The declared count is written by whoever built the archive and so cannot be
    trusted as a *lower* bound. It does not need to be: CPython's zip reader
    walks the central directory exactly `total_entries` times, so an archive
    that under-reports its own count bounds the parse by that lie, and one that
    reports honestly is refused here. Either way the parse can no longer be made
    to build hundreds of thousands of objects before this module gets to speak.

    An archive whose tail cannot be read as a zip at all is left alone: opening
    it is what produces the honest "this does not open as a zip" message.

    ArcGIS / ArcPy equivalent
        None — ArcGIS Pro unpacks a .zip through the operating system's shell
        and inherits whatever it does with a hostile one. This bound is a thing
        a web service has to do and a desktop tool never had to.
    """
    try:
        size = zip_path.stat().st_size
    except OSError:  # pragma: no cover - the caller already proved it exists
        return
    if size > MAX_ARCHIVE_BYTES:
        raise _blocking(
            "PIP-L012",
            f"The file {archive_name!r} is {_megabytes(size)}, and this tool "
            f"opens archives up to {_megabytes(MAX_ARCHIVE_BYTES)}.",
            reason="archive_too_large",
            name=archive_name,
            archive_bytes=size,
            archive_limit=MAX_ARCHIVE_BYTES,
        )

    declared = _declared_member_count(zip_path)
    if declared is not None and declared > MAX_ARCHIVE_MEMBERS:
        raise _blocking(
            "PIP-L012",
            f"There are {declared:,} items inside {archive_name!r}, and this "
            f"tool opens at most {MAX_ARCHIVE_MEMBERS:,}. A shapefile set is "
            f"five files.",
            reason="too_many_members",
            member_count=declared,
            member_limit=MAX_ARCHIVE_MEMBERS,
        )


# The three records at the tail of a zip that say how many entries it has.
_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
# The end-of-central-directory record is 22 bytes plus a comment of at most
# 65,535, and the Zip64 locator that may sit in front of it is 20 more.
_EOCD_SEARCH_BYTES = 22 + 0xFFFF + 20


def _declared_member_count(zip_path: Path) -> int | None:
    """How many entries the archive's own tail says it holds, or None.

    None means "this does not look like a zip from here" — a truncated
    download, a renamed file — and the caller lets `zipfile` produce the message
    for that, since it is the one that can tell the operator their file did not
    open.
    """
    try:
        with open(zip_path, "rb") as archive:
            size = archive.seek(0, 2)
            tail_start = max(0, size - _EOCD_SEARCH_BYTES)
            archive.seek(tail_start)
            tail = archive.read()

            at = tail.rfind(_EOCD_SIGNATURE)
            if at < 0 or len(tail) - at < 22:
                return None
            (count,) = struct.unpack("<H", tail[at + 10 : at + 12])
            if count != 0xFFFF:
                return count

            # 65,535 entries is also the value that means "look in the Zip64
            # record", so a real 65,535-entry archive simply gets counted twice
            # — which is harmless, and either count is over the cap anyway.
            locator = at - 20
            if locator < 0 or tail[locator : locator + 4] != _ZIP64_LOCATOR_SIGNATURE:
                return count
            (zip64_at,) = struct.unpack("<Q", tail[locator + 8 : locator + 16])
            archive.seek(zip64_at)
            record = archive.read(56)
            if len(record) < 40 or record[:4] != _ZIP64_EOCD_SIGNATURE:
                return count
            (zip64_count,) = struct.unpack("<Q", record[32:40])
            return zip64_count
    except (OSError, struct.error):  # pragma: no cover - unreadable tail
        return None


def _enforce_archive_caps(
    members: list[zipfile.ZipInfo], *, archive_name: str
) -> None:
    """PIP-L012 — refuse an archive on what its directory claims, before unpacking.

    Every number read here comes out of the zip's central directory, which costs
    one seek and no decompression at all. That is the point: deciding whether a
    zip is safe to unpack must not require unpacking it. The claimed sizes are
    written by whoever built the archive and so cannot be trusted as *lower*
    bounds — a liar can under-report — but they are perfectly good grounds for
    refusal, and `_extract_shapefile` counts the real bytes as they are written
    for exactly the case where the header lied downward.
    """
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise _blocking(
            "PIP-L012",
            f"There are {len(members):,} items inside {archive_name!r}, and this "
            f"tool opens at most {MAX_ARCHIVE_MEMBERS:,}. A shapefile set is "
            f"five files.",
            reason="too_many_members",
            member_count=len(members),
            member_limit=MAX_ARCHIVE_MEMBERS,
        )

    total = 0
    for info in members:
        claimed = int(info.file_size)
        packed = int(info.compress_size)
        total += claimed

        if claimed > MAX_MEMBER_UNCOMPRESSED_BYTES:
            raise _blocking(
                "PIP-L012",
                f"One item inside {archive_name!r} — {info.filename!r} — unpacks "
                f"to {_megabytes(claimed)}, and this tool unpacks at most "
                f"{_megabytes(MAX_MEMBER_UNCOMPRESSED_BYTES)} from any single "
                f"item.",
                reason="member_too_large",
                member=info.filename,
                member_bytes=claimed,
                member_limit=MAX_MEMBER_UNCOMPRESSED_BYTES,
            )

        if claimed > RATIO_CHECK_FLOOR_BYTES and packed > 0:
            ratio = claimed / packed
            if ratio > MAX_COMPRESSION_RATIO:
                raise _blocking(
                    "PIP-L012",
                    f"One item inside {archive_name!r} — {info.filename!r} — "
                    f"takes up {_megabytes(packed)} while packed and claims to "
                    f"unpack to {_megabytes(claimed)}, which is {ratio:,.0f} "
                    f"times larger. Map data never swells by more than about "
                    f"{MAX_COMPRESSION_RATIO:,.0f} times.",
                    reason="compression_ratio",
                    member=info.filename,
                    packed_bytes=packed,
                    unpacked_bytes=claimed,
                    ratio=round(ratio, 1),
                    ratio_limit=MAX_COMPRESSION_RATIO,
                )

    if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise _blocking(
            "PIP-L012",
            f"Everything inside {archive_name!r} adds up to "
            f"{_megabytes(total)} once unpacked, and this tool unpacks at most "
            f"{_megabytes(MAX_TOTAL_UNCOMPRESSED_BYTES)}.",
            reason="total_too_large",
            total_bytes=total,
            total_limit=MAX_TOTAL_UNCOMPRESSED_BYTES,
        )


def _reject_escaping_members(
    members: list[zipfile.ZipInfo], *, archive_name: str
) -> None:
    """PIP-L013 — refuse any member that addresses somewhere it should not.

    Four separate ways a zip can reach outside the folder it is unpacked into,
    all judged from the member's recorded name and mode, none of them requiring
    a byte to be decompressed:

    * an absolute path (`/etc/cron.d/x`), which overwrites by address;
    * a Windows absolute path or a backslash separator (`C:\\x`, `..\\..\\x`),
      which `PurePosixPath` would otherwise read as one innocent filename;
    * `..` anywhere in the path, the classic zip-slip;
    * a symbolic link, which escapes at *follow* time rather than at write time
      — a link member pointing at `/etc` followed by a member writing "into" it
      is the two-step version of the same attack.

    Every member is judged, not only the ones this reader would extract: a zip
    carrying one of these is not a map file that happens to be malformed, and
    picking the safe-looking files out of it is not a service worth offering.
    """
    for info in members:
        name = info.filename
        mode = (info.external_attr >> 16) & 0xFFFF
        reason: str | None = None

        if stat.S_ISLNK(mode):
            reason = "symbolic_link"
        elif name.startswith("/") or name.startswith("\\"):
            reason = "absolute_path"
        elif "\\" in name:
            reason = "windows_path_separator"
        elif len(name) > 1 and name[1] == ":":
            reason = "windows_drive_letter"
        elif ".." in PurePosixPath(name).parts:
            reason = "parent_directory"

        if reason is not None:
            raise _blocking(
                "PIP-L013",
                f"Inside {archive_name!r} there is an item recorded as {name!r}, "
                f"which does not name a place inside the folder this tool "
                f"unpacks into. Nothing was unpacked.",
                reason=reason,
                member=name,
                archive=archive_name,
            )


def _extract_shapefile(
    archive: zipfile.ZipFile,
    pieces: dict[str, zipfile.ZipInfo],
    *,
    stem: str,
    into: Path,
    archive_name: str,
) -> list[Path]:
    """Write the chosen shapefile set's pieces into `into`, and nothing else.

    `pieces` is one member per extension, already resolved to a single folder by
    `_shapefile_sets`. Each is written as `<stem><extension>` — a bare filename
    built here rather than taken from the archive, which is both what GDAL wants
    (the set has to sit together under one stem) and a structural answer to
    zip-slip: a name with no separator in it cannot point anywhere but `into`.
    The resolved destination is checked against `into` anyway, because a guard
    that is only correct while the code above it stays correct is not a guard,
    and a destination that already exists is refused rather than overwritten —
    one member of an archive must never be able to replace another.

    Bytes are counted as they are written rather than taken from the header —
    `_enforce_archive_caps` refuses what the archive *claims*, and this stops
    what it actually does.

    A member whose compressed bytes are damaged is a `Finding` like everything
    else here. The archive's directory can be perfectly well formed while one
    entry's stream is not, and the exceptions that come out of that
    (`zlib.error`, `BadZipFile` for a failed CRC, `EOFError` for a member cut
    short) are ordinary — a download that stopped halfway produces them — so
    they are turned into PIP-L001 rather than left to reach F8-T4 as a
    traceback.
    """
    into.mkdir(parents=True, exist_ok=True)
    root = into.resolve()
    written: list[Path] = []

    for suffix, info in sorted(pieces.items()):
        name = f"{stem}{suffix}"
        destination = (into / name).resolve()
        if destination != root / name or root not in destination.parents:
            raise _blocking(  # pragma: no cover - unreachable given the name check
                "PIP-L013",
                f"Inside {archive_name!r} the item {info.filename!r} resolved to "
                f"a place outside the folder this tool unpacks into.",
                reason="resolved_outside_workspace",
                member=info.filename,
            )

        copied = 0
        try:
            # "x", not "w": a second member landing on a name already written is
            # one piece of the archive replacing another, which is how a set
            # gets assembled out of two different layers. Refuse, never
            # overwrite.
            unpacked = open(destination, "xb")
        except FileExistsError as error:
            raise _blocking(
                "PIP-L013",
                f"Inside {archive_name!r} the item {info.filename!r} would "
                f"overwrite something already unpacked out of the same archive. "
                f"Nothing in a shapefile set writes over anything else.",
                reason="member_overwrites_member",
                member=info.filename,
                destination=name,
            ) from error
        try:
            with archive.open(info) as packed, unpacked:
                while True:
                    chunk = packed.read(EXTRACT_CHUNK_BYTES)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > MAX_MEMBER_UNCOMPRESSED_BYTES:
                        raise _blocking(
                            "PIP-L012",
                            f"While unpacking {info.filename!r} out of "
                            f"{archive_name!r} it went past "
                            f"{_megabytes(MAX_MEMBER_UNCOMPRESSED_BYTES)}, "
                            f"which is more than the archive itself said it "
                            f"would be.",
                            reason="member_exceeded_declared_size",
                            member=info.filename,
                            declared_bytes=int(info.file_size),
                            member_limit=MAX_MEMBER_UNCOMPRESSED_BYTES,
                        )
                    unpacked.write(chunk)
        except (zlib.error, zipfile.BadZipFile, EOFError) as error:
            # The archive's directory opened, this member's bytes did not: a
            # flipped byte in the compressed stream (zlib.error), a checksum
            # that does not match what came out (BadZipFile, "Bad CRC-32"), a
            # member cut short (EOFError). Ordinary for a download that failed
            # halfway, and ordinary for a file built to be hostile. Either way
            # the contract is a Finding — F8-T4 has no traceback to show an
            # operator, and a stack trace from `zlib` names neither the archive
            # nor the piece of it that is damaged.
            raise _blocking(
                "PIP-L001",
                f"The piece {info.filename!r} inside {archive_name!r} is "
                f"damaged and could not be unpacked — the archive opens, but "
                f"that item's contents do not come back out of it. The file "
                f"was most likely cut short or corrupted on the way here; "
                f"download it again and retry.",
                reason="unreadable_member",
                name=archive_name,
                member=info.filename,
                error=type(error).__name__,
            ) from error
        written.append(into / name)

    return sorted(written)


def _choose_shapefile(stems: list[str], *, select: str | None, where: str) -> str:
    """The one shapefile to read, or a refusal naming the choice.

    Silently taking the first of several is the failure this guards against:
    a portal that zips wards and precincts together would install whichever one
    sorted first, the preview would draw a real, valid, correct-looking layer,
    and nothing anywhere would say it was not the one asked for.

    There is no registry code for "which of these did you mean", so this fires
    PIP-L001 — the reader could not get *a* map out of the file — and puts the
    choices in `detail["shapefiles"]`, which is what F8-T4 renders as a picker
    before calling back with `select=`.

    The names offered are whatever identifies a set where it came from: a bare
    stem for loose files, and inside an archive the folder as well
    (`2024_official/wards`), because two folders holding a `wards.shp` are two
    layers and the operator has to be able to say which. `select=` takes either
    — the full name always, and a bare stem when only one folder in the archive
    has it.
    """
    unique = sorted(set(stems))
    if len(unique) == 1:
        return unique[0]
    if select is not None:
        if select in unique:
            return select
        by_stem = [key for key in unique if PurePosixPath(key).name == select]
        if len(by_stem) == 1:
            return by_stem[0]
        if by_stem:
            raise _blocking(
                "PIP-L001",
                f"There is more than one shapefile called {select!r} in "
                f"{where!r} — {_join_names(by_stem)} — sitting in different "
                f"folders. Name the folder as well.",
                reason="ambiguous_selection",
                selected=select,
                shapefiles=by_stem,
            )
        raise _blocking(
            "PIP-L001",
            f"There is no shapefile called {select!r} in {where!r}. The ones in "
            f"it are {_join_names(unique)}.",
            reason="unknown_selection",
            selected=select,
            shapefiles=unique,
        )
    raise _blocking(
        "PIP-L001",
        f"There {'is' if len(unique) == 1 else 'are'} {len(unique)} separate "
        f"shapefiles inside {where!r} — {_join_names(unique)} — and this tool "
        f"installs one layer at a time. Say which one you want, or unpack the "
        f"file and send just that one.",
        reason="several_shapefiles",
        shapefiles=unique,
    )


# --------------------------------------------------------------------------
# (2) loose shapefile parts
# --------------------------------------------------------------------------


def _read_shapefile_parts(
    parts: Sequence[Path],
    *,
    supplied_names: Sequence[str],
    select: str | None,
    workspace: Path | None = None,
    container: str | None = None,
    metadata_names: Sequence[str] = (),
) -> Candidate:
    """The pieces of a shapefile set, wherever they came from.

    The pieces an operator drags in do not necessarily share a folder — a
    browser hands each upload its own temporary file — and GDAL can only open a
    shapefile whose companions sit next to it under the same stem. So anything
    not already gathered is staged into a workspace first.

    A workspace this function creates is this function's to release: if the read
    then fails there is no `Candidate` to carry it, and nothing else knows it
    exists. Only the workspace that reaches the returned `Candidate` outlives
    the call, and the caller owns that one.
    """
    named = list(zip(parts, supplied_names))
    by_stem: dict[str, dict[str, tuple[Path, str]]] = {}
    for path, name in named:
        suffix = Path(name).suffix.lower() or path.suffix.lower()
        if suffix not in SHAPEFILE_EXTENSIONS:
            continue
        stem = Path(name).name[: -len(suffix)] if suffix else Path(name).name
        pieces = by_stem.setdefault(stem, {})
        if suffix in pieces:
            # Two files claiming to be the same piece of the same set. Whichever
            # one this loop reached second would silently replace the first, and
            # a set assembled that way can hold one layer's outlines and
            # another's names.
            raise _blocking(
                "PIP-L001",
                f"Two of the files sent are both called "
                f"{Path(name).name!r}, and a shapefile set has one of each "
                f"piece. Send the pieces of one shapefile.",
                reason="duplicate_shapefile_part",
                name=Path(name).name,
                extension=suffix,
                stem=stem,
            )
        pieces[suffix] = (path, Path(name).name)

    where = container or "the files you sent"
    if not by_stem:
        raise _blocking(
            "PIP-L001",
            f"None of the files in {where} are pieces of a shapefile.",
            reason="no_shapefile_parts",
            names=list(supplied_names),
        )

    stem = _choose_shapefile(sorted(by_stem), select=select, where=where)
    pieces = by_stem[stem]

    missing = [
        suffix for suffix in REQUIRED_SHAPEFILE_EXTENSIONS if suffix not in pieces
    ]
    if missing:
        arrived = sorted(name for _, name in pieces.values())
        raise _blocking(
            "PIP-L002",
            f"The {_join_names(missing)} "
            f"{'pieces are' if len(missing) > 1 else 'piece is'} missing. What "
            f"arrived was {_join_names(arrived)}.",
            reason="incomplete_shapefile",
            missing_extensions=missing,
            arrived=arrived,
            stem=stem,
        )

    # GDAL opens a .shp by looking for its companions beside it under the same
    # stem, so the set has to be gathered before it can be read. It already is
    # when the pieces came out of an archive, or when the operator pointed at a
    # folder; it is not when a browser handed each upload its own temporary file
    # under its own temporary name. Staging covers both halves of that: one
    # folder, and the names the operator's own file had.
    owned_workspace = workspace
    already_gathered = _common_directory(
        [path for path, _ in pieces.values()]
    ) is not None and all(
        path.name == f"{stem}{suffix}" for suffix, (path, _) in pieces.items()
    )
    # Whoever creates a workspace releases it if the read then fails. A caller
    # that handed one in (`_read_zip`) already does that for its own; this
    # branch makes one of its own for every browser upload there is — the
    # pieces arrive in separate temporary files, so `already_gathered` is False
    # — and a failure between here and the `Candidate` used to leave that folder
    # behind, holding a full copy of the operator's data, in $TMPDIR forever.
    # The successful path is the one where the folder outlives this function:
    # it is handed to the caller on the `Candidate`, which owns it from then on.
    staged_here = not already_gathered and workspace is None
    try:
        if already_gathered:
            shp_path = pieces[".shp"][0]
        else:
            owned_workspace = owned_workspace or Path(
                tempfile.mkdtemp(prefix="pip-layer-")
            )
            for suffix, (path, _) in pieces.items():
                shutil.copy2(path, owned_workspace / f"{stem}{suffix}")
            shp_path = owned_workspace / f"{stem}.shp"

        arrived_names = tuple(sorted(name for _, name in pieces.values()))
        metadata_sibling = _shapefile_metadata_sibling(
            shp_path, stem, offered=metadata_names
        )

        try:
            frame = gpd.read_file(shp_path)
        except Exception as error:
            raise _blocking(
                "PIP-L001",
                f"The shapefile {stem + '.shp'!r} has all its pieces but could "
                f"not be opened — the file itself may be damaged or cut short.",
                reason="unreadable_shapefile",
                stem=stem,
                error=type(error).__name__,
            ) from error

        written_on = _dbf_written_on(pieces[".dbf"][0])
        findings = [
            _no_vintage_finding(
                SOURCE_SHAPEFILE,
                written_on=written_on,
                metadata_sibling=metadata_sibling,
            )
        ]
        findings.extend(_truncated_name_findings(frame))

        facts = _facts(
            frame,
            source_kind=SOURCE_SHAPEFILE,
            source_files=arrived_names,
            vintage=None,
        )
        facts["shapefile_written_on"] = written_on
        facts["shapefile_metadata_file"] = metadata_sibling
        if container:
            facts["unpacked_from"] = container

        return Candidate(
            frame=frame,
            source_kind=SOURCE_SHAPEFILE,
            source_files=arrived_names,
            # Deliberately None. A shapefile has nowhere to record how old its
            # boundaries are, and the .dbf's write date is not that — see
            # `_dbf_written_on`. Reporting it as a vintage would silence
            # PIP-L017, which is the one thing the operator needs to hear about
            # this format.
            vintage=None,
            findings=tuple(sort_findings(findings)),
            facts=facts,
            workspace=owned_workspace,
        )
    except BaseException:
        if staged_here and owned_workspace is not None:
            shutil.rmtree(owned_workspace, ignore_errors=True)
        raise


def _common_directory(paths: Sequence[Path]) -> Path | None:
    """The one folder all these pieces already share, or None if they are apart."""
    parents = {path.resolve().parent for path in paths}
    return parents.pop() if len(parents) == 1 else None


def _shapefile_metadata_sibling(
    shp_path: Path, stem: str, *, offered: Sequence[str] = ()
) -> str | None:
    """The name of a `<stem>.shp.xml` that came with this shapefile, if any.

    This is the only place a shapefile set can carry a real publication date —
    ESRI writes the layer's metadata document here. This module never parses or
    opens it (its schema varies by ArcGIS version and by agency, and it is
    untrusted XML from a stranger's zip), but its *presence* is worth telling
    the operator about, because it is where the answer to "how old is this?" may
    actually be.

    `offered` are names seen elsewhere — inside the archive's directory, or in
    the operator's own file list — for the cases where the document was never
    written to disk beside the .shp.
    """
    wanted = f"{stem}{SHAPEFILE_METADATA_SUFFIX}"
    for name in offered:
        if Path(name).name.lower() == wanted.lower():
            return Path(name).name
    candidate = shp_path.with_name(wanted)
    try:
        return candidate.name if candidate.exists() else None
    except OSError:  # pragma: no cover - unreadable directory
        return None


def _dbf_written_on(dbf_path: Path) -> str | None:
    """When the .dbf was written out, as `YYYY-MM-DD`, or None.

    Measured, not assumed. A shapefile carries two dates and neither is the age
    of the data:

    * the .shp header's version field is the constant 1000 — the 1998 *format*
      version, identical in every shapefile ever written, so it says nothing at
      all;
    * the .dbf header's bytes 1, 2 and 3 hold the year, month and day the table
      was written out. That is a real date, but it is the date somebody ran an
      export — a 2026 re-export of 2010 boundaries reads as 2026.

    So this is reported to the operator as "written on", inside PIP-L017, and
    never as the layer's vintage. Verified on a file written by this project's
    own GeoPandas stack: bytes 1-3 came back 126, 8, 2 for 2026-08-02.

    The year byte is years-since-1900 (GDAL, and dBase III before it, write 126
    for 2026). Some writers store a two-digit year instead, so a value under 80
    is read as 2000s — the usual windowing, and the only ambiguity in the
    format.
    """
    try:
        with open(dbf_path, "rb") as table:
            header = table.read(4)
    except OSError:  # pragma: no cover - defensive
        return None
    if len(header) < 4:
        return None
    raw_year, month, day = header[1], header[2], header[3]
    year = 1900 + raw_year if raw_year >= 80 else 2000 + raw_year
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# (3) GeoJSON
# --------------------------------------------------------------------------


def _read_geojson(path: Path, *, name: str) -> Candidate:
    """A .geojson, read as it stands.

    RFC 7946 has no field for a date — not for the collection and not for a
    feature — so a GeoJSON never carries a vintage and PIP-L017 always fires.
    That is the honest answer, not a shortcoming of this reader.

    The CRS is whatever the file made of it. RFC 7946 fixed the coordinate
    system at WGS84 and dropped the `crs` member, which is why GDAL hands back
    EPSG:4326 for a plain file, and pre-2016 files carrying the old `crs` member
    still say something different. Neither is corrected here.
    """
    try:
        frame = gpd.read_file(path)
    except Exception as error:
        raise _blocking(
            "PIP-L001",
            f"The file {name!r} ends in {path.suffix} but does not read as map "
            f"data. A .json that is not GeoJSON — a list of records, an API "
            f"response — looks exactly like this.",
            reason="unreadable_geojson",
            name=name,
            error=type(error).__name__,
        ) from error

    return Candidate(
        frame=frame,
        source_kind=SOURCE_GEOJSON,
        source_files=(name,),
        vintage=None,
        findings=(_no_vintage_finding(SOURCE_GEOJSON),),
        facts=_facts(
            frame,
            source_kind=SOURCE_GEOJSON,
            source_files=(name,),
            vintage=None,
        ),
    )


# --------------------------------------------------------------------------
# GeoPackage — beyond the four, and the only format with a real date in it
# --------------------------------------------------------------------------


def _read_geopackage(path: Path, *, name: str, select: str | None) -> Candidate:
    """A .gpkg — the format this service stores its own layers in.

    Worth reading for two reasons beyond the obvious one. It is what
    `scripts/build_data.py` writes, so a maintainer moving a layer between two
    installs of this service has nothing to convert. And it is the only format
    here that carries a genuine date: `gpkg_contents.last_change` is required by
    the OGC standard, every writer populates it, and the stdlib `sqlite3` reads
    it without opening the spatial machinery at all.
    """
    layers = _geopackage_layers(path, name=name)
    layer_names = sorted(layers)
    if not layer_names:
        raise _blocking(
            "PIP-L001",
            f"The file {name!r} opens as a GeoPackage but has no layers of shapes "
            f"in it.",
            reason="empty_geopackage",
            name=name,
        )
    if len(layer_names) > 1 and select is None:
        raise _blocking(
            "PIP-L001",
            f"There are {len(layer_names)} layers inside {name!r} — "
            f"{_join_names(layer_names)} — and this tool installs one layer at a "
            f"time. Say which one you want.",
            reason="several_layers",
            layers=layer_names,
        )
    layer = select or layer_names[0]
    if layer not in layers:
        raise _blocking(
            "PIP-L001",
            f"There is no layer called {layer!r} inside {name!r}. The ones in it "
            f"are {_join_names(layer_names)}.",
            reason="unknown_selection",
            selected=layer,
            layers=layer_names,
        )

    try:
        frame = gpd.read_file(path, layer=layer)
    except Exception as error:
        raise _blocking(
            "PIP-L001",
            f"The layer {layer!r} inside {name!r} could not be read.",
            reason="unreadable_geopackage_layer",
            name=name,
            layer=layer,
            error=type(error).__name__,
        ) from error

    last_change = layers[layer]
    vintage = (
        f"last changed {last_change} (recorded inside the GeoPackage)"
        if last_change
        else None
    )
    findings = [] if vintage else [_no_vintage_finding(SOURCE_GEOPACKAGE)]

    facts = _facts(
        frame,
        source_kind=SOURCE_GEOPACKAGE,
        source_files=(name,),
        vintage=vintage,
    )
    facts["geopackage_layer"] = layer
    facts["geopackage_layers"] = layer_names
    facts["geopackage_last_change"] = last_change

    return Candidate(
        frame=frame,
        source_kind=SOURCE_GEOPACKAGE,
        source_files=(name,),
        vintage=vintage,
        findings=tuple(findings),
        facts=facts,
    )


def _geopackage_layers(path: Path, *, name: str) -> dict[str, str | None]:
    """Every feature layer in the file, mapped to its recorded last-change stamp.

    Read with the stdlib `sqlite3` rather than by opening the file as spatial
    data: a GeoPackage *is* a SQLite database, `gpkg_contents` is part of the
    OGC standard, and reading one table out of it is far cheaper than loading
    geometry that may turn out to be the wrong layer. Opened read-only so a
    file the operator is about to look at is never modified.
    """
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise _blocking(
            "PIP-L001",
            f"The file {name!r} ends in .gpkg but does not open as one.",
            reason="unreadable_geopackage",
            name=name,
            error=type(error).__name__,
        ) from error
    try:
        rows = connection.execute(
            "SELECT table_name, last_change FROM gpkg_contents "
            "WHERE data_type = 'features'"
        ).fetchall()
    except sqlite3.Error as error:
        raise _blocking(
            "PIP-L001",
            f"The file {name!r} ends in .gpkg but does not have the table of "
            f"contents every GeoPackage has, so it is not one.",
            reason="not_a_geopackage",
            name=name,
            error=type(error).__name__,
        ) from error
    finally:
        connection.close()
    return {str(table): (str(stamp) if stamp else None) for table, stamp in rows}


# --------------------------------------------------------------------------
# (4) ArcGIS REST
# --------------------------------------------------------------------------


def _url_scheme(source: str) -> str | None:
    """The scheme of `source` if it is a web address, else None.

    A single-letter scheme is a Windows drive (`C:\\data\\wards.shp`), never a
    protocol, so it is read as a path.
    """
    parsed = urlparse(source)
    if not parsed.scheme or len(parsed.scheme) < 2:
        return None
    return parsed.scheme.lower()


def _public_url(url: str) -> str:
    """`url` with everything secret taken out of it: scheme, host and path only.

    An address an operator pastes can carry a `token=` in its query string and a
    password in its userinfo (`https://user:pw@host/...`), and both are
    credentials. SPEC §9 says neither may reach a message, a finding's detail or
    `facts` — all three of which are rendered in a browser, and any of which a
    volunteer may copy into a bug report. The request itself still carries the
    whole address, because that is what the far end needs; nothing that is
    *shown* does.

    This is the one function that decides what an address looks like once it
    stops being a request and becomes something said out loud, so that "is this
    field safe?" has one answer rather than one per field.
    """
    parsed = urlparse(url)
    # `user:password@host` — everything before the last @ is a credential.
    host = parsed.netloc.rsplit("@", 1)[-1]
    return urlunparse((parsed.scheme, host, parsed.path, "", "", ""))


def _arcgis_query_url(url: str) -> tuple[str, str, dict[str, str]]:
    """`(query_url, layer_url, params)` for an operator-supplied address.

    Accepts both shapes an operator copies: the layer's own address
    (`.../MapServer/2`), which is what a portal's page shows, and a whole query
    or export address someone has already built, which is what a colleague
    pastes out of a browser. In the second case their `where`, `outFields` and
    `outSR` are kept — they may have been narrowing the layer deliberately —
    but `f` is forced to geojson, because a GeoJSON payload is the one thing
    this reader knows how to turn into shapes.

    Both addresses handed back are rebuilt from the scheme, the *host* and the
    path, so neither carries a token in a query string nor a password in its
    userinfo. They are what every message, detail and fact on this path is built
    from; the operator's own string is used to make the request and nowhere
    else.

    A `token=` the operator pasted stays in `params` — it is what the far end
    asked for, and dropping it would turn a working address into a 403. Userinfo
    is dropped from the request as well as from the messages: this tool fetches
    public data by charter, an embedded password is not that, and keeping it
    would mean two versions of the same address to keep straight.
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    supplied = dict(parse_qsl(parsed.query))

    if _LAYER_URL_PATTERN.search(path):
        layer_path, query_path = path, f"{path}/query"
    elif path.lower().endswith("/query"):
        layer_path, query_path = path[: -len("/query")], path
    elif _SERVICE_URL_PATTERN.search(path):
        raise _blocking(
            "PIP-L014",
            f"That address names a whole map service rather than one layer "
            f"inside it. Add the layer's number on the end — {path}/0 for its "
            f"first layer — and try again.",
            reason="service_not_layer",
            url=_public_url(url),
        )
    elif supplied:
        # A query or export address of some other shape. Take it at its word;
        # if what comes back is not features, `_arcgis_features` says so.
        layer_path, query_path = path, path
    else:
        layer_path, query_path = path, f"{path}/query"

    params = dict(ARCGIS_BASE_QUERY)
    params.update(supplied)
    params["f"] = "geojson"

    def rebuild(new_path: str) -> str:
        # The host, never the netloc: `user:pw@host` would carry the operator's
        # password into every message built from these two addresses.
        host = parsed.netloc.rsplit("@", 1)[-1]
        return f"{parsed.scheme}://{host}{new_path}"

    return rebuild(query_path), rebuild(layer_path), params


def _read_arcgis(url: str) -> Candidate:
    """A published ArcGIS REST layer, fetched whole or not at all.

    Paging is the whole difficulty. A service answers with at most its
    `maxRecordCount` features and sets `exceededTransferLimit` to say there are
    more; a reader that takes the first page gets a boundary layer with half a
    county in it, and that half is perfectly valid data — every check in
    `app.admin.validate` passes, the preview map draws a real map, and the
    service then answers "no district" for every address in the missing half,
    confidently and forever. So the set is walked with resultOffset /
    resultRecordCount exactly as `build_data.fetch_address_points` walks the
    county's address points, and anything that stops the walk short is a refusal
    rather than a smaller layer.
    """
    query_url, layer_url, params = _arcgis_query_url(url)
    with httpx.Client(
        timeout=ARCGIS_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}
    ) as client:
        metadata = _arcgis_layer_metadata(client, layer_url)
        features, pages, payload_crs = _arcgis_features(
            client, query_url, params, metadata
        )

    vintage = _arcgis_vintage(metadata)
    frame = _frame_from_features(features, url=query_url, payload_crs=payload_crs)
    frame, out_sr = _declare_requested_out_sr(
        frame, params.get("outSR"), payload_crs=payload_crs, url=query_url
    )

    findings = [] if vintage else [_no_vintage_finding(SOURCE_ARCGIS_REST)]
    facts = _facts(
        frame,
        source_kind=SOURCE_ARCGIS_REST,
        source_files=(),
        vintage=vintage,
    )
    # The address as it can safely be shown: `facts` is rendered in a browser
    # and copied into bug reports, and the string the operator pasted may hold a
    # token. `query_url` is already rebuilt from scheme, host and path.
    facts["source_url"] = _public_url(url)
    facts["query_url"] = query_url
    facts["pages_fetched"] = pages
    facts["service_layer_name"] = metadata.get("name")
    facts["max_record_count"] = metadata.get("maxRecordCount")
    facts["requested_out_sr"] = out_sr

    return Candidate(
        frame=frame,
        source_kind=SOURCE_ARCGIS_REST,
        source_files=(),
        vintage=vintage,
        findings=tuple(findings),
        facts=facts,
    )


@dataclass(frozen=True)
class _BoundedBody:
    """One HTTP answer, read only as far as this reader is willing to hold it.

    `over_limit` says the far end was still talking when the reader stopped
    listening, in which case `text` is empty: there is nothing to describe and
    the point was not to buffer it.
    """

    status_code: int
    content_type: str
    text: str
    over_limit: bool


def _get_bounded(
    client: httpx.Client,
    url: str,
    params: dict[str, str],
    *,
    limit: int,
) -> _BoundedBody:
    """GET `url`, holding at most `limit` bytes of the answer.

    `client.get()` reads the whole body before it returns and `response.text`
    decodes all of it, so an answer this reader will only ever quote 200
    characters of is nevertheless paid for in full: a 314 MB HTML page was
    buffered and decoded — RSS 420 MB to 1.3 GB — on the way to a refusal that
    was itself correct. The size of an answer is chosen entirely by the far end,
    which is the definition of a bound this process has to set for itself.

    So the body is streamed: the declared `Content-Length` is refused before a
    byte arrives when it is already over, and the stream is abandoned mid-flight
    the moment what has arrived goes over. Leaving the `with` closes the
    connection, which is what stops the sender.

    ArcGIS / ArcPy equivalent
        None, and that is the point: `arcpy.FeatureSet.load(url)` hands the
        whole transfer to ArcGIS and a hostile endpoint takes the process down
        with it. A service reading addresses volunteers paste has to bound what
        it will hold.
    """
    with client.stream("GET", url, params=params) as response:
        content_type = response.headers.get("content-type", "")
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            return _BoundedBody(response.status_code, content_type, "", True)

        body = bytearray()
        for chunk in response.iter_bytes(RESPONSE_CHUNK_BYTES):
            body.extend(chunk)
            if len(body) > limit:
                return _BoundedBody(response.status_code, content_type, "", True)
        return _BoundedBody(
            response.status_code,
            content_type,
            _decode_body(bytes(body), content_type),
            False,
        )


def _decode_body(body: bytes, content_type: str) -> str:
    """The bytes as text, by whatever charset the answer named, never raising.

    An answer this reader is about to refuse is exactly the one most likely to
    be mislabelled or half-binary, so a decoding error must not replace the
    honest message about what came back with a `UnicodeDecodeError`.
    """
    charset = "utf-8"
    for parameter in content_type.split(";")[1:]:
        key, _, value = parameter.partition("=")
        if key.strip().lower() == "charset" and value.strip():
            charset = value.strip().strip('"')
    try:
        return body.decode(charset, errors="replace")
    except LookupError:  # a charset nobody has heard of
        return body.decode("utf-8", errors="replace")


def _arcgis_layer_metadata(client: httpx.Client, layer_url: str) -> dict[str, Any]:
    """The layer's own description (`?f=json`), or `{}`.

    Best-effort by design, and never a reason to fail: it is where
    `maxRecordCount` and `editingInfo.lastEditDate` live, and a service is free
    to publish neither. Verified against Cook County's
    politicalBoundary/MapServer/2, which returns `serviceItemId` and
    `currentVersion` but no `editingInfo` at all — so a reader that depended on
    this would fail on the very layer this project ships.

    Bounded like every other body this reader fetches (`MAX_METADATA_BYTES`): a
    layer description is a few kilobytes of JSON, and an endpoint that answers
    this best-effort request with half a gigabyte gets the same treatment as one
    that answers it with a 500 — `{}`, and the conservative walk that follows
    from that.
    """
    try:
        fetched = _get_bounded(
            client, layer_url, {"f": "json"}, limit=MAX_METADATA_BYTES
        )
        if fetched.status_code >= 400 or fetched.over_limit:
            return {}
        payload = json.loads(fetched.text)
    except (httpx.HTTPError, ValueError):
        return {}
    return payload if isinstance(payload, dict) and "error" not in payload else {}


def _arcgis_vintage(metadata: dict[str, Any]) -> str | None:
    """`editingInfo.lastEditDate` as a human sentence, when the service has one.

    The stamp is epoch milliseconds, UTC. Optional in the REST specification and
    genuinely absent in the field, so this returns None far more often than not
    and PIP-L017 takes over.
    """
    editing = metadata.get("editingInfo")
    if not isinstance(editing, dict):
        return None
    stamp = editing.get("lastEditDate")
    if not isinstance(stamp, (int, float)):
        return None
    try:
        edited = datetime.fromtimestamp(stamp / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):  # pragma: no cover - absurd stamp
        return None
    return (
        f"last edited {edited.date().isoformat()} "
        f"(reported by the map service itself)"
    )


def _arcgis_features(
    client: httpx.Client,
    query_url: str,
    params: dict[str, str],
    metadata: dict[str, Any],
) -> tuple[list[dict[str, Any]], int, Any]:
    """Every feature in the layer, or a refusal. Never a prefix of them.

    Returns the features, how many requests it took, and the `crs` member the
    service put on its answer (None when it emitted none) — that last is part of
    the payload and belongs to the data, not to this walk, so it is carried out
    rather than thrown away.

    A page that exactly fills the size asked for is never final: more may
    remain. A *short* page is only final when the size asked for was a fact
    about the service rather than a guess by this reader — see
    `_arcgis_page_size`. That asymmetry is the whole of the truncation
    argument. If the service publishes no `maxRecordCount` and the operator
    named no `resultRecordCount`, this reader asked for `ARCGIS_PAGE_SIZE`
    because it had to ask for something, and a service that caps its answers
    lower than that guess — 500 features against a guess of 1,000, with no
    `exceededTransferLimit` set, which real services do — hands back a short
    page that says nothing whatever about whether the layer is finished. Reading
    it as "that is all there is" is this reader believing its own guess: one
    request, a fifth of a county, and every check downstream passing on it. So
    when the size is a guess the walk ends only on an empty page (or on the
    repeat-page guard, or a cap), which costs one extra round trip and cannot
    truncate.

    `exceededTransferLimit` still ends the question early when it is *set* —
    more definitely remain — and some services set it on a page that is also
    short, which length alone would call the end.

    The offset advances by the number of features actually received rather than
    by the page size, so a service that quietly returns fewer than asked for
    still yields a walk with no gap and no repeat. A service that ignores
    resultOffset altogether would otherwise loop forever returning page one, so
    a whole page repeating verbatim ends the walk as a failure — that is
    duplication, which is as wrong as truncation and much harder to see.
    """
    page_size, page_size_is_published = _arcgis_page_size(params, metadata)
    features: list[dict[str, Any]] = []
    offset = 0
    pages = 0
    previous_page: str | None = None
    payload_crs: Any = None

    while True:
        page_params = dict(params)
        page_params["resultOffset"] = str(offset)
        page_params["resultRecordCount"] = str(page_size)
        payload = _arcgis_page(client, query_url, page_params)
        pages += 1

        # The service's own statement of what its coordinates are. Kept from
        # the first page that carries one; dropping it is how a frame ends up
        # labelled EPSG:4326 while holding State Plane feet.
        if payload_crs is None and payload.get("crs") is not None:
            payload_crs = payload["crs"]

        page_features = payload.get("features")
        if not isinstance(page_features, list):
            raise _blocking(
                "PIP-L014",
                f"The map service answered, but its answer has no list of shapes "
                f"in it. What came back begins: {_head(payload)}",
                reason="no_features_member",
                url=query_url,
            )

        if page_features:
            # The whole page, not just its first feature: two consecutive pages
            # can legitimately open with identical-looking features (a source
            # with genuinely duplicated rows), but a whole page repeating
            # verbatim at a different offset means only one thing.
            fingerprint = json.dumps(page_features, sort_keys=True)
            if previous_page is not None and fingerprint == previous_page:
                raise _blocking(
                    "PIP-L014",
                    f"The map service is handing back the same {len(page_features)} "
                    f"shapes over and over instead of moving on through the layer, "
                    f"so this tool cannot tell how much of it it has. "
                    f"{len(features):,} shapes had arrived when it stopped, and a "
                    f"part of a layer is not safe to install.",
                    reason="paging_ignored",
                    url=query_url,
                    fetched=len(features),
                    pages=pages,
                )
            previous_page = fingerprint

        features.extend(page_features)

        # Judged here, before any way out of the loop, and not on the path that
        # continues. A cap tested only where the walk goes round again is a cap
        # on layers that need more than one request, which is not what it says
        # it is: one page of 600,000 features against a limit of 500,000 used to
        # be accepted outright, because the short-page break came first. The far
        # end picks both how big a page is and what is in it, so a limit it can
        # step over by answering in one go constrains only the services that
        # were never the problem.
        if len(features) > MAX_ARCGIS_FEATURES:
            raise _blocking(
                "PIP-L014",
                f"This layer has more than {MAX_ARCGIS_FEATURES:,} areas in it, "
                f"which is more than this service can hold. {len(features):,} had "
                f"arrived before it stopped, and a part of a layer is not safe to "
                f"install.",
                reason="too_many_features",
                url=query_url,
                fetched=len(features),
                feature_limit=MAX_ARCGIS_FEATURES,
                pages=pages,
            )

        # ArcGIS puts the flag at the top level of a GeoJSON response; some
        # versions tuck it inside `properties` instead. Both count.
        properties = payload.get("properties")
        exceeded = bool(payload.get("exceededTransferLimit")) or bool(
            isinstance(properties, dict)
            and properties.get("exceededTransferLimit")
        )

        if not page_features:
            break
        if len(page_features) < page_size and not exceeded and page_size_is_published:
            break

        offset += len(page_features)
        if pages >= MAX_ARCGIS_PAGES:
            raise _blocking(
                "PIP-L014",
                f"That address has been asked for another page "
                f"{MAX_ARCGIS_PAGES:,} times and is still not finished. "
                f"{len(features):,} areas had arrived when it stopped, and a "
                f"part of a layer is not safe to install.",
                reason="too_many_pages",
                url=query_url,
                fetched=len(features),
                page_limit=MAX_ARCGIS_PAGES,
                pages=pages,
            )

    return features, pages, payload_crs


def _arcgis_page_size(
    params: dict[str, str], metadata: dict[str, Any]
) -> tuple[int, bool]:
    """How many features to ask for at a time, and whether that number is known.

    The number is known when the operator put a `resultRecordCount` in the
    address they pasted, or when the service published its own
    `maxRecordCount` — asking for more than a service will give only wastes a
    round trip. Otherwise `ARCGIS_PAGE_SIZE` is asked for because a request has
    to say something, and the second half of the answer is False: a guess.

    Whoever named the number, it is bounded by `MAX_ARCGIS_PAGE_SIZE`. A
    published `maxRecordCount` is a claim, and a service is free to claim
    1,000,000,000 — at which point the far end has chosen how much of this
    process's memory one answer occupies. A bounded number is also, honestly, a
    number neither the operator nor the service asked for, so the second half of
    the answer goes back to False when it has to be bounded: a short page can no
    longer end the walk, because the size it fell short of was this reader's
    own choice again.

    Whether it is a guess decides whether a short page may end the walk, which
    is why this is returned rather than assumed — see `_arcgis_features`. Note
    that a metadata request that simply *failed* lands here as "not published",
    which is the safe reading: a transient 500 on one best-effort request must
    never change how much of a layer gets installed.
    """
    supplied = params.get("resultRecordCount")
    if supplied and str(supplied).isdigit() and int(supplied) > 0:
        return _bounded_page_size(int(supplied))
    published = metadata.get("maxRecordCount")
    if isinstance(published, int) and not isinstance(published, bool) and published > 0:
        return _bounded_page_size(published)
    return ARCGIS_PAGE_SIZE, False


def _bounded_page_size(asked: int) -> tuple[int, bool]:
    """`asked`, or the most this reader will hold and "not a fact" with it."""
    if asked > MAX_ARCGIS_PAGE_SIZE:
        return MAX_ARCGIS_PAGE_SIZE, False
    return asked, True


def _arcgis_page(
    client: httpx.Client, query_url: str, params: dict[str, str]
) -> dict[str, Any]:
    """One page of the feature query, or PIP-L014 saying which kind of wrong it is.

    The distinction the operator needs is between "that address is not a map
    service" — a portal's landing page, a sign-in screen, an error the service
    itself reports — and "the network failed", because the first means change
    the address and the second means try again. They are told apart here and the
    reason is recorded in `detail["reason"]` for F8-T4.

    The body is read through `_get_bounded`, so an answer larger than this
    reader will hold is its own refusal rather than a memory spike on the way to
    one.
    """
    try:
        fetched = _get_bounded(
            client, query_url, params, limit=MAX_RESPONSE_BYTES
        )
    except httpx.HTTPError as error:
        raise _blocking(
            "PIP-L014",
            f"The tool could not reach that address at all — the network "
            f"request failed ({type(error).__name__}), so nothing came back to "
            f"look at. This is a problem with the connection or with the far "
            f"end being down, not with the address itself.",
            reason="network_failed",
            url=query_url,
            error=type(error).__name__,
        ) from error

    body = fetched.text
    if fetched.over_limit:
        raise _blocking(
            "PIP-L014",
            f"The answer from that address went past "
            f"{_megabytes(MAX_RESPONSE_BYTES)} and was cut off unread. A single "
            f"page of a boundary layer is nothing like that size, so this is "
            f"not a map service answering a query — nothing was read.",
            reason="response_too_large",
            url=query_url,
            status=fetched.status_code,
            response_limit=MAX_RESPONSE_BYTES,
        )
    if fetched.status_code in (401, 403):
        raise _blocking(
            "PIP-L014",
            f"The far end refused the request (HTTP {fetched.status_code}), "
            f"which means this data is not public. This tool only works with "
            f"data that is.",
            reason="sign_in_required",
            url=query_url,
            status=fetched.status_code,
        )
    if fetched.status_code >= 400:
        raise _blocking(
            "PIP-L014",
            f"The far end answered with an error (HTTP "
            f"{fetched.status_code}) instead of map data. What came back "
            f"begins: {_head(body)}",
            reason="http_error",
            url=query_url,
            status=fetched.status_code,
            body_head=_head(body),
        )

    if _looks_like_html(fetched.content_type, body):
        signin = _looks_like_sign_in(body)
        raise _blocking(
            "PIP-L014",
            (
                "What came back is a sign-in page, so this data is not public "
                "and this tool only works with data that is."
                if signin
                else "What came back is an ordinary web page, not map data, so "
                "that address is the page describing the data rather than the "
                "data itself."
            )
            + f" It begins: {_head(body)}",
            reason="sign_in_page" if signin else "not_a_map_service",
            url=query_url,
            body_head=_head(body),
        )

    try:
        payload = json.loads(body)
    except ValueError as error:
        raise _blocking(
            "PIP-L014",
            f"What came back is neither map data nor anything this tool can "
            f"read. It begins: {_head(body)}",
            reason="not_a_map_service",
            url=query_url,
            body_head=_head(body),
        ) from error

    if not isinstance(payload, dict):
        raise _blocking(
            "PIP-L014",
            f"What came back is not shaped like map data. It begins: "
            f"{_head(body)}",
            reason="not_a_map_service",
            url=query_url,
            body_head=_head(body),
        )

    # ArcGIS reports its own failures as HTTP 200 with an {"error": ...} body —
    # the same habit `app.geocoding.arcgis` guards against.
    if "error" in payload:
        detail = payload["error"] if isinstance(payload["error"], dict) else {}
        message = detail.get("message") or "no message given"
        raise _blocking(
            "PIP-L014",
            f"The map service itself reported a problem rather than sending "
            f"data: {message!r} (its code {detail.get('code', 'unknown')}). The "
            f"address reached a real map service, so it is the request it "
            f"objects to — most often a layer number that does not exist.",
            reason="service_error",
            url=query_url,
            service_code=detail.get("code"),
            service_message=message,
        )

    return payload


def _looks_like_html(content_type: str, body: str) -> bool:
    if "html" in content_type.lower():
        return True
    return body.lstrip()[:200].lower().startswith(("<!doctype html", "<html"))


def _looks_like_sign_in(body: str) -> bool:
    lowered = body.lower()
    return any(
        marker in lowered
        for marker in ("sign in", "sign-in", "log in", "login", "password")
    )


def _frame_from_features(
    features: list[dict[str, Any]], *, url: str, payload_crs: Any = None
) -> gpd.GeoDataFrame:
    """The fetched features as a frame, read through the same GeoJSON path a file
    would take.

    The pages are reassembled into one FeatureCollection and written to a
    temporary file for GDAL to read, rather than being converted by hand: that
    is what `scripts/build_data.py` already does with this same payload, it
    keeps one code path for GeoJSON however it arrived, and it means a `crs`
    member the service chose to emit is honoured exactly as it would be in a
    file the operator downloaded.

    Which is why `payload_crs` is put back on the reassembled collection.
    Reassembling without it is not a lost nicety: GDAL stamps EPSG:4326 on a
    GeoJSON that declares nothing, so a service declaring EPSG:3435 and sending
    State Plane feet would come back *labelled* 4326. PIP-L004 then fires with
    the wrong diagnosis — it tells the operator their data claims degrees, when
    the service said 3435 and said it correctly.
    """
    # `crs` is written before `features` because GDAL's GeoJSON driver may read
    # the document as a stream and take the coordinate system from whatever it
    # has seen by the time the first feature arrives.
    collection: dict[str, Any] = {"type": "FeatureCollection"}
    if payload_crs is not None:
        collection["crs"] = payload_crs
    collection["features"] = features
    with tempfile.TemporaryDirectory(prefix="pip-arcgis-") as scratch:
        payload_path = Path(scratch) / "features.geojson"
        payload_path.write_text(json.dumps(collection))
        try:
            return gpd.read_file(payload_path)
        except Exception as error:
            raise _blocking(
                "PIP-L014",
                f"The map service sent {len(features):,} shapes back, but they "
                f"could not be read as map data.",
                reason="unreadable_features",
                url=url,
                feature_count=len(features),
                error=type(error).__name__,
            ) from error


def _declare_requested_out_sr(
    frame: gpd.GeoDataFrame,
    out_sr: Any,
    *,
    payload_crs: Any,
    url: str,
) -> tuple[gpd.GeoDataFrame, int | None]:
    """Record the coordinate system an `outSR=` in the address asked the service
    for, or refuse an `outSR` that cannot be recorded.

    `outSR` tells the service to reproject before answering, so it is a
    statement about what the numbers coming back *are*. ArcGIS then sends
    GeoJSON with no `crs` member — and GDAL reads a GeoJSON with no `crs` member
    as EPSG:4326. So `outSR` that is only forwarded and never recorded is the
    one way this reader can fabricate a coordinate system: with `outSR=4269` the
    frame installs labelled 4326 while holding NAD83, and nothing fires, because
    PIP-L004 compares a declaration against the numbers and degrees look like
    degrees. That is a systematic shift of about a metre — invisible on a
    preview map, wrong forever at a district boundary, and tens of metres for
    NAD27.

    So the requested system becomes the frame's declared system. There is
    nothing helpful or guessed about that: it is the one thing in the whole
    request that says what the coordinates are. What cannot be honoured — a
    spatial reference given as JSON, a WKID pyproj cannot resolve — is refused
    rather than approximated, and a service that declared a `crs` disagreeing
    with the `outSR` asked for is refused too, since there is no way to tell
    which of the two describes what actually arrived.

    ArcGIS / ArcPy equivalent
        `arcpy.FeatureSet.load(url)` gets this for free — the FeatureSet carries
        a real `spatialReference` object, so the reprojection and its label
        never come apart. GeoJSON has no such field, so the label has to be
        reattached by hand here.
    """
    if out_sr is None:
        return frame, None

    text = str(out_sr).strip()
    if not text.isdigit() or int(text) <= 0:
        raise _blocking(
            "PIP-L014",
            "The address asks the map service for a coordinate system this tool "
            "cannot record — only a plain reference number (an EPSG or ESRI "
            "well-known ID) can be. Without recording it there would be no "
            "saying what the coordinates that came back actually are, so "
            "nothing was read. Remove the outSR from the address and the layer "
            "arrives in the service's own coordinate system.",
            reason="unrecordable_out_sr",
            url=url,
        )
    wkid = int(text)

    try:
        declared = frame.set_crs(f"EPSG:{wkid}", allow_override=True)
    except Exception as error:
        raise _blocking(
            "PIP-L014",
            f"The address asks the map service for coordinate system number "
            f"{wkid}, which this tool cannot make sense of, so it could not say "
            f"what the coordinates that came back are. Nothing was read.",
            reason="unknown_out_sr",
            url=url,
            out_sr=wkid,
            error=type(error).__name__,
        ) from error

    if payload_crs is not None:
        stated = _declared_crs(frame)
        stated_epsg = stated.to_epsg() if stated is not None else None
        if stated_epsg is not None and stated_epsg != wkid:
            raise _blocking(
                "PIP-L014",
                f"The address asks for coordinate system number {wkid}, but the "
                f"map service labelled its answer {stated_epsg}. Those cannot "
                f"both describe the shapes that arrived, and installing a layer "
                f"whose coordinate system is in doubt is how boundaries end up "
                f"in the wrong place. Nothing was read.",
                reason="out_sr_conflict",
                url=url,
                out_sr=wkid,
                declared_epsg=stated_epsg,
            )

    return declared, wkid


# --------------------------------------------------------------------------
# findings the reader itself makes
# --------------------------------------------------------------------------


def _no_vintage_finding(
    source_kind: str,
    *,
    written_on: str | None = None,
    metadata_sibling: str | None = None,
) -> Finding:
    """PIP-L017 — nothing here says how old the boundaries are.

    The registry's standing text already explains why that matters. What the
    runtime sentence adds is the two things only the reader knows about a
    shapefile: the date the table was written out, said plainly as *not* the age
    of the data, and whether a .shp.xml — the one place a real publication date
    could be hiding — came along with it.
    """
    if source_kind == SOURCE_SHAPEFILE:
        written = (
            f" The only date inside it is {written_on}, which is when the file "
            f"itself was written out — an old boundary re-exported yesterday "
            f"carries yesterday's date."
            if written_on
            else ""
        )
        alongside = (
            f" There is a {metadata_sibling} alongside it, which is where a real "
            f"publication date would be recorded if anyone recorded one — open "
            f"it in a text editor and look."
            if metadata_sibling
            else ""
        )
        specifics = f"This is a shapefile.{written}{alongside}"
    elif source_kind == SOURCE_GEOJSON:
        specifics = (
            "This is a GeoJSON file, and the GeoJSON format has no field for a "
            "date anywhere in it — not for the file and not for a single area."
        )
    elif source_kind == SOURCE_GEOPACKAGE:
        specifics = (
            "This is a GeoPackage, which does have a place to record when it "
            "was last changed, and in this one that place is empty."
        )
    elif source_kind == SOURCE_ARCGIS_REST:
        specifics = (
            "This came from a map service, which may publish the date it was "
            "last edited, and this one publishes none."
        )
    else:
        specifics = "Nothing in what arrived records a date for these boundaries."
    return build_finding(
        "PIP-L017",
        specifics=specifics,
        detail={
            "source_kind": source_kind,
            "vintage": None,
            "shapefile_written_on": written_on,
            "metadata_file": metadata_sibling,
        },
    )


def _truncated_name_findings(frame: gpd.GeoDataFrame) -> list[Finding]:
    """PIP-L018 — column names cut to ten letters on the way into a shapefile.

    A name sitting exactly on the limit is the fingerprint: `ward_precinct` comes
    out of a shapefile export as `ward_preci`, and nothing in the file records
    what it used to be. The reader raises this from the columns alone, before
    the operator has picked any, so the shortened names are on screen while they
    are choosing — which is the moment it is useful.
    """
    columns = _information_columns(frame)
    at_limit = [column for column in columns if len(column) == SHAPEFILE_NAME_LIMIT]
    if not at_limit:
        return []
    return [
        build_finding(
            "PIP-L018",
            specifics=(
                f"{_join_names(at_limit)} "
                f"{'sit' if len(at_limit) > 1 else 'sits'} exactly on the "
                f"ten-letter limit, so {'they were' if len(at_limit) > 1 else 'it was'} "
                f"probably longer before this file was saved."
            ),
            detail={"columns_at_limit": at_limit, "columns": columns},
        )
    ]


# --------------------------------------------------------------------------
# facts for the preview page
# --------------------------------------------------------------------------


def _facts(
    frame: gpd.GeoDataFrame,
    *,
    source_kind: str,
    source_files: Sequence[str],
    vintage: str | None,
) -> dict[str, Any]:
    """Everything the preview page needs to describe this candidate in words.

    JSON-serializable throughout — every value passes through the same reduction
    the findings' `detail` uses — because this is rendered in a browser, and a
    numpy int64 that survives this far blows up at encoding time, a long way
    from whatever produced it.
    """
    facts: dict[str, Any] = {
        "source_kind": source_kind,
        "source_files": list(source_files),
        "vintage": vintage,
        "feature_count": int(len(frame)),
        "crs": _crs_facts(frame),
        "bounds": _bounds_facts(frame),
        "geometry_types": _geometry_type_counts(frame),
        "columns": _column_facts(frame),
    }
    return _json_safe(facts)


def _crs_facts(frame: gpd.GeoDataFrame) -> dict[str, Any]:
    """What the file says about where on Earth its shapes sit — as it says it.

    `declared: false` is a fact about the file, not a failure of this reader,
    and it is what PIP-L003 is raised from downstream. Nothing here fills it in.
    """
    crs = _declared_crs(frame)
    if crs is None:
        return {"declared": False, "name": None, "epsg": None, "text": None,
                "is_geographic": None, "is_projected": None}
    try:
        return {
            "declared": True,
            "name": crs.name,
            "epsg": crs.to_epsg(),
            "text": crs.to_string(),
            "is_geographic": bool(crs.is_geographic),
            "is_projected": bool(crs.is_projected),
        }
    except Exception:  # pragma: no cover - pyproj cannot describe the declaration
        return {"declared": True, "name": str(crs), "epsg": None, "text": str(crs),
                "is_geographic": None, "is_projected": None}


def _bounds_facts(frame: gpd.GeoDataFrame) -> list[float] | None:
    """The box the shapes occupy, in the file's own numbers — never converted."""
    try:
        extent = frame.total_bounds
    except (AttributeError, KeyError, ValueError):
        return None
    values = [float(value) for value in extent]
    if not all(value == value and abs(value) != float("inf") for value in values):
        return None
    return values


def _geometry_type_counts(frame: gpd.GeoDataFrame) -> dict[str, int]:
    try:
        kinds = frame.geometry.geom_type.dropna().value_counts()
    except (AttributeError, KeyError, ValueError):
        return {}
    return {str(kind): int(count) for kind, count in kinds.items()}


def _column_facts(frame: gpd.GeoDataFrame) -> list[dict[str, Any]]:
    """Each column of names and numbers, with a few real values from it.

    The samples are the point. A column called `dist_num` means nothing to a
    volunteer until they see 1, 2, 3 in it; a column that is blank for every
    area (PIP-L010) shows up here as an empty list before any check runs.
    """
    facts: list[dict[str, Any]] = []
    for column in _information_columns(frame):
        series = frame[column]
        if getattr(series, "ndim", 1) > 1:  # a repeated name — PIP-L011's business
            facts.append({"name": column, "dtype": "repeated name", "samples": []})
            continue
        filled = series[series.notna()]
        samples = [value for value in filled.head(MAX_COLUMN_SAMPLES).tolist()]
        facts.append(
            {
                "name": column,
                "dtype": str(series.dtype),
                "filled_count": int(len(filled)),
                "samples": samples,
            }
        )
    return facts


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _information_columns(frame: gpd.GeoDataFrame) -> list[str]:
    """Every column except the one holding the shapes themselves."""
    shape_column = getattr(frame, "_geometry_column_name", None)
    return [
        str(column) for column in frame.columns if str(column) != str(shape_column)
    ]


def _declared_crs(frame: gpd.GeoDataFrame) -> Any:
    """The frame's declared CRS, or None — guarded the same way `validate` guards
    it, because a spreadsheet read by GeoPandas has no `.crs` at all."""
    try:
        return frame.crs
    except (AttributeError, KeyError, ValueError):
        return None


def _display_names(
    paths: Sequence[Path], source_files: Sequence[str] | None
) -> list[str]:
    """The names to say out loud for these paths.

    An upload lands on disk as `tmp8f2a1c` and no message should ever contain
    that, so the caller's list of what the operator actually sent wins whenever
    it lines up.
    """
    if source_files and len(source_files) == len(paths):
        return [str(name) for name in source_files]
    return [path.name for path in paths]


def _join_names(names: Iterable[str]) -> str:
    """`a`, `b` and `c` — the same phrasing `app.admin.validate` uses."""
    items = [f"{str(name)!r}" for name in names]
    if not items:
        return "nothing"
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _megabytes(count: int) -> str:
    return f"{count / (1024 * 1024):,.1f} MB"


def _head(value: Any, limit: int = 200) -> str:
    """The first of whatever came back, for a message that has to show it.

    Shown to the operator because the first line of an unexpected answer very
    often names the real problem — "Token Required", "Layer not found" — better
    than any sentence written in advance could.
    """
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return f"{text[:limit]}…" if len(text) > limit else text or "(nothing at all)"


__all__ = [
    "Candidate",
    "CandidateError",
    "read_candidate",
    "MAX_TOTAL_UNCOMPRESSED_BYTES",
    "MAX_MEMBER_UNCOMPRESSED_BYTES",
    "MAX_COMPRESSION_RATIO",
    "MAX_ARCHIVE_MEMBERS",
    "MAX_ARCHIVE_BYTES",
    "MAX_ARCGIS_PAGE_SIZE",
    "MAX_RESPONSE_BYTES",
    "SHAPEFILE_EXTENSIONS",
    "REQUIRED_SHAPEFILE_EXTENSIONS",
]
