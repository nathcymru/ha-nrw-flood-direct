"""NRW flood warnings with explicit coverage and YAML-only credentials."""

from dataclasses import dataclass

import probatio as vol
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.reload import async_integration_yaml_config

from .api import Client, ProviderError, StateStore
from .const import DOMAIN, NAME
from .coordinator import FloodCoordinator
from .model import DataError, scope

PLATFORMS = [Platform.SENSOR]


def validate_scope(config):
    try:
        scope(config)
    except DataError as err:
        raise vol.Invalid(str(err)) from None
    return config


CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(DOMAIN): vol.All(
            vol.Schema(
                {
                    vol.Required("api_key"): cv.string,
                    vol.Required("mode"): vol.In(("home", "area_codes", "all_wales")),
                    vol.Optional("area_codes", default=[]): vol.All(cv.ensure_list, [cv.string]),
                    vol.Optional("area_label", default="Selected flood areas"): cv.string,
                }
            ),
            validate_scope,
        )
    },
    extra=vol.ALLOW_EXTRA,
)


@dataclass
class Runtime:
    client: Client
    coordinator: FloodCoordinator


async def async_setup(hass, config):
    if DOMAIN in config:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data={"name": NAME},
            )
        )
    return True


async def async_setup_entry(hass, entry: ConfigEntry):
    yaml = await async_integration_yaml_config(hass, DOMAIN)
    if not yaml or DOMAIN not in yaml:
        raise ConfigEntryError("Configure nrw_flood_direct in YAML with an API !secret")
    config = dict(yaml[DOMAIN])
    key = config.pop("api_key", "")
    if not key:
        raise ConfigEntryError("The NRW API secret is not configured")
    store = StateStore(hass)
    try:
        await store.load()
        client = Client(async_get_clientsession(hass), store, key)
        coordinator = FloodCoordinator(hass, entry, client, config)
    except (ProviderError, DataError) as err:
        raise ConfigEntryError(str(err)) from None
    entry.runtime_data = Runtime(client, coordinator)
    try:
        # Failed data stays visible as unavailable entities and status, not zero.
        await coordinator.async_refresh()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await coordinator.async_shutdown()
        raise
    return True


async def async_unload_entry(hass, entry):
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
