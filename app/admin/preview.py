"""F8-T3 — the preview payload: the candidate layer drawn over the layers this
service already serves, with the error that drawing introduced measured and
written down.

Why this module exists, and what it is not allowed to do
    Every check in `app.admin.validate` is mechanical, and no mechanical check
    can detect stale data. A superseded police-district file is valid in every
    way a machine can test: it declares a coordinate reference system, it holds
    closed areas, it carries the columns the operator asked for. Nothing will
    ever flag it. The only control this service has is the operator looking at
    the candidate drawn over what is already installed and noticing that the
    boundaries no longer line up.

    That makes fidelity the whole job. A preview that smooths a real
    misalignment into apparent agreement is worse than no preview at all,
    because it manufactures confidence in the one place the system has no other
    safeguard. So every reduction this module performs is measured on the ground
    and reported in the payload:

    * `Simplification.max_displacement_metres` is the worst distance any point
      of any original boundary sits from the boundary actually drawn, measured
      after simplification *and* after coordinate rounding, in metres on an
      equal-distance projection. It is not the tolerance asked for; it is what
      was done.
    * `Simplification.escalated` says when the vertex cap forced a coarser
      tolerance than the fidelity ceiling, so the page can stop claiming the
      guarantee.
    * `DrawnLayer.dropped_positions` names every row that did not survive.

    Nothing here is silent, and nothing here is approximate without saying by
    how much.

    "Silent" includes drawing nothing where the payload said a shape was. A
    shape that survives simplification as coincident corners is not empty, draws
    no mark, and used to be published as a feature and counted as drawn;
    `_draws_as_something` is the test that catches it, and such a row is
    disclosed like any other that did not survive.

Either the whole picture fits, or there is no picture
    The vertex cap is a promise, not a preference. When a layer holds more
    detail than one page can draw, the tolerance is coarsened and the cost is
    stated. When it holds more *shapes* than one page can draw, no tolerance can
    help — simplification will not take a ring below four points, so a file of
    20,000 areas needs 100,000 corner points at every tolerance there is — and
    the loop that only knew how to double a distance ran out its rounds, arrived
    at a tolerance of 524 km, published that as this drawing's accuracy, and
    drew a map a third over the cap. Now `_irreducible_vertices` measures the
    floor, and a layer under it is refused outright: `Preview.undrawable_reason`
    is set, nothing is drawn, and the page says why. Drawing the subset that fits
    would be worse, because a missing district is not visible as a missing
    district — the operator would compare an outline this file does not have
    against the installed layers and find that it agreed.

Every measurement has to survive the file it is given, and finish
    A corner that is NaN or infinite is legal GeoJSON, passes validation, and
    raises out of GEOS the moment anything indexes it; `_finite_only` removes
    such shapes once, on the arrays every measurement reads, and names the rows.
    And the displacement measurement is indexed (`_distances_to_linework`) and
    taken once rather than twice: unindexed and doubled, a 100,000-point
    coastline — an ordinary full-resolution county for this tool's audience —
    spent 47 seconds in `build_preview` with no progress and no bound, which an
    operator reads as a hung page. It is now under a second, and it is the same
    exact number: `query_nearest` finds the true nearest piece of the linework,
    so the figure remains an upper bound on the error and M1's fail-closed
    guarantee is untouched.

Unknown is a value, and it is never spelled zero
    Every distance in this payload is either measured or reported as None with
    `Simplification.displacement_unknown_reason` saying why, and a note on the
    page saying it in words. This rule is written down because breaking it is
    not a visible failure: a displacement that could not be computed, quietly
    defaulted to 0.0, renders as "every boundary here may sit up to 0.0 m from
    where it really is" — a claim of *perfect* fidelity, manufactured, in the
    one sentence the operator has nothing else to check against. So the
    measurement fails closed at every step: `_worst_displacement` returns None
    rather than skipping a distance it could not take, `_worst_of` lets one
    unknown outrank every number beside it, and `reveals_promised_offset` is
    False whenever there is no measurement to promise on.

Distances between layers are geodesic; distances within one are projected
    `_separation_metres` — the number behind "that is the wrong file" — is
    solved on the WGS84 ellipsoid with `pyproj.Geod`, so it is true anywhere
    including across the antimeridian, where measuring on a projection centred
    on the viewport reported a 22 km gap as 2,489 km and accused a correct file.
    The displacement figure still uses a local azimuthal equidistant projection,
    which is right and cheap at the extent this tool is for; `_metric_crs_for`
    refuses to supply one once the extent has outrun it, and the displacement is
    then reported as unknown rather than measured on a stretched ruler.

The candidate frame is never modified
    F8-T6 commits the *original* geometry. Display simplification leaking into
    what gets installed would mean this service permanently serves a smoothed
    copy of somebody's boundaries. So `build_preview` reprojects, simplifies and
    rounds copies throughout, and `tests/test_admin_preview.py` asserts the
    caller's frame is byte-identical afterwards.

Pure, so that the fidelity claims can be tested
    `build_preview` takes frames and returns a payload; it opens nothing.
    `load_installed_layers` does the reading. That split is what lets a test
    shift a real layer by a hundred metres and prove the preview still shows the
    two apart.

Where the tolerance comes from
    One screen pixel at the rendered size, clamped by a ceiling stated in metres
    (`FIDELITY_CEILING_METRES`). The pixel rule alone is not safe: Cook County is
    about 60 km across, so one pixel of a 900-pixel view is roughly 67 metres of
    ground, and a boundary moved by a whole city block would vanish into the
    rounding. The ceiling is what actually governs at county scale, and it is
    derived from the thing that matters — `REVEALS_OFFSET_METRES`, the smallest
    boundary shift this preview undertakes to keep visible, with an order of
    magnitude of headroom underneath it. Measured on the two layers this service
    ships (see the module's tests): 0.54 m worst displacement on the police
    districts, 0.67 m on the municipalities, against a 10 m disclosure floor.
    A copy of the police districts shifted 10 m north is drawn 10.1 m from the
    original — fifteen times the error drawing introduced, which is the claim
    this module is built to be able to make.

ArcGIS / ArcPy equivalent
    This is the open-source stand-in for dragging a candidate feature class into
    an ArcGIS Pro map on top of the published layers and looking at it — the
    check every analyst makes before publishing and which no tool automates.
    Its parts: `arcpy.management.Project` (reprojection to a common frame),
    `arcpy.cartography.SimplifyPolygon` with the POINT_REMOVE algorithm and a
    stated tolerance (`shapely.simplify(preserve_topology=True)` here), the
    display scale that governs Pro's own drawing generalization, and
    `arcpy.analysis.Near` / `arcpy.management.GetCount` for the separation
    figure. The difference is that ArcGIS generalizes for drawing speed and
    never tells you by how much; this module measures it and puts the number on
    the page.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import geopandas as gpd
import numpy as np
import pyproj
import shapely
import shapely.ops

from app.admin.codes import _json_safe
from app.config import AppConfig, LayerConfig

# The row positions this module labels its features with have to be the same
# ones PIP-L008 puts in `detail["broken_positions"]`, or the page highlights the
# wrong areas. There is one definition of "which rows have something drawn in
# them, and where does each sit in the operator's file", and it lives in the
# validator; importing it is what keeps the two from drifting apart.
from app.admin.validate import _declared_crs, _drawn_shapes

WGS84 = "EPSG:4326"

# The frame the payload is drawn in, and the one the page's <svg> maps onto.
# 900x700 is a browser window on a laptop with the findings list beside it.
DEFAULT_SIZE = (900, 700)

# The smallest boundary shift this preview undertakes to keep visible. Ten
# metres is already finer than any real change to a district, a ward or a
# municipal limit — a redistricting moves boundaries by streets, not by
# doorsteps — and it is about the digitizing precision of the source data, so
# below it there is nothing trustworthy left to see anyway.
REVEALS_OFFSET_METRES = 10.0

# The most the drawn boundary may sit from the real one, on the ground. A full
# order of magnitude under `REVEALS_OFFSET_METRES`, which is the whole argument:
# an offset this preview claims to disclose must stand a clear decimal place
# above the error the preview itself introduces, or the operator is being asked
# to tell signal from artefact by eye.
#
# Half a metre rather than one, because the measured displacement runs somewhat
# over the tolerance asked for — topology-preserving simplification does not
# promise to stay inside it, and rounding adds a little afterwards. Measured on
# the shipped layers: 0.54 m worst on the police districts and 0.67 m on the
# municipalities at this setting, against a 10 m disclosure floor, so the
# decimal place holds with room over. At 1.0 m it did not: 1.28 m of error
# against a 10 m floor is a factor of eight, and eight is not ten.
FIDELITY_CEILING_METRES = 0.5

# Coordinates are emitted rounded to this many decimal places of a degree.
# 1e-6 degrees is about 0.11 m of latitude — a fifth of the fidelity ceiling, so
# rounding is a rounding error rather than a second simplification, and it takes
# roughly a third off the payload against unrounded floats. The rounding happens
# before the displacement is measured, so its contribution is inside the number
# the page states.
COORDINATE_DECIMALS = 6

# The most corner points the payload will carry across every layer in it. An
# SVG of this size draws in well under a second on a modest laptop, and the
# JSON is roughly 24 bytes a point — about 1.4 MB, which is a local page on
# localhost rather than something crossing a network. Measured on the shipped
# layers at the fidelity ceiling: police districts 7,751 points, municipalities
# 45,198, ward-25 precincts 215. Installing a ward or precinct file against both
# shipped layers comes to about 53,000 and never escalates; installing a fresh
# copy of the municipalities layer itself does escalate, once, and says so.
MAX_TOTAL_VERTICES = 60_000

# When the cap is exceeded the tolerance is doubled and everything is drawn
# again. Each doubling takes a large bite out of the count, so this converges in
# a handful of rounds; the bound exists so that a pathological layer cannot spin
# here while an operator waits. Every escalation is reported.
#
# Doubling a *distance* cannot answer a *feature-count* problem, and that is not
# a corner case: `preserve_topology=True` will not take a ring below four points,
# so a layer of N areas can never be drawn in fewer than about 5N corner points
# no matter how coarse the tolerance. `_irreducible_vertices` measures that floor
# before the loop starts, so the loop is only ever entered with a lever that can
# reach the cap. See `UNDRAWABLE_TOO_DETAILED` for what happens when it cannot.
MAX_ESCALATIONS = 20

# Below this many segments, measuring a feature's displacement point-by-point
# against the whole of its own drawn linework is quicker than building a spatial
# index over that linework first. Above it the unindexed measurement is
# quadratic: a single 100,000-point coastline took 47 s and looked hung, and the
# vertex cap does not bound it, because the cap limits what is *drawn* and the
# measurement runs against the originals, which are never capped. Both paths are
# exact and return the same number; the index only changes how long it takes to
# find it. 64 is where the two cross on a modest laptop, and the answer is
# insensitive to it — anywhere from 16 to 256 gives the same timings to a
# fraction of a second.
INDEXED_MEASUREMENT_MIN_SEGMENTS = 64

# Metres per degree of latitude on the WGS84 ellipsoid, near enough anywhere
# (it runs 110,574 at the equator to 111,694 at the poles). Used only to turn a
# tolerance in metres into one in degrees and to size the viewport; every
# distance this module *reports* is measured on a real equal-distance
# projection, never with this constant.
METRES_PER_DEGREE_LATITUDE = 111_320.0

# How much room to leave around the shapes, as a fraction of the span, so
# nothing is drawn hard against the edge of the frame.
VIEWPORT_MARGIN = 0.04

# The ruler every distance *between* layers is measured with. `Geod` solves the
# inverse geodesic problem on the WGS84 ellipsoid, so it is true anywhere on the
# globe and has no centre to be far from — unlike the azimuthal equidistant
# projection below, which is true only radially from its own centre. pyproj is
# not a new dependency: geopandas already requires it.
GEODESIC = pyproj.Geod(ellps="WGS84")

# Mean Earth radius, used only to turn a distance from the projection's centre
# into an angle so the projection's distortion can be bounded. Never used to
# report a distance.
EARTH_RADIUS_METRES = 6_371_008.8

# How far the local metric projection may stretch before this module stops
# publishing metres measured on it.
#
# An azimuthal equidistant projection is exact along every line *from its
# centre* and stretches everything crosswise by theta/sin(theta), where theta is
# the angle subtended at the Earth's centre by the distance from the projection
# centre. That factor is 1.000 at the centre, 1.01 at about 1,550 km, and runs
# away entirely toward the antipode — which is how a viewport straddling the
# antimeridian (centre computed at longitude 0, i.e. halfway round the world
# from what it contains) turned 0.51 m of real ground into a reported 29.77 m.
#
# 1% is chosen against what the number is for: the fidelity headline is stated
# to a tenth of a metre and is compared against a ceiling of half a metre, so 1%
# of it is a few millimetres — below the precision printed. Past this the extent
# is reported as unmeasurable, with a note, rather than published quietly wrong.
METRIC_PROJECTION_MAX_DISTORTION = 1.01

# A viewport can never be narrower than this, in the units it is drawn in. It is
# what stops a single degenerate area — one polygon collapsed to nearly a point
# — from producing a zero-width frame that no renderer can scale.
MIN_VIEWPORT_SPAN_DEGREES = 1.0e-5

# Why the candidate is being drawn on its own rather than over the installed
# layers.
UNCOMPARABLE_NO_CRS = "no_crs"
UNCOMPARABLE_UNPROJECTABLE = "unprojectable_crs"

ROLE_CANDIDATE = "candidate"
ROLE_INSTALLED = "installed"

# Why nothing was drawn at all. The vertex cap is a promise the payload keeps:
# either every layer in it is drawn within the cap, or nothing is drawn and this
# says so. Publishing a picture that broke the cap by a third — which is what
# twenty futile doublings used to produce — is neither.
UNDRAWABLE_TOO_DETAILED = "too_detailed_to_draw"

# Why the displacement could not be measured. A `None` displacement in this
# payload always carries one of these, because "unknown" that does not say why
# is read as "fine".
DISPLACEMENT_UNKNOWN_NO_CRS = "no_crs"
DISPLACEMENT_UNKNOWN_EXTENT_TOO_LARGE = "extent_too_large"
DISPLACEMENT_UNKNOWN_GEOMETRY = "unmeasurable_geometry"
# Nothing was drawn, so there is no drawing to measure the originals against.
# Distinct from `unmeasurable_geometry`, which blames shapes that were fine.
DISPLACEMENT_UNKNOWN_NOT_DRAWN = "not_drawn"


# --------------------------------------------------------------------------
# what goes in, and what comes out
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DrawableLayer:
    """A layer already serving on this instance, loaded and ready to draw.

    This is the drawable twin of `validate.InstalledLayer`, which carries only a
    bounding box because that is all PIP-L016 needs. The preview needs the
    outlines themselves — a bounding box is exactly the thing that cannot show
    an operator that two boundaries no longer line up.
    """

    id: str
    name: str
    frame: gpd.GeoDataFrame


@dataclass(frozen=True)
class DrawnLayer:
    """One layer as it will be drawn: GeoJSON, plus what was left out of it."""

    id: str
    name: str
    role: str
    geojson: dict[str, Any]
    feature_count: int
    vertex_count: int
    # Rows of the source that are not in `geojson`, as positions in the original
    # file counting from zero — the same counting PIP-L008's `broken_positions`
    # uses. Empty rows are not listed (they were never drawable); this is only
    # for shapes that had something in them and did not survive being drawn.
    dropped_positions: tuple[int, ...] = ()
    separation_metres: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "geojson": self.geojson,
            "feature_count": self.feature_count,
            "vertex_count": self.vertex_count,
            "dropped_positions": list(self.dropped_positions),
            "separation_metres": self.separation_metres,
        }


@dataclass(frozen=True)
class Viewport:
    """The rectangle the page draws, and the pixels it draws into.

    `min_x` … `max_y` are in the units of `units`: degrees of longitude and
    latitude for anything placed on Earth, and the file's own unnamed numbers
    for a candidate that could not be placed.

    `longitude_scale` is cos(latitude) at the middle of the rectangle. A degree
    of longitude is shorter than a degree of latitude everywhere but the
    equator — 0.74 of it in Cook County — so a renderer that maps degrees
    straight onto pixels draws the county a third too wide. Multiply x by this
    before scaling and the shapes come out the shape they are. The rectangle has
    already been widened or heightened so that it matches the pixel box's aspect
    ratio *after* that multiplication.
    """

    min_x: float
    min_y: float
    max_x: float
    max_y: float
    width_px: int
    height_px: int
    longitude_scale: float = 1.0
    units: str = "degrees"

    @property
    def span_x(self) -> float:
        return self.max_x - self.min_x

    @property
    def span_y(self) -> float:
        return self.max_y - self.min_y

    @property
    def units_per_pixel(self) -> float:
        """One screen pixel, in the units the viewport is drawn in."""
        return self.span_y / self.height_px

    def metres_per_pixel(self) -> float | None:
        """One screen pixel, in metres of ground, or None if unknowable."""
        if self.units != "degrees":
            return None
        return self.units_per_pixel * METRES_PER_DEGREE_LATITUDE

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_x": self.min_x,
            "min_y": self.min_y,
            "max_x": self.max_x,
            "max_y": self.max_y,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "longitude_scale": self.longitude_scale,
            "units": self.units,
        }


@dataclass(frozen=True)
class Simplification:
    """What drawing cost, in corner points and in metres of ground.

    `max_displacement_metres` is the headline: the furthest any point of any
    original boundary — candidate or installed — ends up from the boundary
    actually drawn, after both simplification and coordinate rounding. It is
    measured, not derived from the tolerance, because topology-preserving
    simplification does not promise to stay inside its tolerance and rounding
    adds to it afterwards.

    None means it could not be measured, and `displacement_unknown_reason` says
    which of three things happened: the candidate has no coordinate reference
    system so there is no ground to measure on (`max_displacement_units` still
    carries the figure in the file's own numbers); the area covered is too wide
    for any single local projection to measure honestly; or the geometry itself
    offered nothing to measure against.

    None is never a stand-in for zero. A distance that could not be computed is
    reported as unknown here and `reveals_promised_offset` goes False, because a
    fabricated zero in this field would tell the operator the drawing is perfect
    in exactly the place the system has no other safeguard.

    `max_displacement_units` is the same measurement in the coordinates' own
    units, and it is only taken when there is no figure in metres to be had — it
    exists for the layer that names no coordinate reference system, where the
    file's own numbers are the only ruler there is. When
    `max_displacement_metres` carries a figure this is None, because measuring
    the identical thing twice on two rulers doubled the cost of the whole preview
    (a 100,000-point layer spent 47 seconds here, half of it on the copy nobody
    reads) to restate a number the payload already has. None here is therefore
    "not needed", not "unknown"; `displacement_unknown_reason` is the one place
    unknown is declared, and it is set whenever *either* figure is missing for a
    reason.

    `vertex_floor` is the fewest corner points this drawing could possibly take —
    what the layers come to when simplified past the point of no return. It is
    what the escalation loop is checked against, because a tolerance can only
    remove points *within* a shape and can never remove a shape. None when the
    first drawing already fitted under the cap and nothing forced the question.
    """

    tolerance: float
    tolerance_metres: float | None
    coordinate_units: str
    vertices_before: int
    vertices_after: int
    max_displacement_metres: float | None
    max_displacement_units: float | None
    displacement_unknown_reason: str | None = None
    escalated: bool = False
    escalation_rounds: int = 0
    vertex_cap: int = MAX_TOTAL_VERTICES
    vertex_floor: int | None = None
    fidelity_ceiling_metres: float = FIDELITY_CEILING_METRES
    reveals_offset_metres: float = REVEALS_OFFSET_METRES

    @property
    def reveals_promised_offset(self) -> bool:
        """May the page still promise that a `REVEALS_OFFSET_METRES` shift shows?

        The promise is a full order of magnitude: the error drawing introduced
        has to sit at or under a tenth of the smallest offset the preview claims
        to disclose, so that an operator looking at two boundaries a disclosure
        floor apart is looking at a real gap and not at an artefact of drawing.
        False whenever the vertex cap forced a coarser tolerance — and False,
        not unknown, when the displacement could not be measured in metres at
        all, because an unmeasured claim is not a claim worth making.
        """
        if self.max_displacement_metres is None:
            return False
        return self.max_displacement_metres * 10.0 <= self.reveals_offset_metres

    def to_dict(self) -> dict[str, Any]:
        return {
            "tolerance": self.tolerance,
            "tolerance_metres": self.tolerance_metres,
            "coordinate_units": self.coordinate_units,
            "vertices_before": self.vertices_before,
            "vertices_after": self.vertices_after,
            "max_displacement_metres": self.max_displacement_metres,
            "max_displacement_units": self.max_displacement_units,
            "displacement_unknown_reason": self.displacement_unknown_reason,
            "escalated": self.escalated,
            "escalation_rounds": self.escalation_rounds,
            "vertex_cap": self.vertex_cap,
            "vertex_floor": self.vertex_floor,
            "fidelity_ceiling_metres": self.fidelity_ceiling_metres,
            "reveals_offset_metres": self.reveals_offset_metres,
            "reveals_promised_offset": self.reveals_promised_offset,
        }


@dataclass(frozen=True)
class Preview:
    """Everything F8-T5's page needs to draw the candidate and say what it did.

    Two viewports, deliberately. `viewport` contains the candidate *and* every
    installed layer, because a viewport fitted to the candidate alone would draw
    the neighbouring county — or a layer mislabelled into Missouri — as a
    perfectly ordinary map filling the frame, which is the failure this whole
    feature exists to prevent. `candidate_viewport` fits the candidate alone,
    for the page to offer as a second view: when the two are far apart the
    combined frame makes both of them specks, and the operator needs to be able
    to look closely as well as broadly. The choice belongs to the page, so both
    are reported and neither is taken silently.
    """

    candidate: DrawnLayer
    installed: tuple[DrawnLayer, ...]
    viewport: Viewport | None
    candidate_viewport: Viewport | None
    installed_viewport: Viewport | None
    simplification: Simplification
    comparable: bool
    uncomparable_reason: str | None = None
    # Set when this preview draws nothing at all, and why. See
    # `UNDRAWABLE_TOO_DETAILED`: a partial map is not a smaller version of the
    # truth, it is a picture of a different layer, and the one check this whole
    # feature exists for is the operator comparing shapes.
    undrawable_reason: str | None = None
    separation_metres: float | None = None
    overlaps_installed: bool | None = None
    highlight: tuple[int, ...] = ()
    highlight_not_drawn: tuple[int, ...] = ()
    notes: tuple[str, ...] = ()
    candidate_crs: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The whole payload, as something `json.dumps` accepts unaided."""
        return _json_safe(
            {
                "candidate": self.candidate.to_dict(),
                "installed": [layer.to_dict() for layer in self.installed],
                "viewport": self.viewport.to_dict() if self.viewport else None,
                "candidate_viewport": (
                    self.candidate_viewport.to_dict()
                    if self.candidate_viewport
                    else None
                ),
                "installed_viewport": (
                    self.installed_viewport.to_dict()
                    if self.installed_viewport
                    else None
                ),
                "simplification": self.simplification.to_dict(),
                "comparable": self.comparable,
                "uncomparable_reason": self.uncomparable_reason,
                "undrawable_reason": self.undrawable_reason,
                "separation_metres": self.separation_metres,
                "overlaps_installed": self.overlaps_installed,
                "highlight": list(self.highlight),
                "highlight_not_drawn": list(self.highlight_not_drawn),
                "notes": list(self.notes),
                "candidate_crs": self.candidate_crs,
            }
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


# --------------------------------------------------------------------------
# reading what is already installed
# --------------------------------------------------------------------------


def load_installed_layers(
    config: AppConfig, *, exclude: Sequence[str] = ()
) -> tuple[DrawableLayer, ...]:
    """Every layer this instance already serves, loaded so it can be drawn.

    The only I/O in this module, and the reason `build_preview` has none: a test
    can hand `build_preview` a frame it built itself, or one this function read,
    and get the same answer.

    `exclude` drops layer ids from the result — F8-T4 passes the id being
    replaced, so that reinstalling `police_districts` compares the candidate
    against the *other* layers rather than against the copy it is about to
    supersede.

    A layer that cannot be read is not caught here. `app.lookup._LoadedLayer`
    reads the same file at startup and raises on the same failure, so an
    installed layer this function cannot open is a service that is not running —
    a condition to surface, not one to draw around.

    ArcGIS / ArcPy equivalent
        Opening the published .aprx and reading its layer list —
        `arcpy.mp.ArcGISProject(...).listMaps()[0].listLayers()` — to see what
        the map already shows before adding anything to it.
    """
    skip = set(exclude)
    return tuple(
        DrawableLayer(
            id=layer_config.id,
            name=layer_config.name,
            frame=_read_layer(layer_config),
        )
        for layer_id, layer_config in config.layers.items()
        if layer_id not in skip
    )


def _read_layer(layer_config: LayerConfig) -> gpd.GeoDataFrame:
    """One configured layer's GeoPackage table, rows in file order.

    `reset_index(drop=True)` for the same reason `app.lookup._LoadedLayer` does
    it: positions in the frame are what everything downstream counts with, and
    they have to be 0, 1, 2 … regardless of what the reader put in the index.
    """
    return gpd.read_file(layer_config.path, layer=layer_config.layer).reset_index(
        drop=True
    )


# --------------------------------------------------------------------------
# the payload
# --------------------------------------------------------------------------


def build_preview(
    frame: gpd.GeoDataFrame,
    *,
    layer_id: str = "candidate",
    display_name: str | None = None,
    installed: Sequence[DrawableLayer] = (),
    highlight: Iterable[int] = (),
    size: tuple[int, int] = DEFAULT_SIZE,
    max_total_vertices: int = MAX_TOTAL_VERTICES,
) -> Preview:
    """Draw `frame` over the installed layers, and say what drawing cost.

    Pure. `frame` is read and never written: everything below works on copies,
    and `tests/test_admin_preview.py::test_candidate_frame_is_not_mutated` holds
    that line, because F8-T6 commits this same object.

    `highlight` are row positions to mark — PIP-L008's `broken_positions`, in
    practice. Every emitted feature carries its own row position as its GeoJSON
    `id` and as `properties["row"]`, so the page can mark exactly those rows
    however much detail was dropped between here and there.

    Three shapes of answer:

    * ordinary — the candidate reprojected to WGS84 and drawn over the installed
      layers, with `separation_metres` saying how far apart they sit;
    * no coordinate reference system — the candidate cannot be placed on Earth
      at all, so it is drawn alone in its own numbers and `comparable` is False.
      Drawing it over the installed layers would mean inventing a position for
      it, and an invented position that happens to look plausible is precisely
      the lie this preview exists to prevent;
    * a coordinate reference system that contradicts the numbers (PIP-L004) —
      drawn exactly where its own numbers put it, which for a Chicago file
      labelled EPSG:3435 is southern Missouri. Being visibly in Missouri *is*
      the diagnosis, so nothing here corrects it; the viewport takes in both and
      `separation_metres` gives the page the number to say it with.
    """
    width_px, height_px = int(size[0]), int(size[1])
    notes: list[str] = []
    wanted_highlight = tuple(sorted({int(position) for position in highlight}))
    name = display_name or layer_id

    candidate_shapes, candidate_rows = _drawn_rows(frame)
    crs = _declared_crs(frame)

    # ---- can this be placed on Earth at all? -----------------------------
    comparable = True
    uncomparable_reason: str | None = None
    candidate_wgs84: np.ndarray | None = None

    if crs is None:
        comparable = False
        uncomparable_reason = UNCOMPARABLE_NO_CRS
        notes.append(
            "This layer carries no record of where on Earth its shapes sit, so "
            "it cannot be drawn over the layers already installed — placing it "
            "anywhere would be inventing a position for it. What you see is its "
            "own outline, on its own, in the file's own numbers. Its shape is "
            "worth checking; its position here means nothing."
        )
    else:
        try:
            candidate_wgs84 = _to_wgs84(candidate_shapes, crs)
        except Exception:  # pragma: no cover - pyproj refuses the declaration
            comparable = False
            uncomparable_reason = UNCOMPARABLE_UNPROJECTABLE
            notes.append(
                "This layer names a coordinate reference system this tool "
                "cannot convert from, so it cannot be drawn over the layers "
                "already installed. What you see is its own outline, on its "
                "own, in the file's own numbers."
            )

    if comparable:
        drawable_installed = tuple(installed)
    else:
        # Deliberately dropped rather than drawn beside: an installed layer on
        # the same page as an unplaceable candidate would be read as a
        # comparison, and there is no comparison to be had.
        drawable_installed = ()
        if installed:
            notes.append(
                "The layers already installed are not drawn here, because there "
                "is nothing to compare them against."
            )

    units = "degrees" if comparable else "layer units"
    candidate_source = (
        candidate_wgs84 if comparable else _geometry_values(candidate_shapes)
    )

    # A coordinate that is not a number is not a place. Removed here, once, and
    # named — every measurement below this line assumes finite coordinates, and
    # one that does not get them raises out of GEOS with a stack trace instead of
    # telling the operator anything.
    candidate_source, unplaceable_indices = _finite_only(candidate_source)
    unplaceable_positions = tuple(
        int(candidate_rows[index]) for index in unplaceable_indices
    )
    if unplaceable_positions:
        notes.append(
            f"{len(unplaceable_positions)} shape"
            f"{'s' if len(unplaceable_positions) != 1 else ''} in this file "
            f"{'have' if len(unplaceable_positions) != 1 else 'has'} corners "
            f"that are not numbers, so "
            f"{'they cannot' if len(unplaceable_positions) != 1 else 'it cannot'}"
            f" be placed on the map and "
            f"{'are' if len(unplaceable_positions) != 1 else 'is'} not drawn "
            f"here. The rest of the file is drawn as usual; what is missing is "
            f"listed row by row."
        )

    installed_sources: list[tuple[DrawableLayer, np.ndarray, list[int]]] = []
    for layer in drawable_installed:
        shapes, rows = _drawn_rows(layer.frame)
        layer_crs = _declared_crs(layer.frame)
        if layer_crs is None or len(rows) == 0:
            # An installed layer with no CRS cannot be running (app.lookup
            # refuses it at startup), and an empty one has nothing to draw.
            # Neither is silent: a layer missing from the comparison is exactly
            # the kind of absence an operator would otherwise read as agreement.
            notes.append(
                f"The installed layer {layer.name!r} is not on this map — it "
                f"has nothing drawn in it that could be placed on Earth."
            )
            continue
        geometries, unplaceable = _finite_only(_to_wgs84(shapes, layer_crs))
        if unplaceable:
            notes.append(
                f"{len(unplaceable)} shape"
                f"{'s' if len(unplaceable) != 1 else ''} in the installed layer "
                f"{layer.name!r} {'have' if len(unplaceable) != 1 else 'has'} "
                f"corners that are not numbers and "
                f"{'are' if len(unplaceable) != 1 else 'is'} not drawn here."
            )
        installed_sources.append((layer, geometries, rows))

    # ---- the rectangle everything is drawn in ----------------------------
    candidate_viewport = _viewport_from(
        [candidate_source], width_px, height_px, units=units
    )
    installed_viewport = _viewport_from(
        [geometries for _, geometries, _ in installed_sources],
        width_px,
        height_px,
        units=units,
    )
    viewport = _viewport_from(
        [candidate_source] + [geometries for _, geometries, _ in installed_sources],
        width_px,
        height_px,
        units=units,
    )

    # ---- how much detail may be thrown away, and what it cost -------------
    tolerance, tolerance_metres = _tolerance_for(viewport)
    metric_crs = _metric_crs_for(viewport) if comparable else None

    everything: list[np.ndarray] = [candidate_source] + [
        geometries for _, geometries, _ in installed_sources
    ]
    vertices_before = sum(
        int(shapely.get_num_coordinates(group).sum()) for group in everything
    )

    # The loop below has exactly one lever — a distance — and a distance cannot
    # remove a shape. `preserve_topology=True` will not take a ring below four
    # points, so a file of 20,000 areas is stuck at about 100,000 corner points
    # however coarse the smoothing gets, and doubling the tolerance twenty times
    # only arrives at a meaningless 524 km while still exceeding the cap by a
    # third. So the first time the cap is missed, the floor is measured: the
    # fewest corner points this drawing could take at any tolerance at all. If
    # the floor is over the cap the lever cannot reach and nothing is drawn.
    # Computed only when something forces the question, because it costs a
    # simplification pass and the ordinary preview never needs it.
    vertex_floor: int | None = None
    escalation_rounds = 0
    undrawable_reason: str | None = None
    while True:
        drawn_groups = [_simplify_and_round(group, tolerance) for group in everything]
        vertices_after = sum(
            int(shapely.get_num_coordinates(group).sum()) for group in drawn_groups
        )
        if vertices_after <= max_total_vertices:
            break
        if vertex_floor is None:
            vertex_floor = _irreducible_vertices(everything, viewport)
        if vertex_floor > max_total_vertices or escalation_rounds >= MAX_ESCALATIONS:
            undrawable_reason = UNDRAWABLE_TOO_DETAILED
            drawn_groups = [_nothing_drawn(group) for group in everything]
            vertices_after = 0
            break
        tolerance *= 2.0
        tolerance_metres = tolerance * METRES_PER_DEGREE_LATITUDE if comparable else None
        escalation_rounds += 1

    if undrawable_reason == UNDRAWABLE_TOO_DETAILED:
        notes.append(
            f"This layer is made of too many separate shapes for this tool to "
            f"draw, so nothing is drawn here at all. Every shape needs a few "
            f"corner points however far it is smoothed, and this file needs at "
            f"least {vertex_floor or 0:,} of them against a budget of "
            f"{max_total_vertices:,}. Only some of the shapes could have been "
            f"drawn, and a map with shapes missing from it — with no way for you "
            f"to tell which — is worse than no map at all: you would be "
            f"comparing an outline this file does not have against the layers "
            f"already installed, and finding that it agreed. Check this file "
            f"another way, or install a less detailed version of it."
        )
    elif escalation_rounds:
        notes.append(
            f"This layer holds more detail than one page can draw, so the "
            f"outlines were smoothed further than usual to fit — "
            f"{vertices_before:,} corner points came down to "
            f"{vertices_after:,}. {{displacement}}"
        )

    # Measured once, on the ruler the payload actually reports in. The figure in
    # the coordinates' own units is only taken when there is no ground
    # measurement to be had (see `Simplification`): taking both meant measuring
    # the identical thing twice, which was half the cost of the whole preview.
    displacement_metres: float | None = None
    displacement_units: float | None = None
    if metric_crs is not None:
        displacement_metres = _worst_of(
            _worst_displacement_metres(before, after, metric_crs)
            for before, after in zip(everything, drawn_groups)
        )
    if displacement_metres is None:
        displacement_units = _worst_of(
            _worst_displacement(before, after)
            for before, after in zip(everything, drawn_groups)
        )

    # Rows that had something drawable in them when they came in, against what
    # is on the map now. Counted on the rows rather than on `vertices_before`,
    # because a row discarded as unplaceable contributes no vertices either and
    # this is exactly the case that must not come out as "drawn perfectly".
    rows_to_draw = len(candidate_rows) + sum(
        len(rows) for _layer, _geometries, rows in installed_sources
    )
    nothing_was_drawn = rows_to_draw > 0 and vertices_after == 0
    if nothing_was_drawn:
        # Nothing is on the map, so there is nothing the originals can be
        # compared against. Zero here would read as a perfect drawing — of a
        # blank page.
        displacement_metres = None
        displacement_units = None

    # Why there is no number, if there is no number. An unknown that does not
    # say why reads as "fine", and this is the field the operator's confidence
    # in the whole picture rests on.
    unknown_reason: str | None = None
    if nothing_was_drawn:
        unknown_reason = DISPLACEMENT_UNKNOWN_NOT_DRAWN
    elif not comparable:
        unknown_reason = (
            DISPLACEMENT_UNKNOWN_GEOMETRY
            if displacement_units is None
            else DISPLACEMENT_UNKNOWN_NO_CRS
        )
    elif displacement_metres is None:
        unknown_reason = (
            DISPLACEMENT_UNKNOWN_EXTENT_TOO_LARGE
            if metric_crs is None and viewport is not None
            else DISPLACEMENT_UNKNOWN_GEOMETRY
        )

    if unknown_reason == DISPLACEMENT_UNKNOWN_NOT_DRAWN:
        notes.append(
            "Nothing from this file is drawn on the map, so this tool cannot "
            "say how faithfully it was drawn. It is not saying the file is "
            "good; it is saying you have not seen it."
        )
    elif unknown_reason == DISPLACEMENT_UNKNOWN_EXTENT_TOO_LARGE:
        notes.append(
            "The ground this map covers is too wide for this tool to measure "
            "distances across accurately, so it cannot say how far the outlines "
            "drawn here sit from the real ones. Treat the picture as a rough "
            "one. A candidate spread this far across the world is itself worth "
            "a second look — a layer of one county does not span continents."
        )
    elif unknown_reason == DISPLACEMENT_UNKNOWN_GEOMETRY:
        notes.append(
            "This tool could not measure how far the outlines drawn here sit "
            "from the real ones — some of the shapes in this file are not of a "
            "kind it can measure. It is not saying the drawing is accurate; it "
            "is saying it does not know."
        )

    notes = [
        note.replace("{displacement}", _displacement_phrase(
            displacement_metres, displacement_units, comparable
        ))
        for note in notes
    ]

    simplification = Simplification(
        tolerance=tolerance,
        tolerance_metres=tolerance_metres,
        coordinate_units=units,
        vertices_before=vertices_before,
        vertices_after=vertices_after,
        max_displacement_metres=displacement_metres,
        max_displacement_units=displacement_units,
        displacement_unknown_reason=unknown_reason,
        escalated=escalation_rounds > 0,
        escalation_rounds=escalation_rounds,
        vertex_cap=max_total_vertices,
        vertex_floor=vertex_floor,
    )

    # ---- the features themselves -----------------------------------------
    candidate_drawn, candidate_dropped = _drawn_layer(
        drawn_groups[0],
        candidate_rows,
        layer_id=layer_id,
        name=name,
        role=ROLE_CANDIDATE,
        highlight=set(wanted_highlight),
    )
    installed_drawn: list[DrawnLayer] = []
    for index, (layer, _, rows) in enumerate(installed_sources, start=1):
        drawn, _dropped = _drawn_layer(
            drawn_groups[index],
            rows,
            layer_id=layer.id,
            name=layer.name,
            role=ROLE_INSTALLED,
            highlight=set(),
        )
        installed_drawn.append(drawn)

    # Rows that fell out for want of size, as opposed to the ones already
    # accounted for above: a shape with a corner that is not a number is not a
    # small shape, and telling the operator it was would send them looking for
    # something that is not there.
    too_small = tuple(
        position
        for position in candidate_dropped
        if position not in set(unplaceable_positions)
    )
    if too_small and undrawable_reason is None:
        notes.append(
            f"{len(too_small)} area"
            f"{'s' if len(too_small) != 1 else ''} in this file "
            f"{'are' if len(too_small) != 1 else 'is'} too small to draw "
            f"at this size and {'are' if len(too_small) != 1 else 'is'} "
            f"not on the map. They are still in the file and would still be "
            f"installed."
        )

    # Not drawn covers both senses: a row the file never had anything drawable
    # in, and a row that had something and did not survive being drawn. The
    # payload claims a highlighted row is marked on the map, and it can only
    # claim that for rows that are on the map.
    drawn_positions = set(candidate_rows) - set(candidate_dropped)
    highlight_not_drawn = tuple(
        position for position in wanted_highlight if position not in drawn_positions
    )

    # ---- how far apart are they? -----------------------------------------
    separation: float | None = None
    overlaps: bool | None = None
    if comparable and len(candidate_rows):
        per_layer: list[float | None] = []
        for index, (layer, geometries, _rows) in enumerate(
            installed_sources, start=1
        ):
            distance = _separation_metres(candidate_source, geometries)
            per_layer.append(distance)
            installed_drawn[index - 1] = _with_separation(
                installed_drawn[index - 1], distance
            )
        measured = [distance for distance in per_layer if distance is not None]
        unmeasured = len(per_layer) - len(measured)
        if measured:
            separation = min(measured)
        if separation is not None and separation <= 0.0:
            # Touching one layer is enough to know; nothing unmeasured can
            # contradict it.
            overlaps = True
        elif measured and not unmeasured:
            overlaps = False
            notes.append(
                f"The nearest layer already installed is "
                f"{_distance_phrase(separation)} away from this one. If you "
                f"meant to install a layer covering the same ground, that "
                f"is the wrong file, or it is labelled with the wrong "
                f"coordinate system."
            )
        elif unmeasured:
            # Not "they are far apart" and not "they overlap": unknown. Saying
            # either would be an accusation, or a reassurance, this tool has not
            # earned — and both are acted on.
            overlaps = None
            notes.append(
                f"This tool could not work out how far this layer sits from "
                f"{unmeasured} of the layers already installed, so it is not "
                f"telling you whether they cover the same ground. Look at the "
                f"map."
            )

    return Preview(
        candidate=candidate_drawn,
        installed=tuple(installed_drawn),
        viewport=viewport,
        candidate_viewport=candidate_viewport,
        installed_viewport=installed_viewport,
        simplification=simplification,
        comparable=comparable,
        uncomparable_reason=uncomparable_reason,
        undrawable_reason=undrawable_reason,
        separation_metres=separation,
        overlaps_installed=overlaps,
        highlight=wanted_highlight,
        highlight_not_drawn=highlight_not_drawn,
        notes=tuple(notes),
        candidate_crs=_crs_text(crs),
    )


# --------------------------------------------------------------------------
# geometry, all of it on copies
# --------------------------------------------------------------------------


def _drawn_rows(frame: gpd.GeoDataFrame) -> tuple[gpd.GeoSeries | None, list[int]]:
    """The rows with something drawn in them, and each one's place in the file."""
    try:
        geometries = frame.geometry
    except (AttributeError, KeyError, ValueError):
        return None, []
    return _drawn_shapes(geometries)


def _geometry_values(shapes: gpd.GeoSeries | None) -> np.ndarray:
    """The shapes as a plain array of shapely objects, never a view to write to."""
    if shapes is None or len(shapes) == 0:
        return np.empty(0, dtype=object)
    return np.asarray(shapes.to_numpy(), dtype=object)


def _to_wgs84(shapes: gpd.GeoSeries | None, crs: Any) -> np.ndarray:
    """A reprojected *copy* of these shapes, in degrees of latitude and longitude.

    `GeoSeries.to_crs` builds new geometry objects and leaves the originals
    alone; the frame the caller handed in is never touched. That is not an
    incidental property — F8-T6 commits that frame.

    ArcGIS / ArcPy equivalent
        `arcpy.management.Project`, which likewise writes a new feature class
        rather than editing the input.
    """
    if shapes is None or len(shapes) == 0:
        return np.empty(0, dtype=object)
    series = gpd.GeoSeries(shapes.to_numpy(), crs=crs)
    return np.asarray(series.to_crs(WGS84).to_numpy(), dtype=object)


def _finite_only(geometries: np.ndarray) -> tuple[np.ndarray, tuple[int, ...]]:
    """These shapes with the unplaceable ones removed, and where they were.

    A coordinate that is NaN or infinite is not a place. Such a shape is
    perfectly legal GeoJSON, `read_candidate` accepts it (PIP-L017 warns and does
    not block), `validate_candidate` finds nothing to refuse, and every
    measurement below this line then raises out of GEOS —
    ``IllegalArgumentException: Non-finite envelope bounds passed to index
    insert`` — because a spatial index cannot hold a box with no numbers in it.
    An operator dragging in a file with one bad corner got a stack trace.

    The module already knew this input was possible: `_viewport_from` has always
    discarded non-finite bounding boxes. What it did not do was remove the shape
    itself, so the anticipated input was precisely the one that crashed. Removing
    it here, once, on the arrays every measurement reads, is the whole fix — and
    it is done *after* reprojection, because a projection can produce a non-finite
    coordinate from a perfectly finite one.

    Removed, not repaired. There is no honest guess at where a corner with no
    number was meant to be, and the position is returned so the caller can name
    the row rather than let it vanish.

    ArcGIS / ArcPy equivalent
        `arcpy.management.CheckGeometry` / `RepairGeometry`, which likewise
        report the offending OBJECTIDs rather than quietly moving vertices —
        except that Pro's repair will delete such a feature outright, where this
        keeps it in the file and only leaves it off the map.
    """
    if len(geometries) == 0:
        return geometries, ()
    kept = np.array(geometries, dtype=object, copy=True)
    unplaceable: list[int] = []
    for index, geometry in enumerate(kept):
        if geometry is None:
            continue
        coordinates = shapely.get_coordinates(geometry)
        if len(coordinates) and not bool(np.all(np.isfinite(coordinates))):
            kept[index] = None
            unplaceable.append(index)
    return kept, tuple(unplaceable)


def _nothing_drawn(geometries: np.ndarray) -> np.ndarray:
    """A group of the same shape with every feature left off the map."""
    return np.full(len(geometries), None, dtype=object)


def _irreducible_vertices(
    groups: Sequence[np.ndarray], viewport: Viewport | None
) -> int:
    """The fewest corner points these layers could be drawn with at any tolerance.

    Simplification's lever is a distance, and a distance has a floor it cannot go
    below: `preserve_topology=True` keeps a ring at four points and a line at two
    no matter how coarse the tolerance, because the alternative is deleting the
    shape, which simplification will not do. So a layer's vertex count is not a
    dial that reaches zero — it bottoms out at roughly five points per feature,
    and for 20,000 features that is 100,000 points, well over the cap, at every
    tolerance there is.

    Measured rather than assumed: the whole layer is simplified at a tolerance
    several times the width of the picture, which is as coarse as coarse gets,
    and the result counted. That answers the question the escalation loop was
    silently getting wrong — not "is this too detailed", which more smoothing can
    fix, but "is this too *many*", which it cannot.

    ArcGIS / ArcPy equivalent
        The MinSimpTol / MaxSimpTol columns `arcpy.cartography.SimplifyPolygon`
        writes out, which likewise record that the tolerance asked for could not
        be honoured for a given feature.
    """
    if viewport is None:
        return 0
    coarsest = max(viewport.span_x, viewport.span_y) * 4.0
    if not math.isfinite(coarsest) or coarsest <= 0.0:
        return sum(int(shapely.get_num_coordinates(group).sum()) for group in groups)
    return sum(
        int(
            shapely.get_num_coordinates(
                shapely.simplify(group, coarsest, preserve_topology=True)
            ).sum()
        )
        for group in groups
        if len(group)
    )


def _simplify_and_round(geometries: np.ndarray, tolerance: float) -> np.ndarray:
    """Simplified, rounded copies — what will actually be drawn.

    `preserve_topology=True` throughout. The faster Douglas-Peucker will happily
    turn a boundary inside out or detach a hole, and an outline that has been
    torn is not a cheaper picture of the truth, it is a different claim.

    Rounding happens here rather than at encoding time so that the displacement
    measured downstream is measured against the coordinates that reach the page,
    not against an intermediate nobody ever sees.
    """
    if len(geometries) == 0:
        return geometries
    simplified = shapely.simplify(geometries, tolerance, preserve_topology=True)
    return shapely.transform(
        simplified, lambda coordinates: np.round(coordinates, COORDINATE_DECIMALS)
    )


_POLYGONAL_TYPE_IDS = frozenset({3, 6})  # Polygon, MultiPolygon
_COLLECTION_TYPE_ID = 7  # GeometryCollection
_POINT_TYPE_ID = 0
_MULTIPART_TYPE_IDS = frozenset({4, 5, 6, 7})  # MultiPoint … GeometryCollection


def _draws_as_something(geometry: Any) -> bool:
    """Would this shape put a mark on the page?

    `is_empty` is not that question, and the difference is a hole in the
    module's central promise that nothing here is silent. A 0.05 m polygon —
    ordinary in a parcel or building-footprint file — survives rounding to six
    decimal places as four *coincident* corners: a ring that encloses nothing,
    zero area, `is_valid` False, and not empty. So it was counted as drawn, it
    was published as a feature, it was left out of `dropped_positions`, no note
    mentioned it, and the page rendered nothing where the payload said a shape
    was. Worse when that row was highlighted: the payload asserted a marked row
    was on the map for the operator to look at, and there was nothing there.

    A shape marks the page if it has two corners that are not the same corner —
    or, for a point, one corner at all, a point being the one thing that draws
    without extent. Multi-part shapes and collections are asked part by part,
    because a MultiPolygon of collapsed rings draws exactly as much as one
    collapsed ring does: nothing.
    """
    if geometry is None or geometry.is_empty:
        return False
    type_id = int(shapely.get_type_id(geometry))
    if type_id in _MULTIPART_TYPE_IDS:
        return any(_draws_as_something(part) for part in geometry.geoms)
    coordinates = shapely.get_coordinates(geometry)
    if len(coordinates) == 0:
        return False
    if type_id == _POINT_TYPE_ID:
        return True
    # "Two corners that are not the same corner" is "any corner unlike the
    # first", which is one linear sweep rather than a sort of every coordinate —
    # this runs over every feature of every layer on every preview.
    return bool(np.any(coordinates != coordinates[0]))


def _drawn_linework(geometry: Any) -> Any | None:
    """The lines a drawn shape is actually made of, or None if it has none.

    This exists because `.boundary` is not that thing. `.boundary` is the
    topological boundary, which is the right answer for an area and the wrong
    answer for everything else:

    * a polygon's boundary is its rings — correct, and the reason the measure is
      taken against lines at all (the distance from a point to an *area* it sits
      inside is zero, which would report a boundary that had moved as one that
      had not);
    * a line's boundary is its two end *points*, so measuring against it asks
      "how far is this corner from the ends of the line?" — a LineString that
      simplification did not touch at all reported 20 km of displacement;
    * a GeometryCollection has no defined boundary and shapely returns None, so
      every distance came back NaN.

    So: areas are measured against their rings, lines and points against
    themselves, and a collection against the linework of each of its parts,
    recursively. Nothing here can return a shape a distance cannot be taken to;
    when there is genuinely nothing to measure against it returns None and the
    caller reports the displacement as unknown rather than as zero.

    ArcGIS / ArcPy equivalent
        `arcpy.management.FeatureToLine` / `PolygonToLine`, which likewise turn
        areas into the linework `arcpy.analysis.Near` can measure to. ArcGIS
        also refuses a mixed-geometry input rather than silently returning null
        distances for it.
    """
    if geometry is None or geometry.is_empty:
        return None
    type_id = int(shapely.get_type_id(geometry))
    if type_id in _POLYGONAL_TYPE_IDS:
        boundary = geometry.boundary
        if boundary is None or boundary.is_empty:
            return None
        return boundary
    if type_id == _COLLECTION_TYPE_ID:
        parts = [_drawn_linework(part) for part in geometry.geoms]
        parts = [part for part in parts if part is not None]
        if not parts:
            return None
        return shapely.geometrycollections(np.asarray(parts, dtype=object))
    # Points and lines are already the thing to measure to.
    return geometry


def _worst_displacement(before: np.ndarray, after: np.ndarray) -> float | None:
    """The furthest any original corner point sits from the drawn linework, in
    whatever units the coordinates are in — or None if that could not be
    measured.

    One-sided on purpose, and exact for this kind of simplification: every
    corner point of the drawn outline is one of the original's (simplification
    only removes points; rounding then moves them by less than half a unit in
    the last place), so the interesting direction is original → drawn. Measured
    against the drawn shape's linework (see `_drawn_linework`) rather than the
    area it encloses, because the distance from a point to an area it happens to
    sit inside is zero and would report a boundary that had moved as one that
    had not.

    A discrete Hausdorff distance would compare corner point to corner point
    instead, and so would report a removed midpoint of a long straight run as a
    large error. This measures point to line, which is the question actually
    being asked: how far is the truth from what is drawn?

    Fails closed, and that is the whole point of the return type. An earlier
    version dropped non-finite distances and kept the running maximum at its
    initial 0.0, so a shape it could not measure at all reported perfect
    fidelity. Here, any distance that does not come back finite, and any drawn
    shape with no linework to measure to, makes the answer None — unknown — and
    the payload says so.

    Shapes that vanished entirely (`drawn` empty) are not measured and do not
    make the answer unknown on their own: they are disclosed by name in
    `DrawnLayer.dropped_positions` and in a note, which is a louder statement
    than a number. But if *nothing* in a layer survived to be measured, there is
    no measurement, and the answer is None rather than a comfortable zero.
    """
    worst = 0.0
    measurable = 0
    measured = 0
    for original, drawn in zip(before, after):
        if original is None:
            continue
        coordinates = shapely.get_coordinates(original)
        if len(coordinates) == 0:
            continue
        measurable += 1
        if not _draws_as_something(drawn):
            # Not on the map, so not measured — and disclosed by row number in
            # `DrawnLayer.dropped_positions`, which is a louder statement than a
            # distance. Counting it as well drawn because its collapsed remains
            # happen to sit near the original would be the same lie in a quieter
            # voice.
            continue
        linework = _drawn_linework(drawn)
        if linework is None:
            return None
        distances = _distances_to_linework(coordinates, linework)
        if distances is None:
            return None
        measured += 1
        worst = max(worst, float(distances.max()))
    if measurable and not measured:
        return None
    return worst


def _straight_runs(linework: Any) -> np.ndarray:
    """The drawn linework cut into its individual straight pieces.

    Only so that a spatial index has something to index. A single 100,000-point
    outline is one geometry to GEOS and every point must be compared against all
    of it; the same outline as 100,000 two-point pieces is something an R-tree
    can put in a box and skip.
    """
    pieces: list[Any] = []
    for part in _flat_parts(linework):
        coordinates = shapely.get_coordinates(part)
        if len(coordinates) == 0:
            continue
        if len(coordinates) == 1:
            pieces.append(shapely.points(coordinates))
            continue
        pairs = np.stack([coordinates[:-1], coordinates[1:]], axis=1).reshape(-1, 2)
        indices = np.repeat(np.arange(len(coordinates) - 1), 2)
        pieces.extend(shapely.linestrings(pairs, indices=indices).tolist())
    return np.asarray(pieces, dtype=object)


def _flat_parts(geometry: Any) -> Iterable[Any]:
    """Every single-part shape inside this one, however deeply nested."""
    if geometry is None or geometry.is_empty:
        return
    if int(shapely.get_type_id(geometry)) in _MULTIPART_TYPE_IDS:
        for part in geometry.geoms:
            yield from _flat_parts(part)
        return
    yield geometry


def _distances_to_linework(
    coordinates: np.ndarray, linework: Any
) -> np.ndarray | None:
    """Every original corner's distance to the drawn linework, or None if any of
    them could not be taken.

    Exact, by either route. `shapely.distance` against the whole outline is
    unindexed and so costs points x segments: a 100,000-point coastline — an
    ordinary full-resolution TIGER county or shoreline for this tool's audience —
    took 47 seconds inside `build_preview` with no progress shown and no bound,
    and doubling the point count quadrupled it. The vertex cap does not help,
    because the cap limits what is *drawn* while this measurement runs against
    the originals, which are never capped.

    So above a size worth the setup the linework goes into an R-tree first and
    each point asks it for its nearest piece, which is n log n. This is not a
    sample and not an approximation: `query_nearest` returns the true nearest
    piece, and the distance to the nearest piece of a line is the distance to the
    line. M1's guarantee is untouched — a point the tree returns no answer for,
    or any distance that comes back non-finite, still makes the whole measurement
    unknown rather than a number.

    ArcGIS / ArcPy equivalent
        Building a spatial index on the near features before
        `arcpy.analysis.Near`, which Pro does for you and does not mention.
    """
    points = shapely.points(coordinates)
    if (
        len(points) >= INDEXED_MEASUREMENT_MIN_SEGMENTS
        and int(shapely.get_num_coordinates(linework))
        >= INDEXED_MEASUREMENT_MIN_SEGMENTS
    ):
        pieces = _straight_runs(linework)
        if len(pieces) == 0:
            return None
        _matched, distances = shapely.STRtree(pieces).query_nearest(
            points, all_matches=False, return_distance=True
        )
        if len(distances) != len(points):
            # A point the index could not answer for. Unknown, not "far".
            return None
    else:
        distances = shapely.distance(points, linework)
    if not bool(np.all(np.isfinite(distances))):
        return None
    return distances


def _worst_displacement_metres(
    before: np.ndarray, after: np.ndarray, metric_crs: str
) -> float | None:
    """`_worst_displacement`, measured on the ground.

    `metric_crs` is an azimuthal equidistant projection centred on the viewport:
    distances from its centre are true metres, which is exactly the measurement
    wanted and is not something a state plane grid or Web Mercator gives. It is
    only honest within a bounded radius of that centre, which is why the caller
    obtains it from `_metric_crs_for` — that function refuses to hand one back
    for an extent too wide for it, and the displacement is then reported as
    unknown instead of being measured on a ruler that has stretched.
    """
    if len(before) == 0:
        return 0.0
    projected_before = _project(before, metric_crs)
    projected_after = _project(after, metric_crs)
    return _worst_displacement(projected_before, projected_after)


def _worst_of(values: Iterable[float | None]) -> float | None:
    """The largest of these displacements, or None if any one is unknown.

    Unknown wins over any number. The payload states a single worst-case figure
    across every layer drawn, and a worst case computed from only the parts that
    could be measured is not a worst case.
    """
    worst = 0.0
    for value in values:
        if value is None:
            return None
        worst = max(worst, value)
    return worst


def _project(geometries: np.ndarray, crs: str) -> np.ndarray:
    if len(geometries) == 0:
        return geometries
    return np.asarray(
        gpd.GeoSeries(geometries, crs=WGS84).to_crs(crs).to_numpy(), dtype=object
    )


def _separation_metres(candidate: np.ndarray, installed: np.ndarray) -> float | None:
    """How far apart these two layers sit on the ground; 0.0 when they touch,
    None when it could not be measured.

    Both arrays are in WGS84 degrees, and the answer is a true geodesic distance
    on the ellipsoid — not a distance read off a projection. That distinction is
    not academic. This number is rendered as "the nearest layer already
    installed is N km away from this one … that is the wrong file", which is an
    accusation the operator acts on, so it has to be right everywhere and not
    merely near some chosen centre. Measured on a single azimuthal equidistant
    projection it was not: two boxes 22,264 m apart across the antimeridian came
    out at 2,489,180 m, because the viewport's centre longitude averaged +179
    and -179 to 0 and put the projection's origin halfway round the world from
    the data it was measuring.

    Measured on the originals, not on what is drawn: this number is the page's
    warning that a file covers the wrong ground, and it should not inherit the
    error of a display simplification.

    Three steps, and the first two exist only to find *which* pair of points to
    put the geodesic ruler on:

    1. The installed layer is tried at longitude +360, 0 and -360. Degree
       coordinates cut the world at the antimeridian, so a layer at 179.9 and
       one at -179.9 are 0.2 degrees apart on the ground and 359.8 apart in the
       numbers; shifting a whole copy re-joins them. The smallest true distance
       across the three wins.
    2. Longitudes are scaled by cos(latitude) before the nearest pair is looked
       for, so that "nearest" means nearest on the ground rather than nearest in
       degrees, which at Chicago's latitude would be a third out crosswise.
    3. `GEODESIC.inv` then measures that pair exactly.

    Steps 1 and 2 only *choose* the pair; any error left in the choice can only
    make the answer larger, never smaller, so this cannot under-report a gap and
    call a wrong file right.

    ArcGIS / ArcPy equivalent
        `arcpy.analysis.Near` with `method="GEODESIC"` — the same deliberate
        choice of the geodesic method over the default `PLANAR`, and for the
        same reason.
    """
    if len(candidate) == 0 or len(installed) == 0:
        return None
    left = shapely.geometrycollections(np.asarray(candidate, dtype=object))
    right = shapely.geometrycollections(np.asarray(installed, dtype=object))

    best: float | None = None
    for shift in (0.0, 360.0, -360.0):
        shifted = right if shift == 0.0 else _shifted_east(right, shift)
        try:
            if left.intersects(shifted):
                return 0.0
            near_left, near_right = shapely.ops.nearest_points(
                *_longitude_scaled(left, shifted)
            )
        except Exception:  # pragma: no cover - shapely refuses the geometry
            continue
        scale = _longitude_scale_for(left, shifted)
        _azimuth, _back, metres = GEODESIC.inv(
            near_left.x / scale, near_left.y, near_right.x / scale, near_right.y
        )
        if not math.isfinite(metres):
            continue
        distance = abs(float(metres))
        if best is None or distance < best:
            best = distance
    return best


def _shifted_east(geometry: Any, degrees: float) -> Any:
    """A copy of `geometry` moved a whole number of turns around the world.

    The same ground, written down differently — which is the point: it is the
    writing-down that puts a 359.8-degree gap between two neighbours either side
    of the antimeridian.
    """

    def move(coordinates: np.ndarray) -> np.ndarray:
        moved = coordinates.copy()
        moved[:, 0] += degrees
        return moved

    return shapely.transform(geometry, move)


def _longitude_scale_for(left: Any, right: Any) -> float:
    """cos(latitude) midway between two shapes — one degree of longitude as a
    fraction of one degree of latitude, there."""
    _, min_y_left, _, max_y_left = left.bounds
    _, min_y_right, _, max_y_right = right.bounds
    middle = (min_y_left + max_y_left + min_y_right + max_y_right) / 4.0
    return max(math.cos(math.radians(middle)), 0.01)


def _longitude_scaled(left: Any, right: Any) -> tuple[Any, Any]:
    """Both shapes with longitude squeezed so that a unit east is a unit north.

    Only so that "the nearest pair of points" means the same thing it would on
    the ground. The pair chosen here is measured properly afterwards; these
    squeezed copies never leave this function.
    """
    scale = _longitude_scale_for(left, right)

    def squeeze(coordinates: np.ndarray) -> np.ndarray:
        squeezed = coordinates.copy()
        squeezed[:, 0] *= scale
        return squeezed

    return shapely.transform(left, squeeze), shapely.transform(right, squeeze)


def _with_separation(layer: DrawnLayer, distance: float) -> DrawnLayer:
    return DrawnLayer(
        id=layer.id,
        name=layer.name,
        role=layer.role,
        geojson=layer.geojson,
        feature_count=layer.feature_count,
        vertex_count=layer.vertex_count,
        dropped_positions=layer.dropped_positions,
        separation_metres=distance,
    )


# --------------------------------------------------------------------------
# the viewport, and the tolerance that comes out of it
# --------------------------------------------------------------------------


def _viewport_from(
    groups: Sequence[np.ndarray], width_px: int, height_px: int, *, units: str
) -> Viewport | None:
    """A rectangle containing every shape in `groups`, or None if there are none."""
    boxes = [
        shapely.total_bounds(group) for group in groups if len(group)
    ]
    boxes = [box for box in boxes if all(math.isfinite(value) for value in box)]
    if not boxes:
        return None
    min_x = min(float(box[0]) for box in boxes)
    min_y = min(float(box[1]) for box in boxes)
    max_x = max(float(box[2]) for box in boxes)
    max_y = max(float(box[3]) for box in boxes)
    return _fit(min_x, min_y, max_x, max_y, width_px, height_px, units=units)


def _fit(
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    width_px: int,
    height_px: int,
    *,
    units: str,
) -> Viewport:
    """Pad the box, give it a floor, and stretch it to the pixel box's shape.

    Fitting the aspect ratio here rather than in the page is what makes
    `units_per_pixel` — and so the tolerance, and so the fidelity claim — mean
    something. A page free to letterbox the rectangle however it liked would
    draw at a scale this module never knew about.
    """
    longitude_scale = 1.0
    if units == "degrees":
        middle_latitude = (min_y + max_y) / 2.0
        longitude_scale = max(math.cos(math.radians(middle_latitude)), 0.01)

    span_x = max(max_x - min_x, 0.0)
    span_y = max(max_y - min_y, 0.0)
    floor = MIN_VIEWPORT_SPAN_DEGREES if units == "degrees" else max(
        MIN_VIEWPORT_SPAN_DEGREES, max(abs(min_x), abs(min_y)) * 1e-9
    )
    # A single point, or a whole layer collapsed onto one, still has to produce
    # a rectangle something can be drawn in.
    span_x = max(span_x, floor)
    span_y = max(span_y, floor)
    centre_x = (min_x + max_x) / 2.0
    centre_y = (min_y + max_y) / 2.0

    span_x *= 1.0 + 2 * VIEWPORT_MARGIN
    span_y *= 1.0 + 2 * VIEWPORT_MARGIN

    # Match the pixel box's shape in the space the page will actually draw in,
    # which is x already multiplied by `longitude_scale`.
    pixel_aspect = width_px / height_px
    scaled_span_x = span_x * longitude_scale
    if scaled_span_x / span_y < pixel_aspect:
        scaled_span_x = span_y * pixel_aspect
        span_x = scaled_span_x / longitude_scale
    else:
        span_y = scaled_span_x / pixel_aspect

    return Viewport(
        min_x=centre_x - span_x / 2.0,
        min_y=centre_y - span_y / 2.0,
        max_x=centre_x + span_x / 2.0,
        max_y=centre_y + span_y / 2.0,
        width_px=width_px,
        height_px=height_px,
        longitude_scale=longitude_scale,
        units=units,
    )


def _tolerance_for(viewport: Viewport | None) -> tuple[float, float | None]:
    """The simplification tolerance, in coordinate units and in metres.

    Two rules, and the tighter of the two wins.

    One screen pixel
        Detail finer than a pixel cannot be seen, so removing it costs the
        operator nothing. This is the rule that makes the tolerance a property
        of the picture rather than a constant somebody guessed.

    `FIDELITY_CEILING_METRES`
        And this is why the pixel rule cannot stand alone. Cook County is about
        60 km across; one pixel of a 900-pixel-wide view of it is roughly 67 m
        of ground, and simplifying at 67 m smooths away a boundary that has
        moved by a city block. The preview would then draw a stale file and a
        current one as the same picture — the exact failure this module exists
        to prevent, arrived at by an argument that sounded reasonable.

        So the pixel rule is capped at a tolerance stated in metres, which at
        county scale is what actually governs. The cost is a payload of a few
        hundred kilobytes instead of a few tens; the benefit is that the one
        thing only a human can catch stays catchable.
    """
    if viewport is None:
        return FIDELITY_CEILING_METRES / METRES_PER_DEGREE_LATITUDE, (
            FIDELITY_CEILING_METRES
        )
    pixel = viewport.units_per_pixel
    if viewport.units != "degrees":
        # No ground truth to hold the tolerance to, so the picture's own rule is
        # the only one there is — and it is the right one here, because an
        # unplaceable candidate is being looked at for its shape alone.
        return pixel, None
    ceiling = FIDELITY_CEILING_METRES / METRES_PER_DEGREE_LATITUDE
    tolerance = min(pixel, ceiling)
    return tolerance, tolerance * METRES_PER_DEGREE_LATITUDE


def _metric_crs_for(viewport: Viewport | None) -> str | None:
    """An equal-distance projection centred on what is being drawn, or None when
    no single projection can measure this extent honestly.

    Azimuthal equidistant about the viewport's middle: distance from that centre
    is true, which is what the displacement figure is measured with. A state
    plane grid would be nearly as good over one county and wrong for a candidate
    that turned out to be three states away; Web Mercator would be wrong
    everywhere.

    But "true from the centre" is the whole of the promise. Crosswise, the
    projection stretches by theta/sin(theta) with distance from that centre, and
    a viewport wide enough puts its own contents where that factor is not one —
    a case that was quietly inflating a 0.51 m fidelity headline to 29.77 m,
    which is not a small error in a number the operator is asked to trust. So
    the distortion at the furthest corner of the viewport is computed, and past
    `METRIC_PROJECTION_MAX_DISTORTION` this hands back nothing. The caller then
    reports the displacement as unknown, with a note saying the area covered was
    too large to measure on — which is a thing the payload can now say. It could
    not before, which is why the wrong number went out instead.

    The separation between layers does *not* come through here: it is geodesic
    (see `_separation_metres`) and has no such limit.

    ArcGIS / ArcPy equivalent
        Choosing a projected coordinate system for a data frame before running
        `arcpy.analysis.Near` in `PLANAR` mode — and the check is the discipline
        Pro leaves to the analyst, of not measuring on a projection outside the
        extent it was designed for.
    """
    if viewport is None or viewport.units != "degrees":
        return None
    centre_x = (viewport.min_x + viewport.max_x) / 2.0
    centre_y = (viewport.min_y + viewport.max_y) / 2.0
    if _projection_distortion(viewport) > METRIC_PROJECTION_MAX_DISTORTION:
        return None
    return (
        f"+proj=aeqd +lat_0={centre_y} +lon_0={centre_x} "
        f"+datum=WGS84 +units=m +no_defs"
    )


def _projection_distortion(viewport: Viewport) -> float:
    """How much an azimuthal equidistant projection centred on this viewport
    stretches at the viewport's furthest corner: 1.0 is no stretch.

    theta/sin(theta), where theta is the angle the corner subtends at the
    Earth's centre. 1.000004 over a county, 1.01 at about 1,550 km, and
    unbounded toward the antipode.

    A viewport wider than the world, or taller than pole to pole, is refused
    outright. Padding and the aspect-ratio fit can push a rectangle's corners
    past latitude 90 — where a geodesic distance is not a meaningful thing to
    ask for, and where asking for one anyway came back small and reassuring.
    Latitudes are clamped before measuring for the same reason.
    """
    if viewport.span_x > 360.0 or viewport.span_y > 180.0:
        return math.inf
    centre_x = (viewport.min_x + viewport.max_x) / 2.0
    centre_y = _clamped_latitude((viewport.min_y + viewport.max_y) / 2.0)
    corners = (
        (viewport.min_x, viewport.min_y),
        (viewport.min_x, viewport.max_y),
        (viewport.max_x, viewport.min_y),
        (viewport.max_x, viewport.max_y),
    )
    radius = 0.0
    for corner_x, corner_y in corners:
        _azimuth, _back, metres = GEODESIC.inv(
            centre_x, centre_y, corner_x, _clamped_latitude(corner_y)
        )
        if not math.isfinite(metres):
            return math.inf
        radius = max(radius, abs(float(metres)))
    theta = radius / EARTH_RADIUS_METRES
    if theta <= 0.0:
        return 1.0
    if theta >= math.pi:
        return math.inf
    return theta / math.sin(theta)


# --------------------------------------------------------------------------
# features
# --------------------------------------------------------------------------


def _clamped_latitude(degrees: float) -> float:
    """A latitude a geodesic can actually be measured to."""
    return max(-90.0, min(90.0, degrees))


def _drawn_layer(
    geometries: np.ndarray,
    row_positions: Sequence[int],
    *,
    layer_id: str,
    name: str,
    role: str,
    highlight: set[int],
) -> tuple[DrawnLayer, tuple[int, ...]]:
    """One layer's GeoJSON, every feature carrying the row it came from.

    The id is the row's position in the operator's file, counting every row from
    the top including ones with nothing drawn in them — the same counting
    PIP-L008 uses for `broken_positions`, so "the preview map marks each one" is
    a promise this can keep. It survives simplification (which never reorders)
    and it survives dropping (which removes features and never renumbers them),
    which is the whole reason the position travels with the geometry rather than
    being recovered from a list index later.
    """
    features: list[dict[str, Any]] = []
    dropped: list[int] = []
    vertex_count = 0
    for geometry, position in zip(geometries, row_positions):
        if not _draws_as_something(geometry):
            # `is_empty` was the test here, and it is the wrong one: a shape too
            # small to survive rounding comes back as coincident corners rather
            # than as nothing, and was published as a feature that draws no mark.
            dropped.append(int(position))
            continue
        vertex_count += int(shapely.get_num_coordinates(geometry))
        features.append(
            {
                "type": "Feature",
                "id": int(position),
                "properties": {
                    "row": int(position),
                    "highlighted": int(position) in highlight,
                },
                "geometry": json.loads(shapely.to_geojson(geometry)),
            }
        )
    return (
        DrawnLayer(
            id=layer_id,
            name=name,
            role=role,
            geojson={"type": "FeatureCollection", "features": features},
            feature_count=len(features),
            vertex_count=vertex_count,
            dropped_positions=tuple(dropped),
        ),
        tuple(dropped),
    )


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _crs_text(crs: Any) -> str | None:
    if crs is None:
        return None
    try:
        code = crs.to_string()
    except AttributeError:
        return str(crs)
    return str(code)


def _displacement_phrase(
    metres: float | None, units: float | None, comparable: bool
) -> str:
    """How far the drawn outline may sit from the real one, said in a sentence.

    Never a number this module did not measure. "up to 0.0 m" is the single most
    dangerous string this file could emit — a claim of perfect fidelity, in the
    sentence the operator trusts — so an unknown says it is unknown.
    """
    if metres is not None:
        return (
            f"Every boundary here may sit up to {metres:,.1f} m from where it "
            f"really is."
        )
    if not comparable and units is not None:
        return (
            f"Every boundary here may sit up to {units:g} of the file's own "
            f"units from where it really is — this file says nothing about "
            f"where on Earth it is, so that cannot be turned into metres."
        )
    return (
        "How far the boundaries drawn here sit from the real ones is not "
        "something this tool could measure. It is not saying they are accurate; "
        "it is saying it does not know."
    )


def _distance_phrase(metres: float) -> str:
    """A distance said the way somebody would say it out loud."""
    if metres >= 1000.0:
        return f"{metres / 1000.0:,.0f} km"
    return f"{metres:,.0f} m"


__all__ = [
    "DrawableLayer",
    "DrawnLayer",
    "Preview",
    "Simplification",
    "Viewport",
    "build_preview",
    "load_installed_layers",
    "FIDELITY_CEILING_METRES",
    "REVEALS_OFFSET_METRES",
    "MAX_TOTAL_VERTICES",
    "UNCOMPARABLE_NO_CRS",
    "UNCOMPARABLE_UNPROJECTABLE",
    "UNDRAWABLE_TOO_DETAILED",
]
