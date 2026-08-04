"""F8-T3 — unit tests for the preview payload.

Offline throughout: the two layers this service ships (`data/layers.gpkg`) and
the ward-25 precinct file (`shapefiles/ward25_precincts.geojson`), plus frames
built in memory. Nothing here opens a socket.

The tests that matter most are the fidelity ones. Automated validation cannot
detect stale data — a superseded boundary file passes every mechanical check
there is — so the operator looking at the preview is the only control, and a
preview that smooths a real misalignment into apparent agreement is worse than
no preview at all. `test_a_ten_metre_shift_survives_being_drawn` and its
neighbours are what stop that: they take a real layer, move a copy of it by a
realistic distance, draw both, and prove the two are still measurably apart with
an order of magnitude between the shift and the error drawing introduced.
"""
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import shapely
from shapely.geometry import MultiPolygon, Point, Polygon, box, shape

from app.admin.preview import (
    COORDINATE_DECIMALS,
    FIDELITY_CEILING_METRES,
    REVEALS_OFFSET_METRES,
    UNCOMPARABLE_NO_CRS,
    DrawableLayer,
    build_preview,
    load_installed_layers,
)
from app.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_LAYERS = REPO_ROOT / "data" / "layers.gpkg"
WARD_25_PRECINCTS = REPO_ROOT / "shapefiles" / "ward25_precincts.geojson"

needs_shipped_layers = pytest.mark.skipif(
    not SHIPPED_LAYERS.exists(),
    reason="data/layers.gpkg not built (run scripts/build_data.py)",
)
needs_ward_25 = pytest.mark.skipif(
    not WARD_25_PRECINCTS.exists(), reason="shapefiles/ward25_precincts.geojson absent"
)


# --------------------------------------------------------------------------
# helpers — measurement, kept independent of the module under test
# --------------------------------------------------------------------------


def equal_distance_crs(lon: float, lat: float) -> str:
    """An azimuthal equidistant projection: distance from the centre is metres."""
    return f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs"


CHICAGO_METRIC = equal_distance_crs(-87.73, 41.83)


def separation_metres(left, right, crs: str = CHICAGO_METRIC) -> float:
    """The furthest any corner of `left` sits from the outline of `right`.

    Deliberately reimplemented here rather than imported: a fidelity test that
    borrows the module's own ruler proves only that the module agrees with
    itself.
    """
    projected_left = gpd.GeoSeries(list(left), crs="EPSG:4326").to_crs(crs)
    projected_right = gpd.GeoSeries(list(right), crs="EPSG:4326").to_crs(crs)
    worst = 0.0
    for one, other in zip(projected_left.values, projected_right.values):
        corners = shapely.points(shapely.get_coordinates(one))
        worst = max(worst, float(np.nanmax(shapely.distance(corners, other.boundary))))
    return worst


def drawn_geometries(layer) -> list:
    """The shapes a `DrawnLayer` actually carries, back as shapely objects."""
    return [shape(feature["geometry"]) for feature in layer.geojson["features"]]


def moved_north(frame: gpd.GeoDataFrame, metres: float) -> gpd.GeoDataFrame:
    """A copy of `frame` shifted `metres` north, measured on the ground.

    The shift is applied in an equal-distance projection so that "100 m" means
    100 m, not 100 of whatever unit the source happens to use.
    """
    degrees = frame.to_crs("EPSG:4326")
    metric = degrees.to_crs(CHICAGO_METRIC)
    shifted = metric.copy()
    shifted["geometry"] = metric.geometry.translate(0.0, metres)
    return shifted.to_crs("EPSG:4326")


def fingerprint(frame: gpd.GeoDataFrame) -> tuple:
    """Everything about a frame that must be identical after a preview is built.

    WKB, not equality: two shapely polygons compare equal while holding
    different vertices, and it is exactly the vertices this feature must not
    quietly change.
    """
    return (
        str(frame.crs),
        tuple(str(column) for column in frame.columns),
        tuple(
            None if geometry is None else shapely.to_wkb(geometry, include_srid=True)
            for geometry in frame.geometry.to_numpy()
        ),
    )


def shipped(layer_name: str) -> gpd.GeoDataFrame:
    return gpd.read_file(SHIPPED_LAYERS, layer=layer_name).reset_index(drop=True)


def shipped_installed(layer_name: str = "police_districts") -> tuple[DrawableLayer, ...]:
    return (DrawableLayer(id=layer_name, name=layer_name, frame=shipped(layer_name)),)


def chicago_squares() -> gpd.GeoDataFrame:
    """Two small areas in Chicago, in degrees, with a couple of columns."""
    return gpd.GeoDataFrame(
        {"ward": [25, 26], "name": ["west", "east"]},
        geometry=[box(-87.72, 41.83, -87.70, 41.85), box(-87.70, 41.83, -87.68, 41.85)],
        crs="EPSG:4326",
    )


# --------------------------------------------------------------------------
# 1. the candidate frame is never modified
# --------------------------------------------------------------------------


@needs_shipped_layers
@needs_ward_25
def test_the_candidate_frame_is_byte_identical_afterwards():
    """F8-T6 commits this object; display simplification must not reach it."""
    candidate = gpd.read_file(WARD_25_PRECINCTS)
    before = fingerprint(candidate)

    preview = build_preview(candidate, layer_id="ward25", installed=shipped_installed())

    assert fingerprint(candidate) == before
    # And the preview really did simplify something, so the assertion above is
    # not passing because nothing happened.
    assert preview.simplification.vertices_after < preview.simplification.vertices_before


