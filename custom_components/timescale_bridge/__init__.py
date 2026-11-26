"""System-specific TimescaleDB reader for Home Assistant."""

import logging

import asyncpg
import voluptuous as vol
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .database import Database
from .model import BridgeError
from .panel import async_add_panel, remove_panel
from .service import Bridge
from .websocket import register_commands

DOMAIN = "timescale_bridge"
_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required("host"): vol.All(cv.string, vol.Match(r"^[A-Za-z0-9._:-]+$")),
                vol.Optional("port", default=5432): cv.port,
                vol.Required("database"): cv.string,
                vol.Required("username"): cv.string,
                vol.Required("password"): cv.string,
                vol.Optional("sslmode", default="verify-full"): vol.In(["verify-full", "disable"]),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass, config):
    """Register the read API and start any legacy YAML connection."""
    if not hass.data.get("timescale_bridge_ws_registered"):
        register_commands(hass)
        hass.data["timescale_bridge_ws_registered"] = True
    if DOMAIN not in config:
        return True
    hass.data["timescale_bridge_yaml"] = config[DOMAIN]
    if hass.config_entries and hass.config_entries.async_entries(DOMAIN):
        return True
    try:
        await _start(hass, config[DOMAIN])
    except (BridgeError, TimeoutError, OSError, asyncpg.PostgresError, asyncpg.InterfaceError):
        _LOGGER.error("Timescale Bridge could not connect to the read-only replica")
        return False
    return True


async def _start(hass, settings):
    if DOMAIN in hass.data:
        return
    db = await Database.open(settings)
    try:
        await async_add_panel(hass)
    except BaseException:
        await db.close()
        raise
    hass.data[DOMAIN] = Bridge(db)

    async def stop(_event):
        await _close(hass)

    hass.data["timescale_bridge_stop"] = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)


async def _close(hass):
    remove_panel(hass)
    unsubscribe = hass.data.pop("timescale_bridge_stop", None)
    if unsubscribe:
        unsubscribe()
    bridge = hass.data.pop(DOMAIN, None)
    if bridge:
        await bridge.database.close()


async def async_setup_entry(hass, entry):
    try:
        await _start(hass, entry.data)
    except (BridgeError, TimeoutError, OSError, asyncpg.PostgresError, asyncpg.InterfaceError):
        raise ConfigEntryNotReady("Replica unavailable") from None
    return True


async def async_unload_entry(hass, entry):
    await _close(hass)
    return True
