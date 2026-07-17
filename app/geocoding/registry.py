"""F4/F5 — build geocoder providers from configuration.

Maps each ``[[geocoders]]`` entry to a concrete ``Geocoder`` by dispatching on
its ``type``. This is the seam every provider plugs into — a new provider is a
new builder here, not a change to the service:

- ``arcgis_rest`` — public or private ArcGIS ``GeocodeServer`` (F3, §5 modes 1–2)
- ``census``      — US Census onelineaddress, free/no key (F5, §5 mode 1)
- ``nominatim``   — OSM Nominatim, opt-in / self-hostable (F5, §5 mode 2)
- ``local_points``— fully-offline address-point match (F5, §5 mode 3)
- ``chain``       — an ordered fallback across the above (F5, D7)

A ``chain`` is itself a ``Geocoder`` but references *other* providers by id, so
it is built in a second pass after the leaf providers exist (``build_geocoders``).

ArcGIS / ArcPy equivalent
    A composite ``.loc`` locator lists child locators and a priority order; this
    registry is that composite-plus-catalog expressed as config — leaf locators
    are the ``arcgis_rest``/``census``/… entries, and a composite is a ``chain``.
"""
from __future__ import annotations

from app.config import AppConfig, ConfigError, GeocoderConfig
from app.geocoding.arcgis import ArcGISRestGeocoder
from app.geocoding.base import Geocoder
from app.geocoding.census import CensusGeocoder
from app.geocoding.chain import GeocoderChain
from app.geocoding.local_points import LocalAddressPointGeocoder
from app.geocoding.nominatim import NominatimGeocoder

CHAIN_TYPE = "chain"

# type slug -> builder(entry_dict) -> Geocoder. Leaf providers only; a chain is
# built separately (build_geocoders) because it references other providers.
_BUILDERS = {
    "arcgis_rest": ArcGISRestGeocoder.from_config,
    "census": CensusGeocoder.from_config,
    "nominatim": NominatimGeocoder.from_config,
    "local_points": LocalAddressPointGeocoder.from_config,
}


class UnknownProviderError(Exception):
    """A request named a provider that is not configured (→ HTTP 400 at F4)."""


def build_geocoder(config: GeocoderConfig) -> Geocoder:
    """Build one leaf provider. A builder's own validation failure (a missing
    user_agent, an absent offline GeoPackage) is re-raised as ConfigError so the
    service fails fast at startup with one consistent diagnostic (D1)."""
    builder = _BUILDERS.get(config.type)
    if builder is None:
        known = sorted([*_BUILDERS, CHAIN_TYPE])
        raise ConfigError(
            f"unknown geocoder type {config.type!r} for provider {config.id!r}; "
            f"known types: {known}"
        )
    try:
        return builder(config.options)
    except ConfigError:
        raise
    except (ValueError, KeyError) as error:
        raise ConfigError(f"geocoder {config.id!r}: {error}") from error


def build_geocoders(app_config: AppConfig) -> dict[str, Geocoder]:
    """Build every configured provider, keyed by id. Two passes: leaf providers
    first, then chains that reference them by id — so a ``[[geocoders]]`` chain
    may appear before its members in the file. Fails fast (ConfigError) at
    startup on an unknown type or an unresolved chain member."""
    leaves: dict[str, Geocoder] = {}
    chains: list[GeocoderConfig] = []
    for geocoder_id, geocoder_config in app_config.geocoders.items():
        if geocoder_config.type == CHAIN_TYPE:
            chains.append(geocoder_config)
        else:
            leaves[geocoder_id] = build_geocoder(geocoder_config)

    geocoders: dict[str, Geocoder] = dict(leaves)
    for chain_config in chains:
        try:
            # Chains resolve against the leaf providers (and any chain already
            # built this pass). A chain referencing another chain works only if
            # that chain is declared earlier in the file.
            geocoders[chain_config.id] = GeocoderChain.from_config(
                chain_config.options, geocoders
            )
        except (ValueError, KeyError) as error:
            raise ConfigError(f"geocoder {chain_config.id!r}: {error}") from error
    return geocoders
