"""Release-matched HTTP/WebSocket checks using official HA fixtures.

The legacy 2025 environment skips this module. All database calls are mocked;
the official plugin blocks external sockets and DNS, permitting only loopback.
"""

from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant")

from homeassistant.setup import async_setup_component


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir):
    return hass_tmp_config_dir


async def test_setup_catalog_query_and_shutdown(hass, hass_ws_client, database, message):
    """Authenticate over real HA test WS, query, then exercise YAML lifecycle cleanup."""
    config = {
        "timescale_bridge": {
            "host": "replica.example.invalid",
            "database": "fixture",
            "username": "fixture-reader",
            "password": "fixture-only",
        }
    }
    with (
        patch("asyncpg.create_pool", AsyncMock(side_effect=AssertionError("No real database"))),
        patch(
            "custom_components.timescale_bridge.Database.open", AsyncMock(return_value=database)
        ) as open_database,
    ):
        assert await async_setup_component(hass, "timescale_bridge", config)
        open_database.assert_awaited_once()
        client = await hass_ws_client(hass)
        await client.send_json({"id": 1, "type": "timescale_bridge/catalog", "limit": 2})
        catalog = await client.receive_json()
        assert catalog["success"] is True
        assert [row["device"] for row in catalog["result"]["series"]] == ["meter_a", "meter_b"]
        await client.send_json({**message, "id": 2})
        result = await client.receive_json()
        assert result["success"] is True
        assert result["result"]["series"][1]["points"][0][1] == 0
        assert result["result"]["series"][0]["points"][2][1] is None
        assert result["result"]["completeness"] == "unknown"
        await client.send_json({**message, "id": 3, "sql": "SELECT 1"})
        rejected = await client.receive_json()
        assert rejected["error"]["code"] == "invalid_format"
        await client.close()
        # YAML integration has no config-entry unload/reload; HA stop is its cleanup path.
        await hass.async_stop(force=True)
        database.close.assert_awaited_once()
        assert "timescale_bridge" not in hass.data