@needs_shipped_layers
def test_the_installed_frames_are_byte_identical_afterwards():
    installed_frame = shipped("police_districts")
    before = fingerprint(installed_frame)

    build_preview(
        chicago_squares(),
        installed=(DrawableLayer(id="p", name="Police", frame=installed_frame),),
    )

    assert fingerprint(installed_frame) == before


# --------------------------------------------------------------------------
# 2. the simplification error is measured and bounded
# --------------------------------------------------------------------------


@needs_shipped_layers
@pytest.mark.parametrize("layer_name", ["police_districts", "municipalities"])
def test_the_displacement_on_a_shipped_layer_is_measured_and_small(layer_name):
    preview = build_preview(shipped(layer_name), layer_id=layer_name)
    report = preview.simplification

    assert report.coordinate_units == "degrees"
    assert report.tolerance_metres == pytest.approx(FIDELITY_CEILING_METRES)
    assert report.max_displacement_metres is not None
    # The measured figure runs a little over the tolerance asked for —
    # preserve_topology does not promise to stay inside it and rounding adds a
    # little — which is the whole reason it is measured rather than assumed.
    assert report.max_displacement_metres > 0.0
    assert report.max_displacement_metres <= REVEALS_OFFSET_METRES / 10.0
    assert report.reveals_promised_offset is True
    assert report.vertices_after < report.vertices_before


@needs_shipped_layers
def test_the_reported_displacement_is_the_real_one():
    """Re-measure the emitted coordinates with an independent ruler."""
    frame = shipped("police_districts")
    preview = build_preview(frame, layer_id="police_districts")

    original = frame.to_crs("EPSG:4326").geometry.to_numpy()
    drawn = drawn_geometries(preview.candidate)
    measured = separation_metres(original, drawn)

    assert measured == pytest.approx(preview.simplification.max_displacement_metres,
                                     rel=0.02)


@needs_shipped_layers
def test_the_tolerance_comes_from_the_viewport_not_from_a_constant():
    """A tiny viewport tightens the tolerance below the metre ceiling."""
    pinhead = gpd.GeoDataFrame(
        geometry=[box(-87.7000, 41.8300, -87.69995, 41.83005)], crs="EPSG:4326"
    )
    preview = build_preview(pinhead, layer_id="pinhead")

    assert preview.simplification.tolerance_metres < FIDELITY_CEILING_METRES
    assert preview.simplification.tolerance == pytest.approx(
        preview.viewport.units_per_pixel
    )


# --------------------------------------------------------------------------
# 3. simplification cannot hide a misalignment — the acceptance test
# --------------------------------------------------------------------------


@needs_shipped_layers
def test_a_hundred_metre_shift_survives_being_drawn():
    """A realistic redistricting distance is still plainly there afterwards.

    This is the acceptance test for the whole task. If simplification could
    close a gap this size, the preview would draw a stale police-district file
    and a current one as the same picture — and nothing else in this service
    would ever notice.
    """
    frame = shipped("police_districts")
    installed = shipped_installed()

    here = build_preview(frame, layer_id="candidate", installed=installed)
    there = build_preview(
        moved_north(frame, 100.0), layer_id="candidate", installed=installed
    )

    gap = separation_metres(
        drawn_geometries(here.candidate), drawn_geometries(there.candidate)
    )
    displacement = here.simplification.max_displacement_metres

    assert gap == pytest.approx(100.0, abs=1.0)
    assert gap > 10 * displacement


@needs_shipped_layers
def test_a_ten_metre_shift_survives_being_drawn():
    """The disclosure floor the module publishes, held to in metres.

    Ten metres is `REVEALS_OFFSET_METRES` — finer than any real boundary change
    and about the digitizing precision of the source. The measured margin is
    what the docstring's promise rests on.
    """
    frame = shipped("police_districts")
    here = build_preview(frame, layer_id="candidate")
    there = build_preview(moved_north(frame, REVEALS_OFFSET_METRES), layer_id="candidate")

    gap = separation_metres(
        drawn_geometries(here.candidate), drawn_geometries(there.candidate)
    )
    displacement = here.simplification.max_displacement_metres

    assert gap == pytest.approx(REVEALS_OFFSET_METRES, abs=0.5)
    # A full order of magnitude between the shift and the error drawing added.
    assert gap > 10 * displacement


@needs_shipped_layers
@pytest.mark.parametrize("metres", [6.0, 10.0, 25.0, 100.0, 250.0])
def test_the_smallest_offsets_the_preview_still_reveals(metres):
    """Measured floor: 5.4 m is where the order-of-magnitude margin runs out.

    Below about 5.4 m the shift stops standing a clear decimal place above the
    0.54 m of displacement drawing introduces, so the module promises 10 m and
    nothing finer. Everything from 6 m up is still shown with the margin intact.
    """
    frame = shipped("police_districts")
    here = build_preview(frame, layer_id="candidate")
    there = build_preview(moved_north(frame, metres), layer_id="candidate")

    gap = separation_metres(
        drawn_geometries(here.candidate), drawn_geometries(there.candidate)
    )
    assert gap == pytest.approx(metres, rel=0.05, abs=0.2)
    assert gap > 10 * here.simplification.max_displacement_metres


# --------------------------------------------------------------------------
# 4. stable feature ids for highlighting
# --------------------------------------------------------------------------


def test_every_feature_carries_its_row_position_as_its_id():
    frame = chicago_squares()
    preview = build_preview(frame, layer_id="squares", highlight=[1])

    features = preview.candidate.geojson["features"]
    assert [feature["id"] for feature in features] == [0, 1]
    assert [feature["properties"]["row"] for feature in features] == [0, 1]
    assert [feature["properties"]["highlighted"] for feature in features] == [
        False,
        True,
    ]
    assert preview.highlight == (1,)
    assert preview.highlight_not_drawn == ()


