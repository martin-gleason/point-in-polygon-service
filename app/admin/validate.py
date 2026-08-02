"""F8-T1 — the pure checks behind the layer installer.

`validate_candidate` takes a frame of shapes that has already been loaded, plus
a `CandidateContext` describing what the operator chose and what is already
installed, and returns the findings. It reads no files, opens no sockets, and
imports nothing from FastAPI: the file reader (F8-T2) and the preview page
(F8-T3) sit on top of it, and this module can be exercised entirely from
synthetic shapes built in a test.

What it checks, and what it deliberately does not
    Decided here, from the loaded shapes: PIP-L003 through PIP-L011 and
    PIP-L015 through PIP-L018. PIP-L001, PIP-L002, PIP-L012, PIP-L013 and
    PIP-L014 belong to the reader — they are about bytes, zip archives and web
    addresses, none of which exist by the time a frame is in hand. All eighteen
    live in one registry, `app.admin.codes`.

The one check that must never be wrong
    PIP-L003 (no record of where on Earth the shapes sit) blocks, and blocks
    hard, because `app.lookup._LoadedLayer` raises ``layer '<id>' has no CRS``
    at construction. A layer committed without it does not degrade — it stops
    the service booting, for every layer, not just this one.

ArcGIS / ArcPy equivalent
    This is the open-source stand-in for the checks an analyst would run by hand
    before publishing: `arcpy.management.CheckGeometry` (self-crossing outlines,
    PIP-L008), `arcpy.Describe(...).spatialReference.name == "Unknown"` (the
    Define Projection prompt, PIP-L003/PIP-L004), `arcpy.GetCount_management`
    (PIP-L005/PIP-L015), `arcpy.Describe(...).shapeType` (PIP-L006/PIP-L007),
    and `arcpy.ListFields` (PIP-L010/PIP-L011/PIP-L018). The difference is that
    all of them run at once, and the result is written for someone who has never
    opened ArcGIS.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

import geopandas as gpd
import pandas as pd
import shapely
from pyproj import CRS, Transformer

from app.admin.codes import Finding, build_finding, sort_findings

# Where the file came from. It changes what advice is true: only a shapefile
# cuts column names to ten letters (PIP-L018), and only a shapefile has no place
# at all to record a date (PIP-L017).
SOURCE_SHAPEFILE = "shapefile"
SOURCE_GEOPACKAGE = "geopackage"
SOURCE_GEOJSON = "geojson"
SOURCE_ARCGIS_REST = "arcgis_rest"
SOURCE_UNKNOWN = "unknown"

# PIP-L004, first direction. Degrees of latitude and longitude cannot exceed
# 180/90, so a file that claims degrees and stores six-figure numbers is
# certainly mislabelled. The limit is set at 1000 rather than 180 on purpose: a
# handful of legitimate conventions run longitude 0–360 instead of -180–180, and
# some regional degree grids carry a false origin. Nothing legitimate reaches
# four figures in degrees, and a false block on good data is worse here than a
# missed warning.
DEGREE_SANITY_LIMIT = 1000.0

# PIP-L004, the mirror direction — a file holding real degrees while claiming to
# be measured on a local grid. This is the commoner of the two mistakes in
# practice: a program asks "which projection?", the operator picks their state's
# grid because that is the name they recognise, and the numbers in the file never
# change. It is conclusive for two independent reasons.
#
# 1. The envelope is exactly the range latitude and longitude can occupy. Values
#    inside it under a grid's name are degrees wearing a grid's label.
# 2. What the envelope implies about size. A layer whose whole extent fits
#    inside +/-180 by +/-90 grid units spans at most 360 by 180 units — 360
#    metres on a metre grid, 110 metres on a US survey foot grid. No ward,
#    precinct, district or county is that small.
#
# It is also well inside the roughly thousand-unit dead zone around a local
# grid's starting point: every US State Plane zone (and UTM, with its 500,000 m
# false easting) places that starting point hundreds of thousands of units away
# from any ground it covers, precisely so that no real coordinate is ever small
# or negative. Nothing legitimate lands here. Proven against the two layers this
# service actually ships — see tests/test_admin_validate.py.
PROJECTED_DEGREE_LONGITUDE_LIMIT = 180.0
PROJECTED_DEGREE_LATITUDE_LIMIT = 90.0

# PIP-L016. A degree box wider than half the world is not a usable statement of
# where a layer sits: either the layer really is most of the planet, or its
# corners straddle the +/-180 seam and a plain min/max box has smeared it across
# the whole globe. Such a box touches everything, which would silently suppress
# the warning rather than answer it. See `_bounds_in_degrees`.
MAX_TRUSTWORTHY_LONGITUDE_SPAN = 180.0

# PIP-L015. Above either of these the layer still installs, but it is worth a
# word about memory: every layer is held in memory for the life of the process
# (app.lookup.PolygonLookup builds one STRtree per layer at startup).
LARGE_FEATURE_COUNT = 20_000
LARGE_VERTEX_COUNT = 1_000_000

# PIP-L018. The shapefile format stores field names in a fixed 10-byte slot.
SHAPEFILE_NAME_LIMIT = 10

# How many examples to name in a finding's `specifics` before saying "and N more".
MAX_NAMED_EXAMPLES = 5


@dataclass(frozen=True)
class InstalledLayer:
    """A layer already serving on this instance, as PIP-L009 and PIP-L016 see it.

    `bounds` is (min_lon, min_lat, max_lon, max_lat) in degrees of latitude and
    longitude, or None when the caller could not work it out — an unknown box
    is simply not compared against.
    """

    id: str
    name: str
    bounds: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class CandidateContext:
    """Everything the checks need that is not in the frame of shapes itself.

    `attribute_columns` are the columns the operator picked to be reported with
    every answer — the same thing `[[layers]] attributes` holds in config.toml.
    `vintage` is whatever the reader could find out about how old the data is
    (a GeoPackage's last-change stamp, an ArcGIS service's last-edit date);
    None means nothing was found, which is the normal case for a shapefile.
    """

    layer_id: str
    display_name: str
    attribute_columns: tuple[str, ...] = ()
    installed_layers: tuple[InstalledLayer, ...] = ()
    source_kind: str = SOURCE_UNKNOWN
    vintage: str | None = None
    source_files: tuple[str, ...] = ()


def validate_candidate(
    frame: gpd.GeoDataFrame, *, context: CandidateContext
) -> list[Finding]:
    """Every problem this frame has, most severe first.

    Pure: no file is opened and no network call is made. The order is
    deterministic (severity, then code) so the preview page renders the same
    list on every refresh.
    """
    findings: list[Finding] = []

    findings.extend(_check_short_name(context))
    findings.extend(_check_columns(frame, context))
    findings.extend(_check_truncated_names(frame, context))
    findings.extend(_check_vintage(context))

    geometries = _geometry_series(frame)
    drawn, row_positions = _drawn_shapes(geometries)

    if drawn is None or len(drawn) == 0:
        findings.append(_no_shapes_finding(frame, geometries))
        # Everything below reads the shapes themselves; with none there is
        # nothing true to say about their kind, size, or whereabouts.
        #
        # A file with no column of shapes at all (a spreadsheet export, a CSV)
        # has no stated whereabouts to examine either, and asking for one is not
        # merely pointless but unsafe: pandas raises rather than answering None.
        # PIP-L005 is the whole and only true story about such a file.
        if geometries is not None:
            findings.extend(_check_location_declared(frame, context))
        return sort_findings(findings)

    location_findings = _check_location_declared(frame, context)
    findings.extend(location_findings)
    findings.extend(_check_location_plausible(frame, drawn))
    findings.extend(_check_shape_kinds(drawn))
    findings.extend(_check_self_crossing(drawn, row_positions, frame))
    findings.extend(_check_size(drawn))

    # Comparing where this layer sits against the installed ones is only
    # meaningful once we trust its stated whereabouts — which the two checks
    # above are exactly about.
    location_is_trustworthy = not any(
        found.code in ("PIP-L003", "PIP-L004")
        for found in findings
    )
    if location_is_trustworthy:
        findings.extend(_check_overlap_with_installed(frame, drawn, context))

    return sort_findings(findings)


# --------------------------------------------------------------------------
# name and column checks
# --------------------------------------------------------------------------


def _check_short_name(context: CandidateContext) -> list[Finding]:
    """PIP-L009 — the chosen layer id collides with one already installed."""
    clash = next(
        (
            installed
            for installed in context.installed_layers
            if installed.id == context.layer_id
        ),
        None,
    )
    if clash is None:
        return []
    return [
        build_finding(
            "PIP-L009",
            specifics=(
                f"The name {context.layer_id!r} already belongs to the installed "
                f"layer {clash.name!r}."
            ),
            detail={
                "requested_id": context.layer_id,
                "existing_id": clash.id,
                "existing_name": clash.name,
                "installed_ids": [
                    installed.id for installed in context.installed_layers
                ],
            },
        )
    ]


def _check_columns(
    frame: gpd.GeoDataFrame, context: CandidateContext
) -> list[Finding]:
    """PIP-L011 (repeated column names) and PIP-L010 (a chosen column that is
    missing, or blank for every shape)."""
    findings: list[Finding] = []
    columns = _information_columns(frame)

    repeated = _repeated_names(columns)
    if repeated:
        findings.append(
            build_finding(
                "PIP-L011",
                specifics=(
                    f"The repeated name{'s' if len(repeated) > 1 else ''} "
                    f"{_join_names(repeated)} "
                    f"{'appear' if len(repeated) > 1 else 'appears'} more than "
                    f"once in this file."
                ),
                detail={"repeated_columns": repeated, "columns": columns},
            )
        )

    missing: list[str] = []
    blank: list[str] = []
    for wanted in context.attribute_columns:
        if wanted not in columns:
            missing.append(wanted)
            continue
        if columns.count(wanted) > 1:
            # Ambiguous — PIP-L011 already blocks, and frame[wanted] would hand
            # back a table rather than a column.
            continue
        if _is_blank_everywhere(frame[wanted]):
            blank.append(wanted)

    if missing or blank:
        parts = []
        if missing:
            parts.append(
                f"The column{'s' if len(missing) > 1 else ''} "
                f"{_join_names(missing)} "
                f"{'are' if len(missing) > 1 else 'is'} not in this file"
            )
        if blank:
            parts.append(
                f"{'The column' if not missing else 'the column'}"
                f"{'s' if len(blank) > 1 else ''} "
                f"{_join_names(blank)} "
                f"{'are' if len(blank) > 1 else 'is'} there but blank for all "
                f"{len(frame)} areas"
            )
        findings.append(
            build_finding(
                "PIP-L010",
                specifics=f"{'; '.join(parts)}.",
                detail={
                    "missing_columns": missing,
                    "blank_columns": blank,
                    "available_columns": columns,
                },
            )
        )
    return findings


def _check_truncated_names(
    frame: gpd.GeoDataFrame, context: CandidateContext
) -> list[Finding]:
    """PIP-L018 — shapefile column names have been cut to ten letters."""
    if context.source_kind != SOURCE_SHAPEFILE:
        return []

    # Only the columns of names and numbers — the column holding the shapes
    # themselves is not something the operator picked or should see named.
    columns = _information_columns(frame)
    # A name the operator asked for that no shapefile could have kept intact.
    over_length = [
        wanted
        for wanted in context.attribute_columns
        if len(wanted) > SHAPEFILE_NAME_LIMIT
    ]
    # A name sitting exactly on the limit is the fingerprint of a cut.
    at_limit = [column for column in columns if len(column) == SHAPEFILE_NAME_LIMIT]
    if not over_length and not at_limit:
        return []

    if over_length:
        specifics = (
            f"You asked for {_join_names(over_length)}, which cannot survive in a "
            f"shapefile at that length; the columns this file actually has are "
            f"{_join_names(columns)}."
        )
    else:
        specifics = (
            f"{_join_names(at_limit)} "
            f"{'sit' if len(at_limit) > 1 else 'sits'} exactly on the ten-letter "
            f"limit, so {'they were' if len(at_limit) > 1 else 'it was'} probably "
            f"longer before the file was saved."
        )
    return [
        build_finding(
            "PIP-L018",
            specifics=specifics,
            detail={
                "requested_over_limit": over_length,
                "columns_at_limit": at_limit,
                "columns": columns,
            },
        )
    ]


def _check_vintage(context: CandidateContext) -> list[Finding]:
    """PIP-L017 — nothing in the file says how old the data is.

    Probing the real formats confirmed there is usually nothing to find: a
    shapefile's version field is always 1000 (the 1998 format number) and its
    table header date is only when the file was written out. A GeoPackage can
    carry `gpkg_contents.last_change` and an ArcGIS service can carry
    `editingInfo.lastEditDate`, but Cook County's own layer publishes neither.
    So the honest thing is to say so and send the operator to the preview.
    """
    if context.vintage and str(context.vintage).strip():
        return []
    return [
        build_finding(
            "PIP-L017",
            specifics=(
                f"Nothing in this {_source_phrase(context.source_kind)} records a "
                f"date for the boundaries in it."
            ),
            detail={"source_kind": context.source_kind, "vintage": None},
        )
    ]


# --------------------------------------------------------------------------
# shape checks
# --------------------------------------------------------------------------


def _no_shapes_finding(
    frame: gpd.GeoDataFrame, geometries: pd.Series | None
) -> Finding:
    """PIP-L005 — the file opened but holds nothing drawn."""
    row_count = len(frame)
    if geometries is None:
        specifics = "This file has no column of shapes in it at all."
    elif row_count == 0:
        specifics = "There are zero rows in this file."
    else:
        specifics = (
            f"There {'is' if row_count == 1 else 'are'} {row_count} "
            f"row{'' if row_count == 1 else 's'} in this file, but every one of "
            f"them has an empty space where its outline should be."
        )
    return build_finding(
        "PIP-L005",
        specifics=specifics,
        detail={"row_count": row_count, "drawn_count": 0},
    )


def _check_location_declared(
    frame: gpd.GeoDataFrame, context: CandidateContext
) -> list[Finding]:
    """PIP-L003 — the file does not record where on Earth its shapes sit.

    Certain, and blocking: `app.lookup._LoadedLayer.__init__` raises
    ``layer '<id>' has no CRS`` when it loads such a layer, which happens at
    startup for every configured layer, so committing one would keep the whole
    service from booting.

    The registry's standing advice for this code is written for a shapefile —
    go and find the missing .prj — because that is the format the problem
    usually arrives in. It is not true of the others: a .gpkg and a .geojson
    have no companion file at all, and a web service has no file on this
    operator's computer to repair. So the runtime sentence says what is
    actually true of the format in hand, rather than sending a GeoPackage user
    hunting for a file their format has never had.
    """
    if _declared_crs(frame) is not None:
        return []
    companion = _missing_location_advice(context)
    return [
        build_finding(
            "PIP-L003",
            specifics=(
                f"The layer you are installing as {context.layer_id!r} has no "
                f"such record.{companion}"
            ),
            detail={
                "source_kind": context.source_kind,
                "source_files": list(context.source_files),
            },
        )
    ]


def _check_location_plausible(
    frame: gpd.GeoDataFrame, drawn: gpd.GeoSeries
) -> list[Finding]:
    """PIP-L004 — what the file says about where its shapes sit contradicts the
    numbers stored in it. Both directions of the same mistake.

    Neither test is a bare magnitude test, because a bare magnitude test is
    wrong in both directions: a file measured on a local grid legitimately
    stores six-figure numbers, and a file measured in degrees legitimately
    stores two-figure ones. It is the *disagreement* between the claim and the
    numbers that is conclusive.

    Says degrees, stores huge numbers
        The original case. Caught above `DEGREE_SANITY_LIMIT`.

    Says a local grid, stores degrees
        The mirror, and the one an operator is likelier to make: asked "which
        projection?" by an export dialog, they pick their state's grid because
        it is the name they recognise, while the numbers in the file stay as
        latitude and longitude. Nothing then complains. `app.lookup` reprojects
        the layer on load and Chicago lands in southern Missouri; every lookup
        misses, silently, forever. Caught inside the degree envelope — see
        `PROJECTED_DEGREE_LONGITUDE_LIMIT` for why that is conclusive.

    Both directions block, for the same reason: the consequence is not a
    degraded answer an operator would notice but a layer that is confidently,
    completely in the wrong place while every part of the service reports
    success.

    ArcGIS / ArcPy equivalent
        `arcpy.Describe(fc).spatialReference` compared against
        `arcpy.Describe(fc).extent` — the check an analyst makes by eye when the
        Catalog pane draws a layer in the Gulf of Guinea, or when Define
        Projection has been run on a file that needed Project instead.
    """
    crs = _declared_crs(frame)
    if crs is None:
        return []
    try:
        described = CRS.from_user_input(crs)
    except Exception:  # pragma: no cover - pyproj cannot read the declaration
        return []

    min_x, min_y, max_x, max_y = (float(value) for value in drawn.total_bounds)
    extremes = [min_x, min_y, max_x, max_y]
    if not all(math.isfinite(value) for value in extremes):
        return []

    if described.is_geographic:
        worst = max(abs(value) for value in extremes)
        if worst <= DEGREE_SANITY_LIMIT:
            return []
        specifics = (
            f"This one is the first of those two: it says latitude and longitude "
            f"and holds numbers far too big to be those. They run from {min_x:,.0f} to {max_x:,.0f} across "
            f"and {min_y:,.0f} to {max_y:,.0f} up, and the largest of them is "
            f"{worst:,.0f}."
        )
        detail = {
            "disagreement": "says_degrees_stores_grid_numbers",
            "declared": _crs_label(crs),
            "bounds": [min_x, min_y, max_x, max_y],
            "largest_magnitude": worst,
            "degree_limit": DEGREE_SANITY_LIMIT,
        }
    elif described.is_projected:
        widest = max(abs(min_x), abs(max_x))
        tallest = max(abs(min_y), abs(max_y))
        if (
            widest > PROJECTED_DEGREE_LONGITUDE_LIMIT
            or tallest > PROJECTED_DEGREE_LATITUDE_LIMIT
        ):
            return []
        specifics = (
            f"This one is the second of those two: it says a local grid "
            f"({_crs_label(crs)!r}) and holds plain latitude and longitude. Its "
            f"numbers run from {min_x:,.4f} to {max_x:,.4f} across and "
            f"{min_y:,.4f} to {max_y:,.4f} up, which are ordinary readings for a "
            f"real place. Nothing real ever sits this close to such a grid's "
            f"starting point — a whole layer this small would be a few hundred "
            f"metres across — so the numbers are the honest part and the label "
            f"is wrong."
        )
        detail = {
            "disagreement": "says_grid_stores_degrees",
            "declared": _crs_label(crs),
            "bounds": [min_x, min_y, max_x, max_y],
            "widest": widest,
            "tallest": tallest,
            "longitude_limit": PROJECTED_DEGREE_LONGITUDE_LIMIT,
            "latitude_limit": PROJECTED_DEGREE_LATITUDE_LIMIT,
        }
    else:
        # Neither kind of claim (a bare vertical or engineering declaration).
        # Nothing to contradict, so nothing conclusive to say.
        return []

    return [build_finding("PIP-L004", specifics=specifics, detail=detail)]


def _check_shape_kinds(drawn: gpd.GeoSeries) -> list[Finding]:
    """PIP-L006 (nothing enclosed) and PIP-L007 (a mixture)."""
    kinds = sorted({str(kind) for kind in drawn.geom_type.dropna().unique()})
    # Polygon and MultiPolygon are the same thing to an operator — one area
    # versus one area drawn in two pieces — so they are not a mixture.
    families = {kind.removeprefix("Multi") for kind in kinds}

    if "GeometryCollection" in kinds:
        return [
            build_finding(
                "PIP-L007",
                specifics=(
                    f"At least one entry in this file bundles several different "
                    f"kinds of thing into a single entry. What the file holds: "
                    f"{_join_plain(_plain_kinds(kinds))}."
                ),
                detail={"kinds": kinds, "counts": _kind_counts(drawn)},
            )
        ]

    if "Polygon" not in families:
        return [
            build_finding(
                "PIP-L006",
                specifics=(
                    f"All {len(drawn)} of the things drawn in this file are "
                    f"{_join_plain(_plain_kinds(kinds))}."
                ),
                detail={"kinds": kinds, "counts": _kind_counts(drawn)},
            )
        ]

    if len(families) > 1:
        counts = _kind_counts(drawn)
        non_areas = {
            kind: count
            for kind, count in counts.items()
            if kind.removeprefix("Multi") != "Polygon"
        }
        return [
            build_finding(
                "PIP-L007",
                specifics=(
                    f"Out of {len(drawn)} things drawn, "
                    f"{sum(non_areas.values())} enclose nothing — they are "
                    f"{_join_plain(_plain_kinds(sorted(non_areas)))}."
                ),
                detail={"kinds": kinds, "counts": counts},
            )
        ]
    return []


def _check_self_crossing(
    drawn: gpd.GeoSeries, row_positions: list[int], frame: gpd.GeoDataFrame
) -> list[Finding]:
    """PIP-L008 — outlines that cross their own edge. Repairable, so a warning.

    `drawn` has had the rows with nothing in them dropped, so counting along it
    does not give the row an operator would find in their file, and F8-T3 needs
    the row: `broken_positions` is what the preview map highlights with. A file
    whose rows are [nothing, a good area, a crossed one] must point at the third
    row, not the second. `row_positions` carries each surviving shape's place in
    the original file, and is what goes into the finding.

    The two counts in the sentence are different populations on purpose — how
    many of the *drawn* shapes cross themselves, and which *rows* of the file
    those are — so each one says which it is.
    """
    valid = drawn.is_valid
    broken_rows = [
        row_positions[position]
        for position, ok in enumerate(valid.to_numpy())
        if not ok
    ]
    if not broken_rows:
        return []
    shown = broken_rows[:MAX_NAMED_EXAMPLES]
    more = len(broken_rows) - len(shown)
    tail = f", and {more} more" if more > 0 else ""
    return [
        build_finding(
            "PIP-L008",
            specifics=(
                f"{len(broken_rows)} of the {len(drawn)} shapes drawn in this "
                f"file do this. Counting every row of the file from the top, "
                f"including any row with nothing drawn in it, they are the "
                f"{_ordinal_list(shown)}{tail}."
            ),
            detail={
                "broken_count": len(broken_rows),
                "drawn_count": len(drawn),
                "row_count": len(frame),
                # Deliberately positions in the file, not in the drawn subset.
                "broken_positions": broken_rows[:100],
            },
        )
    ]


def _check_size(drawn: gpd.GeoSeries) -> list[Finding]:
    """PIP-L015 — more areas, or more detail, than this service wants in memory."""
    feature_count = len(drawn)
    vertex_count = int(shapely.get_num_coordinates(drawn.to_numpy()).sum())
    if feature_count <= LARGE_FEATURE_COUNT and vertex_count <= LARGE_VERTEX_COUNT:
        return []
    return [
        build_finding(
            "PIP-L015",
            specifics=(
                f"This one has {feature_count:,} areas drawn with "
                f"{vertex_count:,} corner points between them; the comfortable "
                f"limits are {LARGE_FEATURE_COUNT:,} areas and "
                f"{LARGE_VERTEX_COUNT:,} corner points."
            ),
            detail={
                "feature_count": feature_count,
                "vertex_count": vertex_count,
                "feature_limit": LARGE_FEATURE_COUNT,
                "vertex_limit": LARGE_VERTEX_COUNT,
            },
        )
    ]


def _check_overlap_with_installed(
    frame: gpd.GeoDataFrame, drawn: gpd.GeoSeries, context: CandidateContext
) -> list[Finding]:
    """PIP-L016 — this layer covers ground nowhere near the installed ones.

    The "you loaded the neighbouring county" check. A warning only: adding a
    genuinely new place is a normal thing to do.
    """
    known = [
        installed for installed in context.installed_layers if installed.bounds
    ]
    if not known:
        return []

    candidate_box = _bounds_in_degrees(drawn, frame.crs)
    if candidate_box is None:
        return []

    overlapping = [
        installed.id
        for installed in known
        if _boxes_touch(candidate_box, installed.bounds)
    ]
    if overlapping:
        return []

    return [
        build_finding(
            "PIP-L016",
            specifics=(
                # Deliberately no latitude and longitude readings: deciding
                # whether 41.60°N 87.94°W is the right place is exactly the
                # judgement this reader cannot make, and the preview map makes
                # it for them at a glance. The numbers stay in `detail`, where
                # F8-T3 can draw the box and a support request can quote it.
                f"The ground it covers does not touch "
                f"{_join_names([installed.name for installed in known])}; the "
                f"preview map shows where it does sit."
            ),
            detail={
                "candidate_bounds": list(candidate_box),
                "installed": [
                    {"id": installed.id, "name": installed.name,
                     "bounds": list(installed.bounds)}
                    for installed in known
                ],
            },
        )
    ]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _information_columns(frame: gpd.GeoDataFrame) -> list[str]:
    """Every column except the one holding the shapes themselves."""
    shape_column = getattr(frame, "_geometry_column_name", None)
    return [
        str(column) for column in frame.columns if str(column) != str(shape_column)
    ]


def _geometry_series(frame: gpd.GeoDataFrame) -> gpd.GeoSeries | None:
    """The frame's active shape column, or None if it has none set."""
    try:
        return frame.geometry
    except (AttributeError, KeyError, ValueError):
        return None


