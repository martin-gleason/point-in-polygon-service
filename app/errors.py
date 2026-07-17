"""The SPEC §4 error model and its handlers.

Every error response is `{"error": {"code": "<slug>", "message": "<human>"}}`.
The domain exceptions the engine raises are mapped to the status codes §4
mandates: 400 for bad input, 404 for an unknown layer. (502 for an upstream
geocoder failure arrives with F3.)
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.geocoding.base import GeocoderUnavailable
from app.geocoding.registry import UnknownProviderError
from app.lookup import InvalidCoordinateError, UnknownLayerError


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


async def _unknown_layer(request: Request, exc: UnknownLayerError) -> JSONResponse:
    return error_response(404, "unknown_layer", str(exc))


async def _invalid_coordinate(
    request: Request, exc: InvalidCoordinateError
) -> JSONResponse:
    return error_response(400, "invalid_coordinate", str(exc))


async def _geocoder_unavailable(
    request: Request, exc: GeocoderUnavailable
) -> JSONResponse:
    # 502: an upstream geocoder failed (SPEC §4). The exception message is
    # already sanitized of any address/token by the adapter (§9).
    return error_response(502, "geocoder_unavailable", str(exc))


async def _unknown_provider(request: Request, exc: UnknownProviderError) -> JSONResponse:
    return error_response(400, "unknown_provider", str(exc))


async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
    # SPEC §4 wants 400 for invalid params; FastAPI's default for validation is
    # 422, so we remap and flatten the detail into one human-readable message.
    problems = "; ".join(
        f"{'.'.join(str(p) for p in err['loc'][1:]) or 'body'}: {err['msg']}"
        for err in exc.errors()
    )
    return error_response(400, "invalid_request", problems or "invalid request")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(UnknownLayerError, _unknown_layer)
    app.add_exception_handler(InvalidCoordinateError, _invalid_coordinate)
    app.add_exception_handler(GeocoderUnavailable, _geocoder_unavailable)
    app.add_exception_handler(UnknownProviderError, _unknown_provider)
    app.add_exception_handler(RequestValidationError, _validation)