def test_row_positions_count_rows_with_nothing_drawn_in_them():
    """PIP-L008's `broken_positions` counts every row; so must the preview.

    A file whose rows are [nothing, an area, an area] must mark the third row
    when the finding says the third row — not the second, which is where a naive
    index into the drawn shapes would land.
    """
    frame = gpd.GeoDataFrame(
        {"ward": [1, 2, 3]},
        geometry=[
            None,
            box(-87.72, 41.83, -87.70, 41.85),
            box(-87.70, 41.83, -87.68, 41.85),
        ],
        crs="EPSG:4326",
    )
    preview = build_preview(frame, layer_id="gappy", highlight=[2])

    features = preview.candidate.geojson["features"]
    assert [feature["id"] for feature in features] == [1, 2]
    highlighted = [f["id"] for f in features if f["properties"]["highlighted"]]
    assert highlighted == [2]


def test_a_highlighted_row_that_was_never_drawn_is_reported_not_swallowed():
    frame = gpd.GeoDataFrame(
        {"ward": [1, 2]},
        geometry=[None, box(-87.72, 41.83, -87.70, 41.85)],
        crs="EPSG:4326",
    )
    preview = build_preview(frame, layer_id="gappy", highlight=[0, 1])

    assert preview.highlight == (0, 1)
    assert preview.highlight_not_drawn == (0,)


@needs_shipped_layers
def test_ids_survive_simplification_on_a_real_layer():
    frame = shipped("municipalities")
    preview = build_preview(frame, layer_id="municipalities")

    ids = [feature["id"] for feature in preview.candidate.geojson["features"]]
    assert ids == sorted(ids)
    assert set(ids) <= set(range(len(frame)))
    assert len(ids) == preview.candidate.feature_count


# --------------------------------------------------------------------------
# 5. candidates the validator has already condemned
# --------------------------------------------------------------------------


@needs_shipped_layers
def test_a_candidate_with_no_crs_is_drawn_alone_and_flagged_uncomparable():
    """PIP-L003. Placing it anywhere would be inventing a position for it."""
    frame = chicago_squares().set_crs(None, allow_override=True)
    preview = build_preview(frame, layer_id="nocrs", installed=shipped_installed())

    assert preview.comparable is False
    assert preview.uncomparable_reason == UNCOMPARABLE_NO_CRS
    assert preview.installed == ()
    assert preview.separation_metres is None
    assert preview.candidate.feature_count == 2
    assert preview.viewport.units == "layer units"
    assert preview.simplification.max_displacement_metres is None
    # Nothing to measure it against on the ground, so the only figure available
    # is in the file's own numbers — and it is still reported, never omitted.
    assert preview.simplification.max_displacement_units == 0.0  # two plain boxes
    assert preview.simplification.coordinate_units == "layer units"
    assert preview.simplification.reveals_promised_offset is False
    assert any("no record of where on Earth" in note for note in preview.notes)
    assert any("not drawn here" in note for note in preview.notes)


@needs_shipped_layers
@needs_ward_25
def test_a_mislabelled_crs_is_drawn_where_its_numbers_actually_put_it():
    """PIP-L004. Being visibly hundreds of kilometres away IS the diagnosis.

    The ward-25 precincts hold degrees; labelled EPSG:3435 (the Illinois State
    Plane grid, in feet) those same numbers land the layer far to the south. The
    preview does not correct that — it draws it there, frames both, and reports
    the distance so the page can say how far.
    """
    frame = gpd.read_file(WARD_25_PRECINCTS).set_crs(3435, allow_override=True)
    preview = build_preview(frame, layer_id="ward25", installed=shipped_installed())

    assert preview.comparable is True
    assert preview.overlaps_installed is False
    assert preview.separation_metres > 500_000.0
    # The viewport takes in both, or the wrong-place layer would fill the frame
    # and look perfectly ordinary.
    honest = gpd.read_file(WARD_25_PRECINCTS).total_bounds
    assert preview.viewport.min_x < preview.candidate_viewport.min_x
    assert preview.viewport.max_x > honest[2]
    assert preview.candidate_viewport.max_x < honest[0]
    assert any("away from this one" in note for note in preview.notes)


@needs_shipped_layers
@needs_ward_25
def test_an_honest_candidate_reports_no_separation_and_a_tight_own_viewport():
    frame = gpd.read_file(WARD_25_PRECINCTS)
    preview = build_preview(frame, layer_id="ward25", installed=shipped_installed())

    assert preview.overlaps_installed is True
    assert preview.separation_metres == pytest.approx(0.0)
    # Both views are offered; the page chooses, this module does not.
    assert preview.candidate_viewport.span_x < preview.viewport.span_x
    assert preview.installed_viewport is not None


# --------------------------------------------------------------------------
# 6. the viewport must not hide separation
# --------------------------------------------------------------------------


def test_the_default_viewport_contains_both_the_candidate_and_the_installed():
    installed = (
        DrawableLayer(
            id="far",
            name="Somewhere else",
            frame=gpd.GeoDataFrame(
                geometry=[box(-90.0, 38.0, -89.9, 38.1)], crs="EPSG:4326"
            ),
        ),
    )
    preview = build_preview(chicago_squares(), layer_id="near", installed=installed)

    viewport = preview.viewport
    assert viewport.min_x <= -90.0 and viewport.max_x >= -87.68
    assert viewport.min_y <= 38.0 and viewport.max_y >= 41.85
    assert preview.separation_metres > 100_000.0
    assert preview.installed[0].separation_metres == preview.separation_metres


