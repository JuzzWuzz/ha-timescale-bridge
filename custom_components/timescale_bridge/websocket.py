"""Authenticated HA WebSocket commands; no SQL or credentials in messages."""

import voluptuous as vol
from homeassistant.components import websocket_api

from .model import BridgeError


async def _handle(hass, connection, message):
    user = connection.user
    if user is None or not user.is_active:
        connection.send_error(message["id"], "unauthorized", "An active HA user is required.")
        return
    if message["type"] == "timescale_bridge/topology":
        entries = hass.config_entries.async_entries("timescale_bridge")
        nodes = entries[0].options.get("meters", []) if entries else []
        connection.send_result(message["id"], {"api_version": 1, "meters": nodes})
        return
    bridge = hass.data.get("timescale_bridge")
    if bridge is None:
        connection.send_error(message["id"], "not_ready", "Timescale Bridge is unavailable.")
        return
    try:
        result = await bridge.request(message)
    except BridgeError as err:
        connection.send_error(message["id"], err.code, str(err))
    else:
        connection.send_result(message["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "timescale_bridge/catalog",
        vol.Optional("after_id"): int,
        vol.Optional("limit"): int,
    }
)
@websocket_api.async_response
async def catalog(hass, connection, message):
    await _handle(hass, connection, message)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "timescale_bridge/query",
        vol.Required("series_ids"): list,
        vol.Required("start"): str,
        vol.Required("end"): str,
        vol.Required("preset"): str,
        vol.Optional("resolution"): str,
    }
)
@websocket_api.async_response
async def query(hass, connection, message):
    await _handle(hass, connection, message)


@websocket_api.websocket_command({vol.Required("type"): "timescale_bridge/topology"})
@websocket_api.async_response
async def topology(hass, connection, message):
    await _handle(hass, connection, message)


def register_commands(hass):
    websocket_api.async_register_command(hass, topology)
    websocket_api.async_register_command(hass, catalog)
    websocket_api.async_register_command(hass, query)
