"""F2-T2 — the generic point-in-polygon engine.

`PolygonLookup` is the layer-agnostic core of the service (SPEC §1). It loads
the configured polygon layers once at startup, builds an in-memory spatial index
per layer, and answers `locate(lon, lat, layer_id)`: which polygon of that layer
covers the point?

Coordinates in are always WGS84 (EPSG:4326) lon/lat, per the API contract
(SPEC §4). Each layer keeps its own native CRS (EPSG:3435 for the Cook County
data); the query point is reprojected onto that CRS rather than reprojecting the
authoritative polygons (D3).

ArcGIS / ArcPy equivalent
    This replaces `arcpy.management.SelectLayerByLocation` (point → containing
    polygons) followed by reading the selected row's fields — or the interactive
    Identify tool. The shapely STRtree plays the role of the feature class's
    spatial index; `covers` is the "INTERSECT / WITHIN" spatial relationship.
"""
from __future__ import annotations

from dataclasses import dataclass

import geopandas as gpd
from pyproj import Transformer
from shapely import STRtree
from shapely.geometry import Point

from app.config import AppConfig, LayerConfig

# WGS84 lon/lat is the API's coordinate system (SPEC §4).
WGS84 = "EPSG:4326"

# The reason string SPEC §4 mandates when a point geocodes but lands in no
# polygon of the requested layer.
OUTSIDE_ALL_POLYGONS = "point_outside_all_polygons"


class UnknownLayerError(Exception):
    """Requested a layer id that is not configured (→ HTTP 404 at F4)."""


class InvalidCoordinateError(Exception):
    """lon/lat outside valid WGS84 range (→ HTTP 400 at F4)."""


@dataclass(frozen=True)
class Match:
    """Result of a lookup: a hit carries the layer's attributes; a miss a reason."""

    found: bool
    feature: dict | None = None
    reason: str | None = None


def _to_python(value):
    """Convert a numpy/pandas scalar to a plain Python value for JSON output."""
    item = getattr(value, "item", None)
    return item() if callable(item) else value


class _LoadedLayer:
    """A configured layer loaded into memory with its spatial index and transformer."""

    def __init__(self, config: LayerConfig):
        frame = gpd.read_file(config.path, layer=config.layer).reset_index(drop=True)
        if frame.crs is None:
            raise ValueError(f"layer {config.id!r} has no CRS")

        missing = [attr for attr in config.attributes if attr not in frame.columns]
        if missing:
            raise ValueError(
                f"layer {config.id!r} is missing configured attributes {missing}"
            )

        self.config = config
        self.frame = frame
        # STRtree indices are positional, aligned to frame.iloc (reset above).
        self._tree = STRtree(frame.geometry.values)
        # Reproject query points WGS84 -> this layer's native CRS (D3). always_xy
        # keeps input in (lon, lat) order regardless of the CRS axis convention.
        self._transformer = Transformer.from_crs(WGS84, frame.crs, always_xy=True)

    def locate(self, lon: float, lat: float) -> Match:
        x, y = self._transformer.transform(lon, lat)
        point = Point(x, y)

        # STRtree.query applies predicate(input, tree_geometry), so we ask for
        # tree polygons that the point is COVERED BY — the dual of "polygon
        # covers point". covered_by (unlike within) includes the boundary, so a
        # point exactly on an edge still matches (D4).
        indices = self._tree.query(point, predicate="covered_by")
        if len(indices) == 0:
            return Match(found=False, reason=OUTSIDE_ALL_POLYGONS)

        # A point on a shared edge can be covered by more than one polygon.
        # Return the first by a stable sort on the layer's first configured
        # attribute, so the answer is deterministic (D4).
        if len(indices) > 1:
            first_attr = self.config.attributes[0]
            indices = sorted(
                indices, key=lambda i: str(self.frame.iloc[int(i)][first_attr])
            )

        row = self.frame.iloc[int(indices[0])]
        feature = {attr: _to_python(row[attr]) for attr in self.config.attributes}
        return Match(found=True, feature=feature)


class PolygonLookup:
    """Config-driven point-in-polygon over one or more polygon layers."""

    def __init__(self, config: AppConfig):
        self._layers = {
            layer_id: _LoadedLayer(layer_config)
            for layer_id, layer_config in config.layers.items()
        }

    @property
    def layer_ids(self) -> tuple[str, ...]:
        return tuple(self._layers)

    def feature_count(self, layer_id: str) -> int:
        return len(self._require(layer_id).frame)

    def locate(self, lon: float, lat: float, layer_id: str) -> Match:
        """Which polygon of `layer_id` covers (lon, lat)? WGS84 degrees in."""
        if not (-180.0 <= lon <= 180.0) or not (-90.0 <= lat <= 90.0):
            raise InvalidCoordinateError(
                f"lon/lat out of range: ({lon}, {lat}); "
                f"expected lon in [-180, 180], lat in [-90, 90]"
            )
        return self._require(layer_id).locate(lon, lat)

    def _require(self, layer_id: str) -> _LoadedLayer:
        try:
            return self._layers[layer_id]
        except KeyError:
            raise UnknownLayerError(
                f"unknown layer {layer_id!r}; configured: {list(self._layers)}"
            ) from None