def test_the_viewport_matches_the_pixel_box_shape_once_longitude_is_scaled():
    preview = build_preview(chicago_squares(), size=(900, 700))
    viewport = preview.viewport

    scaled_width = viewport.span_x * viewport.longitude_scale
    assert scaled_width / viewport.span_y == pytest.approx(900 / 700, rel=1e-6)
    assert 0.7 < viewport.longitude_scale < 0.8  # cos(41.8 degrees)


# --------------------------------------------------------------------------
# 7. the payload is bounded, and says when it had to be
# --------------------------------------------------------------------------


@needs_shipped_layers
def test_a_layer_too_detailed_for_the_cap_escalates_and_says_so():
    frame = shipped("municipalities")
    preview = build_preview(frame, layer_id="municipalities", max_total_vertices=5_000)
    report = preview.simplification

    assert report.vertices_after <= 5_000
    assert report.escalated is True
    assert report.escalation_rounds > 0
    assert report.tolerance_metres > FIDELITY_CEILING_METRES
    assert report.reveals_promised_offset is False
    assert any("smoothed further than usual" in note for note in preview.notes)
    assert any("corner points came down to" in note for note in preview.notes)


@needs_shipped_layers
def test_the_real_layers_fit_inside_the_cap_at_full_fidelity():
    """The shipped case must not escalate — the numbers in the module say so."""
    config = load_config()
    installed = load_installed_layers(config)
    candidate = gpd.read_file(WARD_25_PRECINCTS)

    preview = build_preview(candidate, layer_id="ward25", installed=installed)

    assert preview.simplification.escalated is False
    assert preview.simplification.reveals_promised_offset is True
    assert len(preview.to_json()) < 2_000_000


def test_coordinates_are_rounded_to_the_declared_precision():
    frame = gpd.GeoDataFrame(
        geometry=[
            Polygon(
                [
                    (-87.7000001234, 41.8300007654),
                    (-87.6800004321, 41.8300001111),
                    (-87.6800002222, 41.8500009999),
                    (-87.7000008888, 41.8500003333),
                ]
            )
        ],
        crs="EPSG:4326",
    )
    preview = build_preview(frame, layer_id="rounded")
    drawn = drawn_geometries(preview.candidate)

    for value in shapely.get_coordinates(drawn[0]).ravel():
        assert value == pytest.approx(round(value, COORDINATE_DECIMALS), abs=1e-12)


# --------------------------------------------------------------------------
# shapes that have to survive being drawn
# --------------------------------------------------------------------------


def test_a_polygon_keeps_its_hole():
    """A ward with a park cut out of it is not a ward with the park filled in."""
    outer = box(-87.80, 41.80, -87.60, 41.95).exterior.coords
    hole = box(-87.72, 41.86, -87.68, 41.90).exterior.coords
    frame = gpd.GeoDataFrame(
        geometry=[Polygon(outer, [hole])], crs="EPSG:4326"
    )
    preview = build_preview(frame, layer_id="holed")
    drawn = drawn_geometries(preview.candidate)[0]

    assert drawn.geom_type == "Polygon"
    assert len(drawn.interiors) == 1
    assert not drawn.contains(Point(-87.70, 41.88))


def test_a_multipolygon_keeps_all_its_pieces():
    frame = gpd.GeoDataFrame(
        geometry=[
            MultiPolygon(
                [
                    box(-87.80, 41.80, -87.75, 41.85),
                    box(-87.70, 41.80, -87.65, 41.85),
                    box(-87.60, 41.80, -87.55, 41.85),
                ]
            )
        ],
        crs="EPSG:4326",
    )
    preview = build_preview(frame, layer_id="islands")
    drawn = drawn_geometries(preview.candidate)[0]

    assert drawn.geom_type == "MultiPolygon"
    assert len(drawn.geoms) == 3


@needs_shipped_layers
def test_holes_in_a_real_layer_survive_at_the_shipped_tolerance():
    frame = shipped("municipalities")
    holes_before = sum(
        len(part.interiors)
        for geometry in frame.to_crs("EPSG:4326").geometry
        for part in getattr(geometry, "geoms", [geometry])
    )
    preview = build_preview(frame, layer_id="municipalities")
    holes_after = sum(
        len(part.interiors)
        for geometry in drawn_geometries(preview.candidate)
        for part in getattr(geometry, "geoms", [geometry])
    )

    assert holes_before > 0
    assert holes_after == holes_before


def test_an_empty_frame_produces_an_empty_but_valid_payload():
    frame = gpd.GeoDataFrame({"ward": []}, geometry=[], crs="EPSG:4326")
    preview = build_preview(frame, layer_id="nothing")

    assert preview.candidate.feature_count == 0
    assert preview.candidate.geojson["features"] == []
    assert preview.candidate_viewport is None
    assert preview.viewport is None
    assert preview.separation_metres is None
    assert preview.simplification.vertices_before == 0
    json.dumps(preview.to_dict())


@needs_shipped_layers
def test_an_empty_candidate_still_frames_the_installed_layers():
    frame = gpd.GeoDataFrame({"ward": []}, geometry=[], crs="EPSG:4326")
    preview = build_preview(frame, layer_id="nothing", installed=shipped_installed())

    assert preview.candidate_viewport is None
    assert preview.viewport is not None
    assert preview.installed[0].feature_count == 25
    assert preview.separation_metres is None


