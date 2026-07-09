"""F4 — the FastAPI service exposing the SPEC §4 contract.

This is the point-in-polygon slice, standing up the service early to back a
manual test page (F6): `GET /health`, `GET /layers`, and `POST /locate`
(point-in-polygon only, no geocoding). The address-based `GET /geocode` and
`GET /locate` endpoints arrive with the geocoder (F3/F5).

The static test UI is served from `static/` at the root, so `uvicorn app.main:app`
gives you both the JSON API and a page to exercise it. Run with
`--no-access-log` so query parameters are never written to a log (SPEC §9,
no-PII; D5).

ArcGIS / ArcPy equivalent
    This is the open-source analogue of publishing an ArcGIS GeoProcessing
    service or a GeoServices REST endpoint — the same "point in, JSON out" HTTP
    surface, served by FastAPI/uvicorn instead of ArcGIS Server, with no license.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app.config import AppConfig, load_config
from app.errors import install_error_handlers
from app.lookup import PolygonLookup
from app.models import (
    HealthResponse,
    LayerInfo,
    LayersResponse,
    LocatePointRequest,
    LocatePointResponse,
    MatchModel,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"


def _service_version() -> str:
    try:
        return version("point-in-polygon-service")
    except PackageNotFoundError:
        return "0.1.0"  # running from source, package not installed


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Build the app. Loads config + layers once at startup (fail-fast)."""
    app = FastAPI(
        title="Point-in-Polygon Service",
        version=_service_version(),
        description="Given an address or point, which polygon of a layer contains it?",
    )

    app_config = config if config is not None else load_config()
    lookup = PolygonLookup(app_config)
    app.state.config = app_config
    app.state.lookup = lookup

    install_error_handlers(app)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok", version=_service_version())

    @app.get("/layers", response_model=LayersResponse)
    def layers(request: Request) -> LayersResponse:
        config: AppConfig = request.app.state.config
        engine: PolygonLookup = request.app.state.lookup
        return LayersResponse(
            layers=[
                LayerInfo(
                    id=layer.id,
                    name=layer.name,
                    feature_count=engine.feature_count(layer.id),
                    attributes=list(layer.attributes),
                    source=layer.source,
                )
                for layer in config.layers.values()
            ]
        )

    @app.post(
        "/locate",
        response_model=LocatePointResponse,
        response_model_exclude_none=True,
    )
    def locate_point(body: LocatePointRequest, request: Request) -> LocatePointResponse:
        engine: PolygonLookup = request.app.state.lookup
        # UnknownLayerError / InvalidCoordinateError are mapped to 404 / 400 by
        # the installed handlers (SPEC §4 error model).
        match = engine.locate(body.lon, body.lat, body.layer)
        return LocatePointResponse(
            layer=body.layer,
            match=MatchModel(found=match.found, feature=match.feature, reason=match.reason),
        )

    # Serve the static test UI last, so the API routes above take precedence and
    # everything else (/, /app.js, /style.css) is served from static/.
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app


app = create_app()