def _declared_crs(frame: gpd.GeoDataFrame) -> Any:
    """What the frame says about where on Earth its shapes sit, or None.

    Guarded, because `frame.crs` is not a safe attribute to reach for. A plain
    pandas DataFrame — which is what `gpd.read_file` hands back for a CSV or a
    spreadsheet export, the likeliest wrong file in this whole feature — has no
    `.crs` at all, and a GeoDataFrame with no active column of shapes raises
    AttributeError rather than answering None. Every check that wants the
    declaration comes through here so that neither reaches an operator as a
    stack trace.

    ArcGIS / ArcPy equivalent
        `arcpy.Describe(dataset).spatialReference`, which likewise raises on a
        table rather than a feature class; the equivalent guard is checking
        `arcpy.Describe(dataset).dataType` first.
    """
    try:
        return frame.crs
    except (AttributeError, KeyError, ValueError):
        return None


def _drawn_shapes(
    geometries: gpd.GeoSeries | None,
) -> tuple[gpd.GeoSeries | None, list[int]]:
    """The rows that have something drawn in them, and where each one sits.

    The second value is each surviving shape's position in the original frame,
    counting from zero: rows with nothing in them are dropped from the shapes
    but are still rows of the operator's file, and a finding that names "the
    2nd" has to mean the 2nd row of the file they can open.
    """
    if geometries is None:
        return None, []
    present = geometries.notna()
    if not bool(present.any()):
        return geometries[present], []
    drawn_mask = present.copy()
    drawn_mask[present] = ~geometries[present].is_empty
    row_positions = [
        int(position) for position, kept in enumerate(drawn_mask.to_numpy()) if kept
    ]
    return geometries[drawn_mask], row_positions