def test_a_degenerate_almost_point_polygon_still_produces_a_drawable_viewport():
    """One area collapsed to a speck must not yield a zero-width rectangle."""
    speck = Polygon(
        [
            (-87.700000, 41.830000),
            (-87.699999, 41.830000),
            (-87.699999, 41.830001),
            (-87.700000, 41.830001),
        ]
    )
    frame = gpd.GeoDataFrame(geometry=[speck], crs="EPSG:4326")
    preview = build_preview(frame, layer_id="speck")

    assert preview.viewport.span_x > 0.0
    assert preview.viewport.span_y > 0.0
    assert preview.simplification.vertices_after >= 0
    json.dumps(preview.to_dict())


def test_a_frame_with_no_column_of_shapes_at_all_does_not_raise():
    """A spreadsheet export — the likeliest wrong file in the whole feature."""
    frame = pd.DataFrame({"ward": [1, 2], "name": ["a", "b"]})
    preview = build_preview(frame, layer_id="spreadsheet")

    assert preview.candidate.feature_count == 0
    assert preview.comparable is False
    json.dumps(preview.to_dict())


# --------------------------------------------------------------------------
# the payload itself
# --------------------------------------------------------------------------


@needs_shipped_layers
@needs_ward_25
def test_the_payload_round_trips_through_json():
    installed = shipped_installed()
    candidate = gpd.read_file(WARD_25_PRECINCTS)
    preview = build_preview(candidate, layer_id="ward25", installed=installed,
                            highlight=[0, 4])

    encoded = preview.to_json()
    restored = json.loads(encoded)

    assert restored == preview.to_dict()
    assert restored["candidate"]["geojson"]["type"] == "FeatureCollection"
    assert restored["simplification"]["max_displacement_metres"] > 0
    assert restored["highlight"] == [0, 4]
    assert restored["installed"][0]["role"] == "installed"
    assert restored["candidate"]["role"] == "candidate"
    # Nothing numpy-shaped, nothing infinite, nothing NaN survived.
    assert "NaN" not in encoded and "Infinity" not in encoded


@needs_shipped_layers
def test_load_installed_layers_reads_the_configured_geopackage():
    config = load_config()
    layers = load_installed_layers(config)

    assert {layer.id for layer in layers} == {"police_districts", "municipalities"}
    by_id = {layer.id: layer for layer in layers}
    assert len(by_id["police_districts"].frame) == 25
    assert len(by_id["municipalities"].frame) == 173
    assert by_id["police_districts"].name == "Chicago Police Districts"


@needs_shipped_layers
def test_load_installed_layers_can_leave_out_the_layer_being_replaced():
    config = load_config()
    layers = load_installed_layers(config, exclude=["police_districts"])

    assert [layer.id for layer in layers] == ["municipalities"]


# --------------------------------------------------------------------------
# 12. the numbers on the page are measured, or they are not there
#
# The adversarial pass on F8-T3 found three ways this module printed a figure
# it had not measured. Each one produced a *reassuring* wrong answer — perfect
# fidelity, or a confident accusation — in the sentence the operator acts on,
# which is the one place the system has no other safeguard. These hold that
# line: an unmeasurable distance must come out as unknown, never as zero, and a
# distance between layers must be true anywhere on the globe.
# --------------------------------------------------------------------------


def wiggly_ring(centre_lon: float, centre_lat: float, points: int = 800) -> Polygon:
    """A polygon with enough detail to force the vertex cap to escalate."""
    angles = np.linspace(0.0, 2.0 * np.pi, points, endpoint=False)
    radius = 0.02 * (1.0 + 0.01 * np.sin(9.0 * angles))
    return Polygon(
        np.column_stack(
            [
                centre_lon + radius * np.cos(angles),
                centre_lat + radius * np.sin(angles),
            ]
        )
    )


def wiggly_rings(count: int = 6) -> list:
    return [wiggly_ring(-87.70 + 0.05 * index, 41.83) for index in range(count)]


def test_a_geometry_collection_is_measured_the_same_as_the_polygons_in_it():
    """M1. `GeometryCollection.boundary` is None, so every distance to it came
    back NaN, the non-finite ones were filtered away, and the running worst
    stayed at its initial 0.0 — a claim of perfect fidelity, printed, for a
    layer that had in fact been smoothed by hundreds of metres."""
    rings = wiggly_rings()
    plain = gpd.GeoDataFrame(geometry=rings, crs="EPSG:4326")
    collections = gpd.GeoDataFrame(
        geometry=[shapely.GeometryCollection([ring]) for ring in rings],
        crs="EPSG:4326",
    )

    from_plain = build_preview(plain, max_total_vertices=400).simplification
    from_collections = build_preview(collections, max_total_vertices=400).simplification

    assert from_plain.escalated is True
    assert from_collections.escalated is True
    # The same ground, described two ways, must measure the same.
    assert from_collections.max_displacement_metres == pytest.approx(
        from_plain.max_displacement_metres, rel=1e-9
    )
    # The bug reported 0.0 m here, and promised the offset guarantee on it.
    assert from_collections.max_displacement_metres > FIDELITY_CEILING_METRES
    assert from_collections.reveals_promised_offset is False


def test_a_shape_with_nothing_measurable_reports_unknown_not_perfect():
    """The escalation sentence is where a fabricated zero did the damage: it
    rendered as 'may sit up to 0.0 m from where it really is'."""
    frame = gpd.GeoDataFrame(
        geometry=[shapely.GeometryCollection([])] * 3, crs="EPSG:4326"
    )
    report = build_preview(frame).simplification

    assert report.max_displacement_metres is None
    assert report.displacement_unknown_reason is not None
    assert report.reveals_promised_offset is False
    assert not any("up to 0.0 m" in note for note in build_preview(frame).notes)


