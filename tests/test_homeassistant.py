import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.core import HomeAssistant

from custom_components.timescale_bridge import CONFIG_SCHEMA, async_setup
from custom_components.timescale_bridge.websocket import _handle, register_commands


@pytest.fixture
async def hass(tmp_path):
    instance = HomeAssistant(str(tmp_path))
    yield instance
    await instance.async_stop(force=True)


async def dispatch(hass, message, *, active=True):
    future = asyncio.get_running_loop().create_future()

    def received(data):
        if not future.done():
            future.set_result(json.loads(data) if isinstance(data, (str, bytes)) else data)

    # HA 2026.11 adds a required remote argument to this internal dispatcher.
    kwargs = (
        {"remote": "127.0.0.1"}
        if "remote" in ActiveConnection.__init__.__code__.co_varnames
        else {}
    )
    connection = ActiveConnection(
        MagicMock(),
        hass,
        received,
        SimpleNamespace(is_active=active, name="fixture"),
        SimpleNamespace(id="fixture-token"),
        **kwargs,
    )
    connection.async_handle(message)
    return await asyncio.wait_for(future, 2)


async def test_real_ha_websocket_dispatch(hass, bridge, message):
    hass.data["timescale_bridge"] = bridge
    register_commands(hass)
    result = await dispatch(hass, message)
    assert result["success"] is True
    assert result["result"]["series"][1]["points"][0][1] == 0
    catalog = await dispatch(hass, {"id": 2, "type": "timescale_bridge/catalog", "limit": 1})
    assert catalog["result"]["next_after_id"] == 1


async def test_unknown_fields_rejected_by_real_ha_schema(hass, bridge, message):
    hass.data["timescale_bridge"] = bridge
    register_commands(hass)
    result = await dispatch(hass, {**message, "sql": "SELECT 1"})
    assert result["success"] is False
    assert result["error"]["code"] == "invalid_format"


async def test_inactive_and_missing_users_cannot_query(hass, bridge, message, database):
    hass.data["timescale_bridge"] = bridge
    register_commands(hass)
    result = await dispatch(hass, message, active=False)
    assert result["error"]["code"] == "unauthorized"
    connection = MagicMock(user=None)
    await _handle(hass, connection, message)
    connection.send_error.assert_called_once_with(
        1, "unauthorized", "An active HA user is required."
    )
    database.lookup.assert_not_awaited()


async def test_not_ready_and_safe_database_error(hass, bridge, message, database):
    register_commands(hass)
    result = await dispatch(hass, message)
    assert result["error"]["code"] == "not_ready"
    from custom_components.timescale_bridge.model import BridgeError

    hass.data["timescale_bridge"] = bridge
    database.lookup.side_effect = BridgeError("replica_unavailable", "Replica read failed.")
    result = await dispatch(hass, message)
    assert result["error"]["code"] == "replica_unavailable"


async def test_setup_and_stop_close_pool(hass, database):
    config = CONFIG_SCHEMA(
        {
            "timescale_bridge": {
                "host": "replica.example",
                "database": "fixture",
                "username": "reader",
                "password": "fixture-only",
            }
        }
    )
    with patch(
        "custom_components.timescale_bridge.Database.open", AsyncMock(return_value=database)
    ):
        assert await async_setup(hass, config)
    assert "timescale_bridge" in hass.data
    await hass.async_stop(force=True)
    database.close.assert_awaited_once()
    assert "timescale_bridge" not in hass.data


@pytest.mark.parametrize("host", ["primary,replica", "", "/tmp", "postgres://host", "host name"])
def test_single_target_config(host):
    import voluptuous as vol

    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA(
            {
                "timescale_bridge": {
                    "host": host,
                    "database": "fixture",
                    "username": "reader",
                    "password": "fixture-only",
                }
            }
        )


async def test_startup_failure_does_not_publish_bridge(hass):
    from custom_components.timescale_bridge.model import BridgeError

    config = CONFIG_SCHEMA(
        {
            "timescale_bridge": {
                "host": "replica.example",
                "database": "fixture",
                "username": "reader",
                "password": "fixture-only",
            }
        }
    )
    with patch(
        "custom_components.timescale_bridge.Database.open",
        AsyncMock(side_effect=BridgeError("replica_required", "The target is not a replica.")),
    ):
        assert not await async_setup(hass, config)
    assert "timescale_bridge" not in hass.data