def _repeated_names(columns: list[str]) -> list[str]:
    """Column names that occur more than once, comparing without capitalization.

    Case matters: saving to a GeoPackage folds names that differ only in case
    onto each other, so `Ward` and `ward` collide there even though pandas keeps
    them apart here.
    """
    seen: dict[str, list[str]] = {}
    for column in columns:
        seen.setdefault(column.casefold(), []).append(column)
    return sorted(
        {name for group in seen.values() if len(group) > 1 for name in group}
    )


def _is_blank_everywhere(column: pd.Series) -> bool:
    """True when every cell is missing, or is a string with nothing but spaces."""
    if len(column) == 0:
        return False
    filled = column[column.notna()]
    if len(filled) == 0:
        return True
    as_text = filled.astype(str).str.strip()
    return bool((as_text == "").all())


# The library's names for kinds of shape, said the way an operator would.
_PLAIN_KINDS = {
    "Point": "single spots",
    "MultiPoint": "clusters of spots",
    "LineString": "lines",
    "MultiLineString": "sets of lines",
    "LinearRing": "lines",
    "Polygon": "enclosed areas",
    "MultiPolygon": "enclosed areas drawn in more than one piece",
    "GeometryCollection": "mixed bundles of spots, lines and areas",
}


def _plain_kinds(kinds: Iterable[str]) -> list[str]:
    return [_PLAIN_KINDS.get(kind, "shapes of a kind this tool does not know")
            for kind in kinds]