def test_a_line_is_measured_against_itself_not_against_its_two_ends():
    """M2. `LineString.boundary` is the pair of end points, so an untouched
    three-point line reported 20 km of displacement — a false alarm from the
    same one-line cause as M1. validate.py condemns a non-polygon layer
    (PIP-L006/L007), but the preview is still built for it, and it must not
    lie about a file it is helping the operator reject."""
    line = shapely.LineString([(0.0, 0.0), (20000.0, 0.0), (20000.0, 20000.0)])
    frame = gpd.GeoDataFrame(geometry=[line], crs="EPSG:26916")

    report = build_preview(frame).simplification

    assert report.vertices_before == 3
    assert report.vertices_after == 3  # nothing was removed …
    # … so nothing moved, bar coordinate rounding — certainly not 20 km.
    assert report.max_displacement_metres == pytest.approx(0.0, abs=0.5)


def test_the_separation_across_the_antimeridian_is_the_true_ground_distance():
    """M3. Measured on one azimuthal equidistant projection centred on the
    viewport, these two came out 2,489,180 m apart — 112x the truth — because
    averaging longitudes +179.85 and -179.85 puts the projection's origin
    halfway round the world from the data. The payload then printed 'The
    nearest layer already installed is 2,489 km away … that is the wrong file',
    which is a false accusation against a correct one."""
    truth_metres = 22_263.898  # 179.9 E to 179.9 W on the equator, WGS84
    candidate = gpd.GeoDataFrame(
        geometry=[box(179.8, -0.1, 179.9, 0.1)], crs="EPSG:4326"
    )
    installed = gpd.GeoDataFrame(
        geometry=[box(-179.9, -0.1, -179.8, 0.1)], crs="EPSG:4326"
    )

    preview = build_preview(
        candidate,
        installed=[DrawableLayer(id="other", name="other", frame=installed)],
    )

    assert preview.separation_metres == pytest.approx(truth_metres, rel=1e-4)
    assert preview.overlaps_installed is False
    assert not any("km away" in note and "2,489" in note for note in preview.notes)


def test_separation_is_geodesic_and_agrees_with_pyproj_over_chicago():
    """The ordinary case has to stay right too — the antimeridian fix must not
    be a special case bolted on beside a ruler that is wrong everywhere else."""
    from pyproj import Geod

    candidate = gpd.GeoDataFrame(
        geometry=[box(-87.72, 41.83, -87.70, 41.85)], crs="EPSG:4326"
    )
    installed = gpd.GeoDataFrame(
        geometry=[box(-87.60, 41.83, -87.58, 41.85)], crs="EPSG:4326"
    )
    # The gap runs due east along the 41.83/41.85 band; its narrowest crossing
    # is along the parallel of latitude nearer the pole.
    _, _, truth = Geod(ellps="WGS84").inv(-87.70, 41.85, -87.60, 41.85)

    preview = build_preview(
        candidate,
        installed=[DrawableLayer(id="other", name="other", frame=installed)],
    )

    assert preview.separation_metres == pytest.approx(truth, rel=1e-3)


def test_an_extent_too_large_to_measure_on_says_so_instead_of_guessing():
    """M3's other half: a single local projection is only true near its centre,
    and nothing in the payload used to say when the extent had outrun it — it
    inflated a 0.51 m fidelity headline to 29.77 m and printed that."""
    candidate = gpd.GeoDataFrame(
        geometry=[box(179.8, -0.1, 179.9, 0.1)], crs="EPSG:4326"
    )
    installed = gpd.GeoDataFrame(
        geometry=[box(-179.9, -0.1, -179.8, 0.1)], crs="EPSG:4326"
    )

    preview = build_preview(
        candidate,
        installed=[DrawableLayer(id="other", name="other", frame=installed)],
    )
    report = preview.simplification

    assert report.max_displacement_metres is None
    assert report.displacement_unknown_reason == "extent_too_large"
    assert report.reveals_promised_offset is False
    assert any("too wide for this tool to measure" in note for note in preview.notes)


@needs_shipped_layers
def test_a_county_sized_extent_is_still_measured_in_metres():
    """The refusal above must not swallow the case this tool exists for."""
    preview = build_preview(shipped("police_districts"), layer_id="police_districts")
    report = preview.simplification

    assert report.max_displacement_metres is not None
    assert report.displacement_unknown_reason is None
    assert report.reveals_promised_offset is True


def test_the_unknown_reason_is_always_given_when_there_is_no_number():
    """An unknown that does not say why is read as 'fine'."""
    frame = chicago_squares().set_crs(None, allow_override=True)
    report = build_preview(frame).simplification

    assert report.max_displacement_metres is None
    assert report.displacement_unknown_reason == "no_crs"
    assert report.max_displacement_units is not None


# --------------------------------------------------------------------------
# 13. the preview has to survive the file it is given, and finish
#
# The second adversarial pass found four ways the module failed on input it had
# already anticipated: a corner that is not a number crashed the measurement it
# was filtered out of one layer up; the measurement itself was quadratic and run
# twice, so an ordinary full-resolution county boundary took the best part of a
# minute with no bound and no progress; the escalation loop pulled a lever that
# could not reach, published a 524 km tolerance and drew a map a third over the
# cap anyway; and a shape too small to survive rounding was published as a
# feature that draws nothing at all. The first three end in a traceback or a
# hang, which an operator can at least see. The last one is the dangerous shape
# of failure this module is written against: the payload said a highlighted row
# was on the map to be looked at, and the page drew nothing there.
# --------------------------------------------------------------------------


