"""F4 — build geocoder providers from configuration.

Maps each [[geocoders]] entry to a concrete `Geocoder` by dispatching on its
`type`. This is the seam the fallback chain (F5) and future provider types
(Census, Nominatim, offline, the optional arcpy plugin) plug into — a new
provider is a new builder here, not a change to the service.
"""
from __future__ import annotations

from app.config import AppConfig, ConfigError, GeocoderConfig
from app.geocoding.arcgis import ArcGISRestGeocoder
from app.geocoding.base import Geocoder

# type slug -> builder(entry_dict) -> Geocoder
_BUILDERS = {
    "arcgis_rest": ArcGISRestGeocoder.from_config,
}


class UnknownProviderError(Exception):
    """A request named a provider that is not configured (→ HTTP 400 at F4)."""


def build_geocoder(config: GeocoderConfig) -> Geocoder:
    builder = _BUILDERS.get(config.type)
    if builder is None:
        raise ConfigError(
            f"unknown geocoder type {config.type!r} for provider {config.id!r}; "
            f"known types: {sorted(_BUILDERS)}"
        )
    return builder(config.options)


def build_geocoders(app_config: AppConfig) -> dict[str, Geocoder]:
    """Build every configured provider, keyed by id. Fails fast (ConfigError) at
    startup on an unknown provider type."""
    return {
        geocoder_id: build_geocoder(geocoder_config)
        for geocoder_id, geocoder_config in app_config.geocoders.items()
    }
