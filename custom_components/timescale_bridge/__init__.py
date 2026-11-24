"""System-specific TimescaleDB reader for Home Assistant."""

import logging

import asyncpg
import voluptuous as vol
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import config_validation as cv

from .database import Database
from .model import BridgeError
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
    """Set up one YAML-configured bridge; credentials never enter a frontend flow."""
    if DOMAIN not in config:
        return True
    if DOMAIN in hass.data:
        return True
    try:
        db = await Database.open(config[DOMAIN])
    except (BridgeError, TimeoutError, OSError, asyncpg.PostgresError, asyncpg.InterfaceError):
        _LOGGER.error("Timescale Bridge could not connect to the read-only replica")
        return False
    hass.data[DOMAIN] = Bridge(db)
    register_commands(hass)

    async def stop(_event):
        hass.data.pop(DOMAIN, None)
        await db.close()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
    return True