def nan_cornered_polygon() -> object:
    """The shape `read_candidate` accepts (PIP-L017 only) and `validate_candidate`
    finds nothing blocking in — so the preview is built for it."""
    return shape(
        json.loads(
            '{"type":"Polygon","coordinates":'
            '[[[-87.7,41.9],[-87.6,41.9],[-87.6,NaN],[-87.7,41.9]]]}'
        )
    )


def test_a_corner_that_is_not_a_number_leaves_the_shape_off_the_map():
    """R1. `shapely.distance` raises GEOSException — 'Non-finite envelope bounds
    passed to index insert' — on this, and the operator got a traceback instead
    of a preview. The module had already anticipated the input: `_viewport_from`
    has always discarded non-finite bounding boxes. It just never removed the
    shape those bounds came from."""
    good = box(-87.65, 41.95, -87.60, 42.00)
    frame = gpd.GeoDataFrame(
        geometry=[nan_cornered_polygon(), good], crs="EPSG:4326"
    )

    preview = build_preview(frame, highlight=[0])

    # The bad row is named, not silently absent and not fatal …
    assert preview.candidate.dropped_positions == (0,)
    assert preview.highlight_not_drawn == (0,)
    assert any("not numbers" in note for note in preview.notes)
    # … and the rest of the file is drawn as usual.
    assert preview.candidate.feature_count == 1
    assert [feature["id"] for feature in preview.candidate.geojson["features"]] == [1]
    assert preview.simplification.max_displacement_metres == pytest.approx(0.0, abs=0.5)
    json.dumps(preview.to_dict())


def test_an_infinite_corner_is_treated_the_same_as_a_missing_one():
    """R1. Same crash, same cause: a coordinate that is not a place."""
    infinite = Polygon(
        [(-87.7, 41.9), (-87.6, 41.9), (-87.6, float("inf")), (-87.7, 41.9)]
    )
    frame = gpd.GeoDataFrame(geometry=[infinite], crs="EPSG:4326")

    preview = build_preview(frame)

    assert preview.candidate.dropped_positions == (0,)
    assert preview.candidate.feature_count == 0
    # Nothing is on the map, so there is no fidelity to claim. A zero here is
    # the claim that a blank page was drawn perfectly.
    assert preview.simplification.max_displacement_metres is None
    assert preview.simplification.max_displacement_units is None
    assert preview.simplification.displacement_unknown_reason == "not_drawn"
    assert preview.simplification.reveals_promised_offset is False


def test_a_non_finite_corner_in_an_installed_layer_is_survived_too():
    """R1. The candidate is not the only geometry that reaches the measurement."""
    installed_frame = gpd.GeoDataFrame(
        geometry=[nan_cornered_polygon(), box(-87.68, 41.86, -87.66, 41.88)],
        crs="EPSG:4326",
    )
    preview = build_preview(
        chicago_squares(),
        installed=(
            DrawableLayer(id="broken", name="broken", frame=installed_frame),
        ),
    )

    assert preview.installed[0].dropped_positions == (0,)
    assert any("not numbers" in note for note in preview.notes)
    assert preview.simplification.max_displacement_metres is not None


def coastline(points: int, seed: int = 0) -> Polygon:
    """A full-resolution shoreline: detail a half-metre tolerance cannot remove.

    An ordinary file for this tool's audience — a TIGER county boundary or a
    lakefront city limit runs to six figures of corner points.
    """
    generator = np.random.default_rng(seed)
    angles = np.linspace(0.0, 2.0 * np.pi, points, endpoint=False)
    wobble = generator.standard_normal(points).cumsum() / np.sqrt(points)
    radius = 0.05 + 0.02 * np.cos(3.0 * angles) + 0.01 * wobble
    return Polygon(
        np.column_stack(
            [-87.70 + radius * np.cos(angles), 41.83 + radius * np.sin(angles)]
        )
    )


def test_a_hundred_thousand_corner_points_are_measured_in_seconds_not_minutes():
    """R2. `shapely.distance` against a whole outline is unindexed, so the
    measurement cost points x segments and doubling the count quadrupled the
    time: 0.19 s at 5k, 3.03 s at 20k, 47 s at 100k — and the vertex cap does
    not bound it, because the cap limits what is *drawn* while this runs against
    the originals, which are never capped. The file below finishes under the cap
    and still took the best part of a minute, with no progress and no bound. The
    bound here is generous by an order of magnitude on purpose: it is not a
    benchmark, it is the difference between a page and a page that looks hung.
    """
    import time

    frame = gpd.GeoDataFrame(geometry=[coastline(100_000)], crs="EPSG:4326")

    started = time.perf_counter()
    preview = build_preview(frame)
    elapsed = time.perf_counter() - started

    assert preview.simplification.vertices_before > 100_000
    assert preview.simplification.max_displacement_metres is not None
    assert elapsed < 8.0, f"build_preview took {elapsed:.1f}s"


def test_the_indexed_measurement_returns_exactly_the_unindexed_answer():
    """R2. The index is not a sample and not an approximation. If it ever became
    one, M1's guarantee would have been traded away for speed and this module's
    headline figure would stop being an upper bound on the error."""
    from app.admin.preview import _distances_to_linework, _drawn_linework

    drawn = coastline(2_000, seed=3)
    linework = _drawn_linework(drawn)
    corners = shapely.get_coordinates(coastline(2_000, seed=4))

    indexed = _distances_to_linework(corners, linework)
    plain = shapely.distance(shapely.points(corners), linework)

    assert len(indexed) == len(corners)
    assert np.allclose(indexed, plain, rtol=0.0, atol=1e-12)


