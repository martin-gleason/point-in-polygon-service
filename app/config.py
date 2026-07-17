"""F2-T1 — startup configuration: the polygon layers, loaded from config.toml.

The service is layer-agnostic (SPEC §1): which polygon sets it serves is
configuration, not code. This module parses that configuration and fails fast at
startup — a missing config file, a missing GeoPackage, or a malformed layer
entry raises `ConfigError` before the service accepts a single request, rather
than surfacing as a 500 on the first query.

ArcGIS / ArcPy equivalent
    Where an ArcPy workflow hardcodes feature-class paths inside a script or a
    fixed .aprx project, here the layer set is declared in an external TOML file
    the operator edits — the same role an ArcGIS Server service definition plays,
    minus the server.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"

REQUIRED_LAYER_KEYS = ("id", "name", "path", "layer", "attributes", "source")


class ConfigError(Exception):
    """Configuration is missing, malformed, or points at data that isn't there."""


@dataclass(frozen=True)
class LayerConfig:
    """One configured polygon layer.

    path is an absolute, existence-checked path to the GeoPackage; layer is the
    layer name within it; attributes are the columns the API returns for a hit.
    """

    id: str
    name: str
    path: Path
    layer: str
    attributes: tuple[str, ...]
    source: str


@dataclass(frozen=True)
class GeocoderConfig:
    """One configured geocoder provider (a [[geocoders]] entry).

    `options` is the whole entry (id, type, base_url, token_env, timeout, …); the
    registry builds the concrete adapter from it by dispatching on `type`.
    """

    id: str
    type: str
    options: dict


@dataclass(frozen=True)
class AppConfig:
    layers: dict[str, LayerConfig]
    geocoders: dict[str, GeocoderConfig] = field(default_factory=dict)
    # The provider used when a request names none — the first configured, or None
    # if the deployment runs layers-only (POST /locate needs no geocoder).
    default_geocoder: str | None = None


def default_config_path() -> Path:
    """The config file to load: $PIP_CONFIG if set, else ./config.toml."""
    override = os.environ.get("PIP_CONFIG")
    return Path(override) if override else DEFAULT_CONFIG_PATH


def load_config(config_path: Path | None = None) -> AppConfig:
    """Parse and validate the TOML config, resolving and checking every path."""
    path = Path(config_path) if config_path is not None else default_config_path()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    with open(path, "rb") as handle:
        raw = tomllib.load(handle)

    raw_layers = raw.get("layers")
    if not raw_layers:
        raise ConfigError(f"{path}: no [[layers]] configured")
    if not isinstance(raw_layers, list):
        raise ConfigError(
            f"{path}: 'layers' must be an array of tables ([[layers]]), "
            f"not {type(raw_layers).__name__}"
        )

    base_dir = path.resolve().parent
    layers: dict[str, LayerConfig] = {}
    for entry in raw_layers:
        missing = [key for key in REQUIRED_LAYER_KEYS if key not in entry]
        if missing:
            raise ConfigError(
                f"{path}: layer entry {entry.get('id', '<no id>')!r} is missing "
                f"required keys {missing}"
            )

        attributes = entry["attributes"]
        if not isinstance(attributes, list) or not attributes:
            raise ConfigError(
                f"{path}: layer {entry['id']!r} attributes must be a non-empty list"
            )

        layer_path = Path(entry["path"])
        if not layer_path.is_absolute():
            layer_path = (base_dir / layer_path).resolve()
        if not layer_path.is_file():
            raise ConfigError(
                f"{path}: layer {entry['id']!r} GeoPackage not found at {layer_path} "
                f"(run scripts/build_data.py to build it)"
            )

        layer_id = entry["id"]
        if layer_id in layers:
            raise ConfigError(f"{path}: duplicate layer id {layer_id!r}")

        layers[layer_id] = LayerConfig(
            id=layer_id,
            name=entry["name"],
            path=layer_path,
            layer=entry["layer"],
            attributes=tuple(attributes),
            source=entry["source"],
        )

    geocoders, default_geocoder = _parse_geocoders(raw, path)
    return AppConfig(
        layers=layers, geocoders=geocoders, default_geocoder=default_geocoder
    )


def _parse_geocoders(
    raw: dict, path: Path
) -> tuple[dict[str, GeocoderConfig], str | None]:
    """Parse the optional [[geocoders]] array. Absent is fine — a deployment may
    run layers-only (POST /locate needs no geocoder)."""
    raw_geocoders = raw.get("geocoders", [])
    if not isinstance(raw_geocoders, list):
        raise ConfigError(
            f"{path}: 'geocoders' must be an array of tables ([[geocoders]]), "
            f"not {type(raw_geocoders).__name__}"
        )

    geocoders: dict[str, GeocoderConfig] = {}
    default_geocoder: str | None = None
    for entry in raw_geocoders:
        for key in ("id", "type"):
            if key not in entry:
                raise ConfigError(
                    f"{path}: geocoder entry {entry.get('id', '<no id>')!r} "
                    f"is missing required key {key!r}"
                )
        geocoder_id = entry["id"]
        if geocoder_id in geocoders:
            raise ConfigError(f"{path}: duplicate geocoder id {geocoder_id!r}")
        geocoders[geocoder_id] = GeocoderConfig(
            id=geocoder_id, type=entry["type"], options=dict(entry)
        )
        if default_geocoder is None:
            default_geocoder = geocoder_id

    return geocoders, default_geocoder
