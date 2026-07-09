"""API request/response models (SPEC §4 shapes).

These Pydantic models are the single source of the OpenAPI contract — the schema
at /openapi.json is generated from them, so it cannot drift from the
implementation (SPEC §9).

This is the point-in-polygon slice of the API (F4), pulled forward to back a
manual test page (F6). The geocoding endpoints (`GET /geocode`, `GET /locate`
with an address) arrive with F3/F5.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    version: str


class LayerInfo(BaseModel):
    id: str
    name: str
    feature_count: int
    attributes: list[str]
    source: str


class LayersResponse(BaseModel):
    layers: list[LayerInfo]


class LocatePointRequest(BaseModel):
    """`POST /locate` body — point-in-polygon only, no geocoding.

    Coordinate *range* validation lives in the engine (one place — root cause),
    which raises InvalidCoordinateError → 400. Pydantic here enforces presence
    and type.
    """

    lat: float = Field(..., description="WGS84 latitude in degrees")
    lon: float = Field(..., description="WGS84 longitude in degrees")
    layer: str = Field(..., description="configured layer id, e.g. 'police_districts'")


class MatchModel(BaseModel):
    """The §4 match shape. Serialized with exclude_none, so a hit carries only
    `found` + `feature` and a miss only `found` + `reason`."""

    found: bool
    feature: dict | None = None
    reason: str | None = None


class LocatePointResponse(BaseModel):
    layer: str
    match: MatchModel
