"""F8-T1 — unit tests for the layer-installer registry and validator.

Every frame here is built in memory with shapely: no fixture files, no network,
no GeoPackage on disk. Each check gets a test that proves it fires when it
should AND stays quiet when it should not — a false positive here is worse than
a miss, because it stops a volunteer installing perfectly good data.
"""
import json
import re
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, Point, Polygon, box

import app.admin.validate as validate_module
from app.admin.codes import (
    LAYER_CODES,
    SEVERITIES,
    SEVERITY_BLOCKING,
    SEVERITY_WARNING,
    UnknownLayerCodeError,
    build_finding,
    get_code,
    has_blocking,
)
from app.admin.validate import (
    LARGE_FEATURE_COUNT,
    SOURCE_ARCGIS_REST,
    SOURCE_GEOJSON,
    SOURCE_GEOPACKAGE,
    SOURCE_SHAPEFILE,
    CandidateContext,
    InstalledLayer,
    validate_candidate,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_LAYERS = REPO_ROOT / "data" / "layers.gpkg"
WARD_25_PRECINCTS = REPO_ROOT / "shapefiles" / "ward25_precincts.geojson"

# A believable Chicago-ish footprint in degrees of latitude and longitude.
WEST = box(-87.80, 41.80, -87.70, 41.90)
EAST = box(-87.70, 41.80, -87.60, 41.90)

# The layer already installed on this instance, whose box covers Cook County.
POLICE_DISTRICTS = InstalledLayer(
    id="police_districts",
    name="Chicago Police Districts",
    bounds=(-88.00, 41.60, -87.50, 42.10),
)

ALL_CODES = tuple(f"PIP-L{number:03d}" for number in range(1, 21))
# Raised by the file reader in F8-T2, not by validate_candidate. PIP-L019 —
# "this file holds several maps, say which one" — belongs to the reader and not
# to the validator, because only the reader ever sees a file with more than one
# map in it: by the time `validate_candidate` runs, one has been chosen and
# there is a single frame of shapes. PIP-L020 is the validator's, and is the one
# entry in the registry that fires when nothing is wrong — the name collision it
# reports is the operator's own intention.
READER_CODES = {
    "PIP-L001",
    "PIP-L002",
    "PIP-L012",
    "PIP-L013",
    "PIP-L014",
    "PIP-L019",
}


def clean_frame() -> gpd.GeoDataFrame:
    """Two good areas, each with a ward number and a name."""
    return gpd.GeoDataFrame(
        {"ward": ["1", "2"], "name": ["First", "Second"]},
        geometry=[WEST, EAST],
        crs="EPSG:4326",
    )


def clean_context(**overrides) -> CandidateContext:
    defaults = dict(
        layer_id="wards_2026",
        display_name="Wards 2026",
        attribute_columns=("ward", "name"),
        installed_layers=(POLICE_DISTRICTS,),
        source_kind=SOURCE_GEOPACKAGE,
        vintage="2026-01-15",
    )
    defaults.update(overrides)
    return CandidateContext(**defaults)


def codes_from(frame, context) -> list[str]:
    return [finding.code for finding in validate_candidate(frame, context=context)]


# --------------------------------------------------------------------------
# the baseline: good data must sail through
# --------------------------------------------------------------------------


def test_clean_candidate_produces_no_findings_at_all():
    findings = validate_candidate(clean_frame(), context=clean_context())
    assert findings == [], [finding.code for finding in findings]
    assert has_blocking(findings) is False


# --------------------------------------------------------------------------
# PIP-L003 — no record of where on Earth the shapes sit
# --------------------------------------------------------------------------


def test_l003_fires_when_the_file_records_no_location():
    frame = gpd.GeoDataFrame(
        {"ward": ["1", "2"], "name": ["First", "Second"]},
        geometry=[WEST, EAST],
        crs=None,
    )
    findings = validate_candidate(frame, context=clean_context())
    fired = {finding.code: finding for finding in findings}
    assert "PIP-L003" in fired
    assert fired["PIP-L003"].severity == SEVERITY_BLOCKING
    # The consequence app.lookup would hand the operator, in their own terms.
    assert "wards_2026" in fired["PIP-L003"].specifics


def test_l003_silent_when_the_file_records_a_location():
    assert "PIP-L003" not in codes_from(clean_frame(), clean_context())


def test_l003_advice_names_the_pieces_that_arrived_for_a_shapefile():
    context = clean_context(
        source_kind=SOURCE_SHAPEFILE,
        source_files=("wards.shp", "wards.dbf", "wards.shx"),
    )
    frame = clean_frame().set_crs(None, allow_override=True)
    findings = {f.code: f for f in validate_candidate(frame, context=context)}
    assert "wards.shp" in findings["PIP-L003"].specifics


@pytest.mark.parametrize(
    "source_kind, must_mention",
    [
        (SOURCE_GEOPACKAGE, ".gpkg"),
        (SOURCE_GEOJSON, ".geojson"),
        (SOURCE_ARCGIS_REST, "web address"),
    ],
)
def test_l003_does_not_send_non_shapefile_users_hunting_for_a_companion_file(
    source_kind, must_mention
):
    """The registry's standing advice for PIP-L003 is "go and find the .prj",
    which only a shapefile has. A GeoPackage or GeoJSON operator sent looking
    for one would search for a file that has never existed, so the sentence
    written at runtime has to be true of the format actually in hand."""
    frame = clean_frame().set_crs(None, allow_override=True)
    context = clean_context(source_kind=source_kind, source_files=("wards.gpkg",))
    findings = {f.code: f for f in validate_candidate(frame, context=context)}
    specifics = findings["PIP-L003"].specifics
    assert must_mention in specifics
    assert ".prj" not in specifics
    assert "companion file" not in specifics or "no companion file" in specifics


def test_l003_suppresses_the_whereabouts_comparison():
    """With no stated location there is nothing to compare, so PIP-L016 must
    not pile a second, misleading complaint on top of PIP-L003."""
    frame = clean_frame().set_crs(None, allow_override=True)
    codes = codes_from(frame, clean_context())
    assert "PIP-L003" in codes
    assert "PIP-L016" not in codes


# --------------------------------------------------------------------------
# PIP-L004 — claims degrees, stores something else
# --------------------------------------------------------------------------


def test_l004_fires_when_degrees_are_claimed_but_numbers_are_huge():
    frame = gpd.GeoDataFrame(
        {"ward": ["1"], "name": ["First"]},
        geometry=[box(1_100_000, 1_900_000, 1_102_000, 1_902_000)],
        crs="EPSG:4326",
    )
    findings = {f.code: f for f in validate_candidate(frame, context=clean_context())}
    assert "PIP-L004" in findings
    assert findings["PIP-L004"].severity == SEVERITY_BLOCKING
    assert "1,902,000" in findings["PIP-L004"].specifics


def test_l004_does_not_fire_on_a_legitimate_local_grid():
    """The false-positive guard. Illinois State Plane East is measured in feet
    from a local starting point, so six-figure numbers are correct there."""
    frame = gpd.GeoDataFrame(
        {"ward": ["1"], "name": ["First"]},
        geometry=[box(1_100_000, 1_900_000, 1_102_000, 1_902_000)],
        crs="EPSG:3435",
    )
    assert "PIP-L004" not in codes_from(frame, clean_context())


def test_l004_does_not_fire_on_a_zero_to_three_sixty_longitude_convention():
    """Some degree-based data runs longitude 0–360 rather than -180–180. That
    is unusual but legitimate, and must not be blocked."""
    frame = gpd.GeoDataFrame(
        {"ward": ["1"], "name": ["First"]},
        geometry=[box(272.2, 41.8, 272.4, 41.9)],
        crs="EPSG:4326",
    )
    assert "PIP-L004" not in codes_from(frame, clean_context())


def test_l004_silent_on_ordinary_degrees():
    assert "PIP-L004" not in codes_from(clean_frame(), clean_context())


# ... and the mirror of the same mistake, which is the commoner one: real
# latitude and longitude readings wearing the name of a local grid.


def test_l004_fires_when_a_local_grid_is_claimed_but_the_numbers_are_degrees():
    """The mistake a volunteer makes when an export dialog asks "which
    projection?" and they pick the state grid whose name they recognise. The
    numbers never change; the label does. Nothing else in the tool complains,
    the layer commits, and app.lookup reprojects Chicago into southern
    Missouri, where every lookup misses silently and forever."""
    mislabelled = clean_frame().set_crs(3435, allow_override=True)
    findings = {
        f.code: f for f in validate_candidate(mislabelled, context=clean_context())
    }
    assert "PIP-L004" in findings
    # Blocks, exactly as the mirror case does: same mistake, same consequence,
    # and the consequence is total silent misplacement rather than a bad answer
    # an operator could spot.
    assert findings["PIP-L004"].severity == SEVERITY_BLOCKING
    assert findings["PIP-L004"].detail["disagreement"] == "says_grid_stores_degrees"
    assert "Illinois" in findings["PIP-L004"].specifics


def test_l004_mislabelled_layer_also_loses_the_whereabouts_comparison():
    """Once PIP-L004 fires, the stated whereabouts are not to be trusted, so
    PIP-L016 must not pile a confident geography claim on top."""
    mislabelled = clean_frame().set_crs(3435, allow_override=True)
    codes = codes_from(mislabelled, clean_context())
    assert "PIP-L004" in codes
    assert "PIP-L016" not in codes


@pytest.mark.skipif(
    not WARD_25_PRECINCTS.exists(), reason="candidate geojson not in this checkout"
)
def test_l004_catches_the_mislabelled_ward_25_precincts_file():
    """The maintainer's worked example, end to end on the real candidate file:
    truly degrees, relabelled as the Illinois grid the way an export dialog
    invites."""
    real = gpd.read_file(WARD_25_PRECINCTS)
    assert "PIP-L004" not in codes_from(real, clean_context())
    mislabelled = real.set_crs(3435, allow_override=True)
    assert "PIP-L004" in codes_from(mislabelled, clean_context())


@pytest.mark.skipif(
    not SHIPPED_LAYERS.exists(), reason="data/layers.gpkg not in this checkout"
)
@pytest.mark.parametrize("layer_name", ["police_districts", "municipalities"])
def test_l004_stays_silent_on_the_layers_this_service_actually_ships(layer_name):
    """The false-positive proof that matters. Both shipped layers are genuinely
    measured on the Illinois grid, in US survey feet, at roughly 1.09–1.95
    million units out from its starting point — three to four orders of
    magnitude above the degree envelope the mirror check looks inside."""
    frame = gpd.read_file(SHIPPED_LAYERS, layer=layer_name)
    assert frame.crs.to_epsg() == 3435
    widest = max(abs(frame.total_bounds[0]), abs(frame.total_bounds[2]))
    tallest = max(abs(frame.total_bounds[1]), abs(frame.total_bounds[3]))
    assert widest > 1_000_000 and tallest > 1_000_000
    # These layers carry their own column names, so nothing is asked of them
    # here beyond being installable: the point is a wholly clean run.
    context = clean_context(attribute_columns=())
    assert validate_candidate(frame, context=context) == []


def test_l004_does_not_fire_on_a_utm_layer_measured_in_metres():
    """UTM puts its starting point 500 km west of the zone on purpose, so no
    real easting is ever small. Another shape of legitimate grid data that has
    to sail through."""
    frame = gpd.GeoDataFrame(
        {"ward": ["1"], "name": ["First"]},
        geometry=[box(440_000, 4_630_000, 442_000, 4_632_000)],
        crs="EPSG:32616",
    )
    assert "PIP-L004" not in codes_from(frame, clean_context())


# --------------------------------------------------------------------------
# PIP-L005 — nothing drawn
# --------------------------------------------------------------------------


def test_l005_fires_on_a_file_with_zero_rows():
    frame = gpd.GeoDataFrame(
        {"ward": pd.Series([], dtype=object), "name": pd.Series([], dtype=object)},
        geometry=[],
        crs="EPSG:4326",
    )
    findings = {f.code: f for f in validate_candidate(frame, context=clean_context())}
    assert "PIP-L005" in findings
    assert findings["PIP-L005"].detail["drawn_count"] == 0


def test_l005_fires_when_rows_exist_but_nothing_is_drawn():
    frame = gpd.GeoDataFrame(
        {"ward": ["1", "2"], "name": ["First", "Second"]},
        geometry=[None, None],
        crs="EPSG:4326",
    )
    findings = {f.code: f for f in validate_candidate(frame, context=clean_context())}
    assert "PIP-L005" in findings
    assert findings["PIP-L005"].detail["row_count"] == 2


def test_l005_fires_on_a_spreadsheet_with_no_column_of_shapes_at_all():
    """A volunteer drags in a CSV or a spreadsheet export and gpd.read_file
    hands back a plain table. That is the likeliest wrong file in this whole
    feature, and it has to produce PIP-L005 rather than a stack trace — a plain
    table has no .crs attribute to consult."""
    frame = pd.DataFrame({"ward": ["1", "2"], "name": ["First", "Second"]})
    findings = {f.code: f for f in validate_candidate(frame, context=clean_context())}
    assert "PIP-L005" in findings
    assert "no column of shapes" in findings["PIP-L005"].specifics
    # Nothing may be claimed about where on Earth a table sits.
    assert "PIP-L003" not in findings
    assert "PIP-L004" not in findings


def test_l005_fires_on_a_frame_whose_column_of_shapes_is_not_set():
    """The other shape of the same wrong file: geopandas hands back a
    GeoDataFrame with no active column of shapes, and asking such a frame where
    on Earth it sits raises AttributeError instead of answering nothing."""
    frame = gpd.GeoDataFrame({"ward": [1, 2], "name": ["First", "Second"]})
    findings = {f.code: f for f in validate_candidate(frame, context=clean_context())}
    assert "PIP-L005" in findings
    assert findings["PIP-L005"].detail["row_count"] == 2
    assert findings["PIP-L005"].detail["drawn_count"] == 0
    assert "PIP-L003" not in findings


def test_l005_does_not_fire_when_shapes_exist():
    assert "PIP-L005" not in codes_from(clean_frame(), clean_context())


def test_l005_suppresses_checks_that_need_shapes():
    """With nothing drawn, the shape checks have nothing true to say."""
    frame = gpd.GeoDataFrame(
        {"ward": ["1"], "name": ["First"]}, geometry=[None], crs="EPSG:4326"
    )
    codes = codes_from(frame, clean_context())
    assert "PIP-L005" in codes
    for silent in ("PIP-L006", "PIP-L007", "PIP-L008", "PIP-L015", "PIP-L016"):
        assert silent not in codes


# --------------------------------------------------------------------------
# PIP-L006 / PIP-L007 — wrong kind of shape, and a mixture
# --------------------------------------------------------------------------


def test_l006_fires_on_points_and_lines():
    frame = gpd.GeoDataFrame(
        {"ward": ["1", "2"], "name": ["First", "Second"]},
        geometry=[Point(-87.7, 41.85), LineString([(-87.7, 41.8), (-87.6, 41.9)])],
        crs="EPSG:4326",
    )
    findings = {f.code: f for f in validate_candidate(frame, context=clean_context())}
    assert "PIP-L006" in findings
    assert "PIP-L007" not in findings  # a mixture of two non-areas is still L006
    assert set(findings["PIP-L006"].detail["kinds"]) == {"Point", "LineString"}


def test_l006_does_not_fire_on_areas():
    assert "PIP-L006" not in codes_from(clean_frame(), clean_context())


def test_l007_fires_when_areas_are_mixed_with_points():
    frame = gpd.GeoDataFrame(
        {"ward": ["1", "2"], "name": ["First", "Second"]},
        geometry=[WEST, Point(-87.7, 41.85)],
        crs="EPSG:4326",
    )
    findings = {f.code: f for f in validate_candidate(frame, context=clean_context())}
    assert "PIP-L007" in findings
    assert "PIP-L006" not in findings
    assert findings["PIP-L007"].detail["counts"]["Point"] == 1


def test_l007_does_not_fire_on_one_piece_and_multi_piece_areas_together():
    """A county with an island is stored as a multi-piece area alongside
    ordinary ones. That is normal data, not a mixture."""
    frame = gpd.GeoDataFrame(
        {"ward": ["1", "2"], "name": ["First", "Second"]},
        geometry=[WEST, gpd.GeoSeries([EAST]).union_all()],
        crs="EPSG:4326",
    )
    multi = gpd.GeoDataFrame(
        {"ward": ["1", "2"], "name": ["First", "Second"]},
        geometry=[WEST, box(-87.5, 41.8, -87.4, 41.9).union(box(-87.3, 41.8, -87.2, 41.9))],
        crs="EPSG:4326",
    )
    assert "PIP-L007" not in codes_from(frame, clean_context())
    assert set(multi.geometry.geom_type) == {"Polygon", "MultiPolygon"}
    assert "PIP-L007" not in codes_from(multi, clean_context())


# --------------------------------------------------------------------------
# PIP-L008 — outlines that cross themselves
# --------------------------------------------------------------------------


def bowtie() -> Polygon:
    """A figure-of-eight outline: it crosses its own edge in the middle."""
    return Polygon(
        [(-87.80, 41.80), (-87.70, 41.90), (-87.70, 41.80), (-87.80, 41.90)]
    )


def test_l008_fires_on_a_self_crossing_outline_and_only_warns():
    frame = gpd.GeoDataFrame(
        {"ward": ["1", "2"], "name": ["First", "Second"]},
        geometry=[EAST, bowtie()],
        crs="EPSG:4326",
    )
    findings = {f.code: f for f in validate_candidate(frame, context=clean_context())}
    assert "PIP-L008" in findings
    assert findings["PIP-L008"].severity == SEVERITY_WARNING
    assert findings["PIP-L008"].detail["broken_count"] == 1
    assert findings["PIP-L008"].detail["broken_positions"] == [1]
    assert "2nd" in findings["PIP-L008"].specifics
    # A warning alone must not stop the commit.
    assert has_blocking(list(findings.values())) is False


def test_l008_counts_rows_of_the_file_not_rows_of_what_survived_the_filter():
    """The rows with nothing drawn in them are dropped before this check runs,
    so counting along what is left points at the wrong shape. Here the only
    crossed outline is the 3rd row of the file; the 2nd row is perfectly good.
    F8-T3 highlights `broken_positions` on the preview map, so getting this
    wrong would circle a shape with nothing wrong with it."""
    frame = gpd.GeoDataFrame(
        {"ward": ["1", "2", "3"], "name": ["First", "Second", "Third"]},
        geometry=[None, EAST, bowtie()],
        crs="EPSG:4326",
    )
    findings = {f.code: f for f in validate_candidate(frame, context=clean_context())}
    assert findings["PIP-L008"].detail["broken_positions"] == [2]
    assert "3rd" in findings["PIP-L008"].specifics
    assert "2nd" not in findings["PIP-L008"].specifics
    # The two populations are different, and the finding says which is which.
    assert findings["PIP-L008"].detail["row_count"] == 3
    assert findings["PIP-L008"].detail["drawn_count"] == 2
    assert "2 shapes drawn" in findings["PIP-L008"].specifics
    assert "row of the file" in findings["PIP-L008"].specifics


def test_l008_does_not_fire_on_well_formed_outlines():
    assert "PIP-L008" not in codes_from(clean_frame(), clean_context())


# --------------------------------------------------------------------------
# PIP-L009 — the short name is taken
# --------------------------------------------------------------------------


def test_l009_fires_when_the_short_name_is_already_installed():
    context = clean_context(layer_id="police_districts")
    findings = {f.code: f for f in validate_candidate(clean_frame(), context=context)}
    assert "PIP-L009" in findings
    assert findings["PIP-L009"].severity == SEVERITY_BLOCKING
    assert "Chicago Police Districts" in findings["PIP-L009"].specifics


def test_l009_does_not_fire_on_a_free_short_name():
    assert "PIP-L009" not in codes_from(clean_frame(), clean_context())


def test_l009_does_not_fire_on_a_merely_similar_name():
    context = clean_context(layer_id="police_districts_2026")
    assert "PIP-L009" not in codes_from(clean_frame(), context)


# --------------------------------------------------------------------------
# PIP-L020 — the same name, but on purpose
# --------------------------------------------------------------------------


def test_l020_replaces_l009_when_the_layer_is_being_replaced_on_purpose():
    """A name collision with the layer you are replacing is not an error.

    Reproduced against the pre-fix validator, which had no `replacing` at all:
    the caller handed it every installed layer including the one being replaced,
    PIP-L009 fired, `has_blocking` came back true, and the Install button F8-T5
    puts behind that boolean could never be pressed. Reinstalling a layer — the
    commonest reason to open this tool a second time — was impossible.
    """
    context = clean_context(
        layer_id="police_districts",
        installed_layers=(),
        replacing=POLICE_DISTRICTS,
    )
    findings = validate_candidate(clean_frame(), context=context)
    by_code = {finding.code: finding for finding in findings}

    assert "PIP-L009" not in by_code
    assert "PIP-L020" in by_code
    assert by_code["PIP-L020"].severity == SEVERITY_WARNING
    # The point of the whole fix: the operator can act on this.
    assert has_blocking(findings) is False
    # And it is still *said*, because installed areas are about to be dropped.
    assert "Chicago Police Districts" in by_code["PIP-L020"].specifics
    assert by_code["PIP-L020"].detail["replacing_id"] == "police_districts"


def test_l009_still_blocks_a_collision_with_a_layer_you_are_not_replacing():
    """The other half. Replacing `wards` must not license overwriting
    `police_districts`, or the fix above would have removed the check."""
    context = clean_context(
        layer_id="police_districts",
        installed_layers=(POLICE_DISTRICTS,),
        replacing=InstalledLayer(id="wards", name="Wards"),
    )
    findings = validate_candidate(clean_frame(), context=context)
    by_code = {finding.code: finding for finding in findings}
    assert "PIP-L020" not in by_code
    assert by_code["PIP-L009"].severity == SEVERITY_BLOCKING
    assert has_blocking(findings) is True


def test_l020_does_not_fire_when_nothing_is_being_replaced():
    assert "PIP-L020" not in codes_from(clean_frame(), clean_context())


# --------------------------------------------------------------------------
# PIP-L010 — a chosen column is missing or blank everywhere
# --------------------------------------------------------------------------


def test_l010_fires_when_a_chosen_column_is_missing():
    context = clean_context(attribute_columns=("ward", "precinct"))
    findings = {f.code: f for f in validate_candidate(clean_frame(), context=context)}
    assert "PIP-L010" in findings
    assert findings["PIP-L010"].detail["missing_columns"] == ["precinct"]
    assert findings["PIP-L010"].detail["blank_columns"] == []
    assert "precinct" in findings["PIP-L010"].specifics


def test_l010_fires_when_a_chosen_column_is_blank_for_every_shape():
    frame = gpd.GeoDataFrame(
        {"ward": ["1", "2"], "name": [None, "   "]},
        geometry=[WEST, EAST],
        crs="EPSG:4326",
    )
    findings = {f.code: f for f in validate_candidate(frame, context=clean_context())}
    assert "PIP-L010" in findings
    assert findings["PIP-L010"].detail["blank_columns"] == ["name"]


def test_l010_does_not_fire_when_only_some_values_are_blank():
    """The real Cook County municipalities layer has areas with no name. That
    is a gap, not a broken column, and must not block an install."""
    frame = gpd.GeoDataFrame(
        {"ward": ["1", "2"], "name": [None, "Second"]},
        geometry=[WEST, EAST],
        crs="EPSG:4326",
    )
    assert "PIP-L010" not in codes_from(frame, clean_context())


def test_l010_does_not_fire_on_a_column_of_zeroes():
    """Zero is a value. Treating it as blank would reject valid ward numbers."""
    frame = gpd.GeoDataFrame(
        {"ward": [0, 0], "name": ["First", "Second"]},
        geometry=[WEST, EAST],
        crs="EPSG:4326",
    )
    assert "PIP-L010" not in codes_from(frame, clean_context())


# --------------------------------------------------------------------------
# PIP-L011 — two columns share a name
# --------------------------------------------------------------------------


def duplicate_column_frame() -> gpd.GeoDataFrame:
    table = pd.DataFrame([["1", "A"], ["2", "B"]], columns=["ward", "ward"])
    return gpd.GeoDataFrame(table, geometry=[WEST, EAST], crs="EPSG:4326")


def test_l011_fires_on_exactly_repeated_column_names():
    findings = {
        f.code: f
        for f in validate_candidate(
            duplicate_column_frame(), context=clean_context(attribute_columns=("ward",))
        )
    }
    assert "PIP-L011" in findings
    assert findings["PIP-L011"].severity == SEVERITY_BLOCKING
    assert findings["PIP-L011"].detail["repeated_columns"] == ["ward"]


def test_l011_fires_on_names_differing_only_in_capitals():
    """Saving to a GeoPackage folds these onto each other, so they collide."""
    frame = gpd.GeoDataFrame(
        {"Ward": ["1", "2"], "ward": ["3", "4"]},
        geometry=[WEST, EAST],
        crs="EPSG:4326",
    )
    findings = {
        f.code: f
        for f in validate_candidate(
            frame, context=clean_context(attribute_columns=("ward",))
        )
    }
    assert "PIP-L011" in findings
    assert findings["PIP-L011"].detail["repeated_columns"] == ["Ward", "ward"]


def test_l011_does_not_fire_on_distinct_names():
    assert "PIP-L011" not in codes_from(clean_frame(), clean_context())


def test_l011_does_not_also_raise_a_bogus_missing_column_complaint():
    """With `ward` repeated, the value is ambiguous but not absent — piling
    PIP-L010 on top would send the operator chasing the wrong problem."""
    codes = codes_from(
        duplicate_column_frame(), clean_context(attribute_columns=("ward",))
    )
    assert "PIP-L011" in codes
    assert "PIP-L010" not in codes


# --------------------------------------------------------------------------
# PIP-L015 — unusually large
# --------------------------------------------------------------------------


def test_l015_fires_above_the_comfortable_number_of_areas():
    count = LARGE_FEATURE_COUNT + 1
    squares = [
        box(-87.8 + index * 1e-4, 41.8, -87.8 + (index + 1) * 1e-4, 41.9)
        for index in range(count)
    ]
    frame = gpd.GeoDataFrame(
        {"ward": [str(index) for index in range(count)],
         "name": [f"Ward {index}" for index in range(count)]},
        geometry=squares,
        crs="EPSG:4326",
    )
    findings = {f.code: f for f in validate_candidate(frame, context=clean_context())}
    assert "PIP-L015" in findings
    assert findings["PIP-L015"].severity == SEVERITY_WARNING
    assert findings["PIP-L015"].detail["feature_count"] == count


def test_l015_does_not_fire_on_an_ordinary_layer():
    assert "PIP-L015" not in codes_from(clean_frame(), clean_context())


# --------------------------------------------------------------------------
# PIP-L016 — nowhere near the installed layers
# --------------------------------------------------------------------------


def test_l016_fires_when_the_layer_is_far_from_everything_installed():
    los_angeles = InstalledLayer(
        id="la_council", name="LA Council Districts",
        bounds=(-118.70, 33.70, -118.10, 34.30),
    )
    context = clean_context(installed_layers=(los_angeles,))
    findings = {f.code: f for f in validate_candidate(clean_frame(), context=context)}
    assert "PIP-L016" in findings
    assert findings["PIP-L016"].severity == SEVERITY_WARNING
    assert findings["PIP-L016"].detail["candidate_bounds"][0] == pytest.approx(-87.8)


def test_l016_does_not_fire_when_the_layer_overlaps_an_installed_one():
    assert "PIP-L016" not in codes_from(clean_frame(), clean_context())


def test_l016_does_not_fire_on_the_very_first_layer():
    """Nothing installed yet means nothing to be far from."""
    assert "PIP-L016" not in codes_from(
        clean_frame(), clean_context(installed_layers=())
    )


def test_l016_refuses_to_compare_a_box_smeared_across_the_antimeridian():
    """A layer straddling the -180/+180 seam has no honest min/max box: the one
    you get runs the long way round and covers the planet, so it would touch
    every installed layer and silence PIP-L016 while looking like agreement.
    The box builder says None instead, so no claim is made either way."""
    seam_crossing = gpd.GeoSeries(
        [box(178.0, -1.0, 179.9, 1.0), box(-179.9, -1.0, -178.0, 1.0)],
        crs="EPSG:4326",
    )
    assert validate_module._bounds_in_degrees(seam_crossing, seam_crossing.crs) is None
    # And an ordinary layer still gets a real box, so the guard has not simply
    # switched the check off.
    ordinary = clean_frame().geometry
    box_degrees = validate_module._bounds_in_degrees(ordinary, ordinary.crs)
    assert box_degrees is not None
    assert box_degrees[0] == pytest.approx(-87.80)
    assert box_degrees[2] == pytest.approx(-87.60)


def test_l016_makes_no_claim_about_a_seam_crossing_layer():
    """The whole-file consequence of the refusal above: silence, not a
    confident "nowhere near" drawn from a box covering the whole world."""
    frame = gpd.GeoDataFrame(
        {"ward": ["1", "2"], "name": ["First", "Second"]},
        geometry=[box(178.0, -1.0, 179.9, 1.0), box(-179.9, -1.0, -178.0, 1.0)],
        crs="EPSG:4326",
    )
    assert "PIP-L016" not in codes_from(frame, clean_context())


def test_l016_compares_across_different_ways_of_measuring():
    """A candidate measured on the Illinois grid still has to be recognised as
    sitting on top of a layer whose box is recorded in degrees."""
    frame = gpd.GeoDataFrame(
        {"ward": ["1"], "name": ["First"]},
        geometry=[box(1_100_000, 1_900_000, 1_120_000, 1_920_000)],
        crs="EPSG:3435",
    )
    assert "PIP-L016" not in codes_from(frame, clean_context())


# --------------------------------------------------------------------------
# PIP-L017 — no idea how old the data is
# --------------------------------------------------------------------------


def test_l017_fires_when_no_date_is_known():
    context = clean_context(vintage=None, source_kind=SOURCE_SHAPEFILE)
    findings = {f.code: f for f in validate_candidate(clean_frame(), context=context)}
    assert "PIP-L017" in findings
    assert findings["PIP-L017"].severity == SEVERITY_WARNING
    assert "shapefile" in findings["PIP-L017"].specifics


def test_l017_does_not_fire_when_a_date_is_known():
    assert "PIP-L017" not in codes_from(clean_frame(), clean_context())


def test_l017_treats_an_all_whitespace_date_as_no_date():
    assert "PIP-L017" in codes_from(clean_frame(), clean_context(vintage="   "))


# --------------------------------------------------------------------------
# PIP-L018 — shapefile names cut to ten letters
# --------------------------------------------------------------------------


def test_l018_fires_when_a_chosen_shapefile_column_is_too_long_to_have_survived():
    context = clean_context(
        source_kind=SOURCE_SHAPEFILE, attribute_columns=("ward_precinct",)
    )
    frame = gpd.GeoDataFrame(
        {"ward_preci": ["1", "2"]}, geometry=[WEST, EAST], crs="EPSG:4326"
    )
    findings = {f.code: f for f in validate_candidate(frame, context=context)}
    assert "PIP-L018" in findings
    assert findings["PIP-L018"].severity == SEVERITY_WARNING
    assert findings["PIP-L018"].detail["requested_over_limit"] == ["ward_precinct"]


def test_l018_fires_on_a_column_sitting_exactly_on_the_ten_letter_limit():
    frame = gpd.GeoDataFrame(
        {"ward_preci": ["1", "2"]}, geometry=[WEST, EAST], crs="EPSG:4326"
    )
    context = clean_context(
        source_kind=SOURCE_SHAPEFILE, attribute_columns=("ward_preci",)
    )
    findings = {f.code: f for f in validate_candidate(frame, context=context)}
    assert "PIP-L018" in findings
    assert findings["PIP-L018"].detail["columns_at_limit"] == ["ward_preci"]


def test_l018_does_not_fire_on_a_shapefile_with_short_names():
    context = clean_context(source_kind=SOURCE_SHAPEFILE)
    assert "PIP-L018" not in codes_from(clean_frame(), context)


def test_l018_does_not_fire_on_formats_that_keep_full_names():
    """A GeoPackage does not cut names, so the warning would be a lie."""
    frame = gpd.GeoDataFrame(
        {"ward_precinct_number": ["1", "2"]},
        geometry=[WEST, EAST],
        crs="EPSG:4326",
    )
    context = clean_context(
        source_kind=SOURCE_GEOPACKAGE, attribute_columns=("ward_precinct_number",)
    )
    assert "PIP-L018" not in codes_from(frame, context)


# --------------------------------------------------------------------------
# ordering, serialization, and the registry as a whole
# --------------------------------------------------------------------------


def broken_everything() -> tuple[gpd.GeoDataFrame, CandidateContext]:
    """A candidate that trips several blocking checks and several warnings."""
    frame = gpd.GeoDataFrame(
        {"ward": ["1", "2"], "name": [None, None]},
        geometry=[Point(-87.7, 41.85), bowtie()],
        crs="EPSG:4326",
    )
    context = clean_context(
        layer_id="police_districts",
        source_kind=SOURCE_SHAPEFILE,
        vintage=None,
        attribute_columns=("ward", "name", "ward_precinct"),
    )
    return frame, context


def test_findings_come_back_most_severe_first_and_in_a_stable_order():
    frame, context = broken_everything()
    findings = validate_candidate(frame, context=context)
    severities = [finding.severity for finding in findings]
    assert SEVERITY_BLOCKING in severities and SEVERITY_WARNING in severities
    assert severities == sorted(
        severities, key=lambda severity: 0 if severity == SEVERITY_BLOCKING else 1
    )
    # Within one severity, ordered by code, so nothing reshuffles on refresh.
    blocking = [f.code for f in findings if f.severity == SEVERITY_BLOCKING]
    warning = [f.code for f in findings if f.severity == SEVERITY_WARNING]
    assert blocking == sorted(blocking)
    assert warning == sorted(warning)
    assert validate_candidate(frame, context=context) == findings


def test_findings_survive_a_round_trip_through_json():
    frame, context = broken_everything()
    findings = validate_candidate(frame, context=context)
    encoded = json.dumps([finding.to_dict() for finding in findings])
    decoded = json.loads(encoded)
    assert [row["code"] for row in decoded] == [f.code for f in findings]
    for row in decoded:
        assert row["message"].startswith(row["what"])
        assert row["fix"] in row["message"]
        assert isinstance(row["detail"], dict)


def test_numpy_values_in_detail_are_made_json_safe():
    """Counts come out of pandas as numpy integers, which json.dumps refuses."""
    counts = clean_frame().geometry.geom_type.value_counts()
    finding = build_finding("PIP-L005", detail={"counts": counts.to_dict()})
    assert json.dumps(finding.to_dict())


def test_every_registered_code_is_present_and_fully_written():
    assert set(LAYER_CODES) == set(ALL_CODES)
    for code in ALL_CODES:
        entry = get_code(code)
        assert entry.code == code
        assert entry.severity in SEVERITIES
        for label in ("title", "what", "why", "fix"):
            text = getattr(entry, label)
            assert text and text.strip(), f"{code}.{label} is empty"
            assert len(text.split()) >= 3, f"{code}.{label} is too terse to act on"


def test_every_registry_entry_can_be_fired():
    for code in ALL_CODES:
        finding = build_finding(code, specifics="Here is the concrete detail.")
        assert finding.code == code
        assert finding.title in LAYER_CODES[code].title
        assert "Here is the concrete detail." in finding.message
        assert finding.is_blocking == (finding.severity == SEVERITY_BLOCKING)


def test_an_unknown_code_raises_rather_than_rendering_an_empty_message():
    with pytest.raises(UnknownLayerCodeError):
        get_code("PIP-L999")
    with pytest.raises(UnknownLayerCodeError):
        build_finding("PIP-L999")


def test_the_validator_never_raises_codes_that_belong_to_the_file_reader():
    frame, context = broken_everything()
    fired = {finding.code for finding in validate_candidate(frame, context=context)}
    assert not (fired & READER_CODES)


# --------------------------------------------------------------------------
# the jargon rule
# --------------------------------------------------------------------------

# Words a campaign volunteer would have to look up. The value is the set of
# plain-language phrases that count as defining the word; a word may appear only
# in a sentence that also defines it. An empty set means "never acceptable" —
# there is always a plain way to say those.
#
# Approach: split each string into sentences on terminal punctuation followed by
# a capital letter (which leaves ".shp", ".prj" and the like intact), then check
# each sentence in isolation. Deliberately mechanical — the point is that adding
# jargon to the registry later fails the build, not that the check is clever.
JARGON = {
    # One verb for the act, and "install" is it. "Commit" is what this codebase
    # calls the final step internally; to a campaign volunteer it means
    # "promise", and the registry used it and "install" interchangeably for the
    # same act. Banned outright so it cannot drift back in.
    "commit": set(),
    "commits": set(),
    "committed": set(),
    "committing": set(),
    "crs": set(),
    "epsg": set(),
    "wgs84": set(),
    "wgs 84": set(),
    "geometry": set(),
    "geometries": set(),
    "polygon": set(),
    "polygons": set(),
    "projection": {"where on earth"},
    "projected": {"where on earth"},
    "serialize": set(),
    "serialise": set(),
    "parse": set(),
    "parsed": set(),
    "parsing": set(),
    "null": set(),
    "exception": set(),
    "malformed": set(),
    "invalid": set(),
}

SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")


def sentences(text: str) -> list[str]:
    return [part for part in SENTENCE_BREAK.split(text) if part.strip()]


def jargon_offences(text: str) -> list[str]:
    """Every jargon word used in a sentence that does not also define it."""
    offences = []
    for sentence in sentences(text):
        lowered = sentence.lower()
        for word, glosses in JARGON.items():
            if not re.search(rf"\b{re.escape(word)}\b", lowered):
                continue
            if any(gloss in lowered for gloss in glosses):
                continue
            offences.append(f"{word!r} in: {sentence.strip()}")
    return offences


def test_registry_text_is_free_of_undefined_jargon():
    offences = []
    for code in ALL_CODES:
        entry = get_code(code)
        for label in ("title", "what", "why", "fix"):
            for offence in jargon_offences(getattr(entry, label)):
                offences.append(f"{code}.{label}: {offence}")
    assert offences == [], "\n".join(offences)


def test_the_runtime_detail_sentences_are_free_of_undefined_jargon_too():
    """The registry text is only half of what an operator reads; the sentence a
    check writes about their actual file is the other half."""
    frame, context = broken_everything()
    offences = []
    for finding in validate_candidate(frame, context=context):
        offences.extend(
            f"{finding.code}: {offence}"
            for offence in jargon_offences(finding.specifics)
        )
    for extra_frame, extra_context in (
        (clean_frame(), clean_context(installed_layers=(
            InstalledLayer("la", "LA Council", (-118.7, 33.7, -118.1, 34.3)),
        ))),
        (gpd.GeoDataFrame(
            {"ward": ["1"], "name": ["First"]},
            geometry=[box(1_100_000, 1_900_000, 1_102_000, 1_902_000)],
            crs="EPSG:4326",
        ), clean_context()),
        (clean_frame().set_crs(None, allow_override=True), clean_context()),
        # The mirrored PIP-L004: real readings under a grid's name.
        (clean_frame().set_crs(3435, allow_override=True), clean_context()),
        # The source-aware PIP-L003 sentences, one per format.
        (clean_frame().set_crs(None, allow_override=True),
         clean_context(source_kind=SOURCE_GEOJSON)),
        (clean_frame().set_crs(None, allow_override=True),
         clean_context(source_kind=SOURCE_ARCGIS_REST)),
        (clean_frame().set_crs(None, allow_override=True),
         clean_context(source_kind=SOURCE_SHAPEFILE,
                       source_files=("wards.shp", "wards.dbf"))),
        # A file with no column of shapes at all, and a self-crossing outline
        # sitting behind a blank row.
        (pd.DataFrame({"ward": ["1"], "name": ["First"]}), clean_context()),
        (gpd.GeoDataFrame(
            {"ward": ["1", "2", "3"], "name": ["First", "Second", "Third"]},
            geometry=[None, EAST, bowtie()],
            crs="EPSG:4326",
        ), clean_context()),
        # PIP-L020's sentence, which only exists when a layer is being replaced.
        (clean_frame(), clean_context(
            layer_id="police_districts",
            installed_layers=(),
            replacing=POLICE_DISTRICTS,
        )),
    ):
        for finding in validate_candidate(extra_frame, context=extra_context):
            offences.extend(
                f"{finding.code}: {offence}"
                for offence in jargon_offences(finding.specifics)
            )
    assert offences == [], "\n".join(offences)


# --------------------------------------------------------------------------
# the plain-language rules — one test per rule, each of which fails against
# the wording it replaced
# --------------------------------------------------------------------------


def entry_text(code: str) -> str:
    entry = get_code(code)
    return " ".join((entry.title, entry.what, entry.why, entry.fix))


def test_the_registry_uses_one_verb_for_the_act_of_installing_a_layer():
    """"Commit" and "install" were both used for the same act, in eight places
    and the rest respectively. To a volunteer "commit" means "promise"."""
    offenders = [code for code in ALL_CODES if "commit" in entry_text(code).lower()]
    assert offenders == []
    # And the verb that won is actually in use, so this is not vacuous.
    assert any("install" in entry_text(code).lower() for code in ALL_CODES)


def test_l004_says_nothing_about_magnitude_that_the_threshold_can_contradict():
    """The old text asserted the numbers "run into the hundreds of thousands",
    while the check fires from 1,000 up — so the runtime sentence beside it
    could read "1,200 to 1,400" and flatly contradict it one sentence later."""
    frame = gpd.GeoDataFrame(
        {"ward": ["1"], "name": ["First"]},
        geometry=[box(1_200.0, 1_200.0, 1_400.0, 1_400.0)],
        crs="EPSG:4326",
    )
    findings = {f.code: f for f in validate_candidate(frame, context=clean_context())}
    assert "PIP-L004" in findings
    message = findings["PIP-L004"].message
    assert "1,400" in message
    assert "hundreds of thousands" not in message


def test_l004_registry_text_covers_both_directions_of_the_mistake():
    """The check now fires both ways round, so text describing only one of them
    is factually backwards for half the layers it fires on."""
    entry = get_code("PIP-L004")
    assert "far too big" in entry.what  # says degrees, holds grid numbers
    assert "local grid" in entry.what  # says a grid, holds degrees
    # Each runtime sentence must then say which of the two this file is.
    huge = gpd.GeoDataFrame(
        {"ward": ["1"], "name": ["First"]},
        geometry=[box(1_100_000, 1_900_000, 1_102_000, 1_902_000)],
        crs="EPSG:4326",
    )
    mirrored = clean_frame().set_crs(3435, allow_override=True)
    said_degrees = {f.code: f for f in validate_candidate(huge, context=clean_context())}
    said_grid = {
        f.code: f for f in validate_candidate(mirrored, context=clean_context())
    }
    assert "says latitude and longitude" in said_degrees["PIP-L004"].specifics
    assert "says a local grid" in said_grid["PIP-L004"].specifics
    # ...and neither may contradict the paragraph it is slotted into.
    assert "wrong way round from what is described above" not in (
        said_grid["PIP-L004"].message
    )


def test_l003_registry_text_is_true_of_every_format_it_can_fire_on():
    """PIP-L003 fires for GeoPackage and GeoJSON too, and neither format has
    ever had a companion file to go and find. The .prj belongs in the runtime
    sentence, which knows the format, not in the standing text, which does
    not."""
    entry = get_code("PIP-L003")
    standing = f"{entry.title} {entry.what} {entry.why} {entry.fix}"
    assert ".prj" not in standing
    assert "shapefile" not in standing.lower()
    # The shapefile reader still gets told about the .prj, at runtime.
    frame = clean_frame().set_crs(None, allow_override=True)
    context = clean_context(
        source_kind=SOURCE_SHAPEFILE, source_files=("wards.shp", "wards.dbf")
    )
    findings = {f.code: f for f in validate_candidate(frame, context=context)}
    assert ".prj" in findings["PIP-L003"].specifics


FALLBACK_MARKERS = ("ask whoever", "ask them")


@pytest.mark.parametrize("code", ALL_CODES)
def test_every_fix_that_needs_a_mapping_program_offers_a_way_out_without_one(code):
    """The premise of this whole feature is a volunteer who has never installed
    QGIS. A fix whose only instruction is "open it in your mapping program" has
    failed for exactly the reader it was written for."""
    fix = get_code(code).fix.lower()
    if "mapping program" not in fix:
        return
    assert any(marker in fix for marker in FALLBACK_MARKERS), (
        f"{code}.fix sends the reader to a mapping program with no way out "
        f"if they have not got one"
    )


def test_l008_does_not_ask_anyone_to_count_shapes_on_a_map():
    """"the 22nd shape" is not something a human can find by eye on a preview.
    The map has to mark them; the row number is the fallback for finding the
    same shape in the file's own table."""
    frame = gpd.GeoDataFrame(
        {"ward": ["1", "2"], "name": ["First", "Second"]},
        geometry=[EAST, bowtie()],
        crs="EPSG:4326",
    )
    findings = {f.code: f for f in validate_candidate(frame, context=clean_context())}
    fix = findings["PIP-L008"].fix
    assert "the preview map marks each one" in fix
    assert "row number" in fix
    assert "look at the listed shapes on the preview map" not in fix


def test_l016_leaves_the_coordinates_out_of_the_prose_and_points_at_the_map():
    """Deciding whether 41.60°N 87.94°W is the right place is precisely the
    judgement this reader cannot make. The numbers stay in `detail` for F8-T3
    and for a support request."""
    los_angeles = InstalledLayer(
        id="la_council", name="LA Council Districts",
        bounds=(-118.70, 33.70, -118.10, 34.30),
    )
    findings = {
        f.code: f
        for f in validate_candidate(
            clean_frame(), context=clean_context(installed_layers=(los_angeles,))
        )
    }
    message = findings["PIP-L016"].message
    assert "°" not in message
    assert "does not touch" in message
    assert "does not meet" not in message
    assert "preview map" in message
    # Still machine-readable, so nothing was lost — only moved.
    assert findings["PIP-L016"].detail["candidate_bounds"][0] == pytest.approx(-87.8)


def test_l015_names_a_symptom_this_tool_can_actually_show_someone():
    """There is no memory display anywhere in this tool, so "watch the memory
    use" is an instruction that cannot be carried out."""
    fix = get_code("PIP-L015").fix.lower()
    assert "memory use" not in fix
    assert "restart" in fix


def test_l001_and_l005_do_not_describe_the_same_situation():
    """One is a file that could not be read at all; the other is a map file
    that read perfectly and has nothing drawn in it. The old L001 claimed the
    tool "opened the file", which is L005's territory exactly."""
    unreadable = get_code("PIP-L001")
    empty = get_code("PIP-L005")
    assert "opened the file" not in unreadable.what
    assert "never got as far as opening" in unreadable.what
    assert "opened correctly" in empty.what


def test_l010_does_not_read_as_reassurance_when_it_means_the_opposite():
    """"nothing would look broken" is the good news the sentence is not."""
    why = get_code("PIP-L010").why
    assert "nothing would look broken" not in why
    assert "nothing anywhere would report a problem" in why


# The three promises of user interface that this text makes, kept deliberately
# and recorded here so a later task cannot quietly drop one and leave the words
# lying. Each is a requirement on the task named beside it.
UI_PROMISES = {
    # F8-T3: highlight `detail["broken_positions"]` on the preview map.
    "PIP-L008": "the preview map marks each one",
    # F8-T3: render `detail["available_columns"]` beside the preview.
    "PIP-L010": "listed beside the preview",
    # F8-T5/T6: repair self-crossing outlines during the install step.
    "PIP-L008-repair": "straighten these out for you",
}


def test_the_text_only_promises_interface_that_is_written_down_as_a_requirement():
    assert UI_PROMISES["PIP-L008"] in get_code("PIP-L008").fix
    assert UI_PROMISES["PIP-L008-repair"] in get_code("PIP-L008").fix
    assert UI_PROMISES["PIP-L010"] in get_code("PIP-L010").fix


def test_the_jargon_check_would_actually_catch_something():
    """Guards the guard: a rule that never fires proves nothing."""
    assert jargon_offences(
        "Geometry column has undefined CRS; cannot reproject to EPSG:4326."
    )
    assert jargon_offences("The projection is missing.")
    # ...and accepts the same word once the sentence explains it.
    assert not jargon_offences(
        "Export it again with the projection turned on — the setting that "
        "records where on Earth the map belongs."
    )