def test_the_displacement_is_measured_once_not_twice(monkeypatch):
    """R2. Half the cost of the whole preview was a second pass over the same
    geometry in the coordinates' own units — a figure the comparable payload
    reports nowhere and the page never shows."""
    from app.admin import preview as preview_module

    calls = []
    original = preview_module._worst_displacement

    def counted(before, after):
        calls.append(1)
        return original(before, after)

    monkeypatch.setattr(preview_module, "_worst_displacement", counted)
    report = build_preview(chicago_squares()).simplification

    assert report.max_displacement_metres is not None
    assert len(calls) == 1  # one layer, one measurement


def tiny_square_grid(across: int = 200, down: int = 100) -> gpd.GeoDataFrame:
    """20,000 small areas: a parcel or building-footprint file, in miniature.

    100,000 corner points, and no tolerance on Earth can bring that under the
    cap — `preserve_topology=True` will not take a ring below four points, so the
    floor is the feature count times five whatever the smoothing.
    """
    squares = [
        box(
            -87.70 + 0.001 * column,
            41.80 + 0.001 * row,
            -87.70 + 0.001 * column + 0.0002,
            41.80 + 0.001 * row + 0.0002,
        )
        for column in range(across)
        for row in range(down)
    ]
    return gpd.GeoDataFrame(geometry=squares, crs="EPSG:4326")


def test_a_layer_of_too_many_shapes_is_refused_rather_than_drawn_over_the_cap():
    """R3. The escalation loop's only lever is a distance, and a distance cannot
    remove a shape. It ran all twenty rounds, doubled the tolerance to 524,288 m
    — 524 km, published as this drawing's accuracy — still exceeded the cap by a
    third, and drew the result anyway with 'boundaries may sit up to 33.4 m from
    where it really is' beside it."""
    preview = build_preview(tiny_square_grid())
    report = preview.simplification

    # The cap is a promise, and it is kept: either everything fits or nothing is
    # drawn.
    assert report.vertices_after <= report.vertex_cap
    assert preview.undrawable_reason == "too_detailed_to_draw"
    assert preview.candidate.feature_count == 0
    # No futile doubling, and no meaningless tolerance published as fact.
    assert report.escalation_rounds == 0
    assert report.escalated is False
    assert report.tolerance_metres == pytest.approx(FIDELITY_CEILING_METRES)
    assert report.vertex_floor is not None and report.vertex_floor > report.vertex_cap
    # And nothing drawn means nothing measured — never a comfortable zero.
    assert report.max_displacement_metres is None
    assert report.max_displacement_units is None
    assert report.displacement_unknown_reason == "not_drawn"
    assert report.reveals_promised_offset is False
    assert any("nothing is drawn here at all" in note for note in preview.notes)
    assert not any("0.0 m" in note for note in preview.notes)


def test_a_layer_that_only_needs_more_smoothing_is_still_drawn():
    """R3's refusal must not swallow the case escalation exists for: too much
    detail *within* shapes is a problem a coarser tolerance really does solve."""
    frame = gpd.GeoDataFrame(geometry=wiggly_rings(), crs="EPSG:4326")

    preview = build_preview(frame, max_total_vertices=400)
    report = preview.simplification

    assert preview.undrawable_reason is None
    assert report.escalated is True
    assert 0 < report.vertices_after <= 400
    assert preview.candidate.feature_count == 6
    assert report.max_displacement_metres is not None


def test_a_shape_too_small_to_survive_rounding_is_named_not_silently_blank():
    """R4. Rounded to six decimal places a 0.05 m square becomes four coincident
    corners: not empty, so the `is_empty` check missed it. It was published as a
    feature with zero area and `is_valid` False, counted as drawn, left out of
    `dropped_positions`, and mentioned in no note — while the page rendered
    nothing where the payload said a highlighted shape was."""
    tiny = box(-87.70, 41.90, -87.70 + 5e-7, 41.90 + 5e-7)
    ordinary = box(-87.60, 41.90, -87.50, 42.00)
    frame = gpd.GeoDataFrame(geometry=[tiny, ordinary], crs="EPSG:4326")

    preview = build_preview(frame, highlight=[0])

    assert preview.candidate.dropped_positions == (0,)
    assert preview.candidate.feature_count == 1
    # The payload must not claim a highlighted row is on the map when it is not.
    assert preview.highlight_not_drawn == (0,)
    assert any("too small to draw" in note for note in preview.notes)
    # And every feature that IS published draws a mark.
    for geometry in drawn_geometries(preview.candidate):
        assert geometry.is_valid
        assert not geometry.is_empty
        assert geometry.area > 0.0


def test_a_multipolygon_of_collapsed_parts_is_not_drawn_either():
    """R4. A shape made only of parts that draw nothing draws nothing."""
    collapsed = MultiPolygon(
        [
            box(-87.70, 41.90, -87.70 + 5e-7, 41.90 + 5e-7),
            box(-87.69, 41.90, -87.69 + 5e-7, 41.90 + 5e-7),
        ]
    )
    frame = gpd.GeoDataFrame(
        geometry=[collapsed, box(-87.60, 41.90, -87.50, 42.00)], crs="EPSG:4326"
    )

    preview = build_preview(frame)

    assert preview.candidate.dropped_positions == (0,)
    assert preview.candidate.feature_count == 1
