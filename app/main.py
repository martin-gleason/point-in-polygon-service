"""F4 — the FastAPI service exposing the SPEC §4 contract.

Endpoints:
- `GET /health` — liveness + version.
- `GET /layers` — the configured polygon layers.
- `GET /geocode?address=&provider=` — address → point (geocode only).
- `GET /locate?address=&layer=&provider=` — the headline endpoint: geocode → point-in-polygon.
- `POST /locate` — point-in-polygon only, no geocoding (the pure generic use).

The static test UI is served from `static/` at the root, so `uvicorn app.main:app`
gives both the JSON API and a page to exercise it. Run with `--no-access-log` so
addresses (which the §4 GET endpoints carry in the query string) are never
written to a log (SPEC §9, no-PII; D5).

ArcGIS / ArcPy equivalent
    The open-source analogue of publishing an ArcGIS GeoProcessing / GeoServices
    REST endpoint — "address or point in, JSON out" — served by FastAPI/uvicorn
    instead of ArcGIS Server, with no license.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.staticfiles import StaticFiles

from app.config import AppConfig, load_config
from app.errors import install_error_handlers
from app.geocoding.base import GeocodeResult, Geocoder, GeocoderUnavailable
from app.geocoding.registry import UnknownProviderError, build_geocoders
from app.lookup import PolygonLookup, UnknownLayerError
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


def _geocode_dict(result: GeocodeResult) -> dict:
    """The SPEC §4 geocode object. A match carries point/score/matched_address;
    a no-match carries `point: null` and omits score/matched_address."""
    body = {"query": result.query, "matched": result.matched, "provider": result.provider}
    if result.matched and result.point is not None:
        longitude, latitude = result.point
        body["point"] = {"lon": longitude, "lat": latitude}
        body["score"] = result.score
        body["matched_address"] = result.matched_address
    else:
        body["point"] = None
    return body


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Build the app. Loads config, layers, and geocoders once at startup
    (fail-fast on a bad config or missing data)."""
    app = FastAPI(
        title="Point-in-Polygon Service",
        version=_service_version(),
        description="Given an address or point, which polygon of a layer contains it?",
    )

    app_config = config if config is not None else load_config()
    app.state.config = app_config
    app.state.lookup = PolygonLookup(app_config)
    app.state.geocoders = build_geocoders(app_config)
    app.state.default_geocoder = app_config.default_geocoder

    install_error_handlers(app)

    def select_geocoder(request: Request, provider: str | None) -> Geocoder:
        geocoders: dict[str, Geocoder] = request.app.state.geocoders
        provider_id = provider or request.app.state.default_geocoder
        if provider_id is None:
            raise GeocoderUnavailable("no geocoder is configured")
        try:
            return geocoders[provider_id]
        except KeyError:
            raise UnknownProviderError(
                f"unknown provider {provider!r}; configured: {sorted(geocoders)}"
            ) from None

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

    @app.get("/geocode")
    def geocode(
        request: Request,
        address: str = Query(..., description="street address to geocode"),
        provider: str | None = Query(None, description="geocoder provider id"),
    ) -> dict:
        geocoder = select_geocoder(request, provider)
        return _geocode_dict(geocoder.geocode(address))

    @app.get("/locate")
    def locate(
        request: Request,
        address: str = Query(..., description="street address to locate"),
        layer: str = Query(..., description="configured layer id"),
        provider: str | None = Query(None, description="geocoder provider id"),
    ) -> dict:
        engine: PolygonLookup = request.app.state.lookup
        # Validate the layer before the external geocode call, so an unknown
        # layer is a fast 404 and we don't spend a geocode on it.
        if layer not in engine.layer_ids:
            raise UnknownLayerError(
                f"unknown layer {layer!r}; configured: {list(engine.layer_ids)}"
            )

        geocoder = select_geocoder(request, provider)
        result = geocoder.geocode(address)
        geocode_body = _geocode_dict(result)
        if not result.matched or result.point is None:
            # Address didn't geocode: return the geocode object with no match.
            return {"query": address, "geocode": geocode_body, "layer": layer}

        longitude, latitude = result.point
        match = engine.locate(longitude, latitude, layer)
        match_body: dict = {"found": match.found}
        if match.found:
            match_body["feature"] = match.feature
        else:
            match_body["reason"] = match.reason
        return {
            "query": address,
            "geocode": geocode_body,
            "layer": layer,
            "match": match_body,
        }

    @app.post(
        "/locate",
        response_model=LocatePointResponse,
        response_model_exclude_none=True,
    )
    def locate_point(body: LocatePointRequest, request: Request) -> LocatePointResponse:
        engine: PolygonLookup = request.app.state.lookup
        match = engine.locate(body.lon, body.lat, body.layer)
        return LocatePointResponse(
            layer=body.layer,
            match=MatchModel(found=match.found, feature=match.feature, reason=match.reason),
        )

    # Serve the static test UI last, so the API routes above take precedence.
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app


app = create_app()