def _kind_counts(drawn: gpd.GeoSeries) -> dict[str, int]:
    counts = drawn.geom_type.value_counts()
    return {str(kind): int(count) for kind, count in counts.items()}


def _bounds_in_degrees(
    drawn: gpd.GeoSeries, crs: Any
) -> tuple[float, float, float, float] | None:
    """The candidate's box as (min_lon, min_lat, max_lon, max_lat) in degrees,
    or None when no honest box can be drawn.

    Only the four corners are converted — the whole layer never is, because this
    runs while an operator waits.

    The antimeridian, and why this refuses rather than guesses
        A plain min/max box cannot describe a layer that straddles the -180/+180
        seam. Alaska's western islands sit at +179 and -179, and the box around
        them runs the long way round: -180 to +180, the whole planet. That box
        touches every installed layer, so PIP-L016 would fall silent and the
        operator would read that silence as "this is where I expected". The
        check would be wrong without ever saying so.

        Covering the seam properly means a two-part box threaded through
        `_boxes_touch`, for a case a Cook County deployment cannot produce. So
        instead this refuses: a box spanning more than
        `MAX_TRUSTWORTHY_LONGITUDE_SPAN` is either seam-smeared or genuinely
        half the world, and in neither case is "nowhere near" a claim this
        function can support. The caller then makes no claim at all, which is
        the same outward result for a global layer and an honest one for a
        seam-crossing layer.
    """
    if crs is None:
        return None
    min_x, min_y, max_x, max_y = (float(value) for value in drawn.total_bounds)
    if not all(math.isfinite(value) for value in (min_x, min_y, max_x, max_y)):
        return None
    try:
        transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        xs, ys = transformer.transform(
            [min_x, min_x, max_x, max_x], [min_y, max_y, min_y, max_y]
        )
    except Exception:  # pragma: no cover - pyproj refuses the conversion
        return None
    corners = list(zip(xs, ys))
    if not all(math.isfinite(x) and math.isfinite(y) for x, y in corners):
        return None
    west = min(x for x, _ in corners)
    east = max(x for x, _ in corners)
    if east - west > MAX_TRUSTWORTHY_LONGITUDE_SPAN:
        return None
    return (
        west,
        min(y for _, y in corners),
        east,
        max(y for _, y in corners),
    )


def _boxes_touch(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    """Do these two boxes share any ground at all?"""
    return not (
        left[2] < right[0]
        or left[0] > right[2]
        or left[3] < right[1]
        or left[1] > right[3]
    )


def _crs_label(crs: Any) -> str:
    try:
        return CRS.from_user_input(crs).name
    except Exception:  # pragma: no cover - defensive
        return str(crs)


def _missing_location_advice(context: CandidateContext) -> str:
    """PIP-L003's next step, said for the format actually in hand.

    The registry's `fix` is deliberately format-neutral: it says what is missing
    and who can put it back, in words that are true of every format this tool
    accepts. The concrete next step is not — only a shapefile keeps this record
    in a companion file, so only a shapefile reader should be sent hunting for
    one, and a web service has no file on this operator's computer at all. The
    registry cannot say four different things at once, so the sentence written
    at runtime says the one that is true of the file in hand.
    """
    if context.source_kind == SOURCE_SHAPEFILE:
        arrived = (
            f" The pieces that arrived were "
            f"{_join_names(list(context.source_files))}."
            if context.source_files
            else ""
        )
        return (
            " A shapefile is a set of files that have to travel together, and "
            "the one that carries this record is the piece whose name ends in "
            ".prj. It is not among the pieces that arrived, so ask whoever sent "
            "you the file for that piece as well."
            f"{arrived}"
        )
    if context.source_kind == SOURCE_GEOPACKAGE:
        return (
            " A .gpkg keeps that record inside the one file, so there is no "
            "companion file for you to go and find: open this file in your "
            "mapping program, set where on Earth its shapes belong, and save it "
            "again."
        )
    if context.source_kind == SOURCE_GEOJSON:
        return (
            " A .geojson has no companion file either. This kind of file is "
            "meant to hold plain latitude and longitude readings and to say so "
            "inside itself, and this one says nothing, so open it in your "
            "mapping program and export it again."
        )
    if context.source_kind == SOURCE_ARCGIS_REST:
        return (
            " This came from a web address rather than a file, so there is "
            "nothing on your own computer to repair — ask whoever publishes it, "
            "or download the same data as a .gpkg and send that instead."
        )
    return ""


def _source_phrase(source_kind: str) -> str:
    return {
        SOURCE_SHAPEFILE: "shapefile",
        SOURCE_GEOPACKAGE: "GeoPackage file",
        SOURCE_GEOJSON: "GeoJSON file",
        SOURCE_ARCGIS_REST: "web service",
        SOURCE_UNKNOWN: "file",
    }.get(source_kind, "file")


def _join_names(names: Iterable[str]) -> str:
    """`a`, `b` and `c` — quoted, so a name with a space in it still reads right."""
    return _join_plain([f"{str(name)!r}" for name in names])


def _join_plain(words: Iterable[str]) -> str:
    """`a`, `b` and `c`, with nothing added around each item."""
    items = list(words)
    if not items:
        return "nothing"
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _ordinal_list(positions: list[int]) -> str:
    """Row positions as an operator counts them: 0 -> '1st'."""
    return _join_plain(_ordinal(position + 1) for position in positions)


def _ordinal(number: int) -> str:
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


__all__ = [
    "CandidateContext",
    "InstalledLayer",
    "validate_candidate",
    "SOURCE_SHAPEFILE",
    "SOURCE_GEOPACKAGE",
    "SOURCE_GEOJSON",
    "SOURCE_ARCGIS_REST",
    "SOURCE_UNKNOWN",
]
