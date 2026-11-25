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


async def test_ui_topology(hass, hass_ws_client, database, sample):
    """Exercise the same config and options flow manager as the HA frontend."""
    domain = "timescale_bridge"
    sample["catalog"].extend(
        {"id": i, "device": f"fixture_{i}", "metric": "energy", "subtype": ""} for i in range(4, 9)
    )
    settings = dict(
        host="replica.invalid",
        database="fixture",
        username="reader",
        password="fixture",
        port=5432,
        sslmode="verify-full",
    )
    with patch("custom_components.timescale_bridge.Database.open", return_value=database):
        flow = await hass.config_entries.flow.async_init(domain, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(flow["flow_id"], settings)
        assert result["type"] == "create_entry"
        entry = result["result"]
        await hass.async_block_till_done()
        options = hass.config_entries.options

        async def choose(action, series):
            menu = await options.async_init(entry.entry_id)
            picker = await options.async_configure(menu["flow_id"], {"next_step_id": action})
            assert picker["step_id"] == action
            return await options.async_configure(menu["flow_id"], {"series": str(series)})

        rows = [
            (1, "Feed A", None),
            (2, "Feed B", None),
            (4, "Plant", 1),
            (5, "Cabinet", 4),
            (6, "Load", 5),
            (7, "Oven", 1),
            (8, "Lighting", 2),
        ]
        for sid, label, parent in rows:
            form = await choose("add", sid)
            saved = await options.async_configure(
                form["flow_id"], {"label": label, "parent": str(parent or "grid")}
            )
            assert saved["type"] == "create_entry"
        client = await hass_ws_client(hass)
        await client.send_json({"id": 1, "type": domain + "/topology"})
        assert (await client.receive_json())["result"] == {
            "api_version": 1,
            "meters": [
                {"series_id": sid, "label": label, "parent_id": parent}
                for sid, label, parent in rows
            ],
        }
        assert (await choose("remove", 1))["reason"] == "has_children"
        form = await choose("edit", 1)
        invalid = await options.async_configure(form["flow_id"], {"label": "Feed", "parent": "6"})
        assert invalid["errors"]["parent"] == "invalid_parent"
        form = await choose("edit", 6)
        saved = await options.async_configure(form["flow_id"], {"label": "Moved", "parent": "2"})
        assert saved["type"] == "create_entry"
        assert (await choose("remove", 6))["type"] == "create_entry"
        assert await hass.config_entries.async_reload(entry.entry_id)
        assert len(entry.options["meters"]) == 6
        await client.close()


async def test_adopt_yaml(hass, database):
    settings = {
        "host": "replica.invalid",
        "database": "fixture",
        "username": "reader",
        "password": "fixture",
    }
    domain = "timescale_bridge"
    with patch("custom_components.timescale_bridge.Database.open", return_value=database) as opened:
        assert await async_setup_component(hass, domain, {domain: settings})
        flow = await hass.config_entries.flow.async_init(domain, context={"source": "user"})
        assert flow["step_id"] == "adopt"
        result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
        assert result["result"].data["host"] == settings["host"]
        await hass.async_block_till_done()
        opened.assert_awaited_once()
        again = await hass.config_entries.flow.async_init(domain, context={"source": "user"})
        assert again["reason"] in {"single_instance_allowed", "already_configured"}


async def test_frontend_http_forms(hass, hass_client, hass_read_only_access_token, database):
    """Submit serialized setup and options forms via HA's authenticated HTTP API."""
    from homeassistant.components.config import config_entries as views

    assert await async_setup_component(hass, "http", {})
    views.async_setup(hass)
    client = await hass_client()
    base = "/api/config/config_entries"

    async def post(path, payload):
        response = await client.post(base + path, json=payload)
        assert response.status == 200, await response.text()
        return await response.json()

    with patch("custom_components.timescale_bridge.Database.open", return_value=database):
        start = await post("/flow", {"handler": "timescale_bridge"})
        await post(
            "/flow/" + start["flow_id"],
            {
                "host": "replica.invalid",
                "port": 5432,
                "database": "fixture",
                "username": "reader",
                "password": "fixture",
                "sslmode": "verify-full",
            },
        )
        await hass.async_block_till_done()
        (entry,) = hass.config_entries.async_entries("timescale_bridge")
        denied = await client.post(
            base + "/options/flow",
            json={"handler": entry.entry_id},
            headers={"Authorization": f"Bearer {hass_read_only_access_token}"},
        )
        assert denied.status in (401, 403)
        menu = await post("/options/flow", {"handler": entry.entry_id})
        path = "/options/flow/" + menu["flow_id"]
        picker = await post(path, {"next_step_id": "add"})
        assert picker["step_id"] == "add"
        assert "meter_a" in str(picker["data_schema"])
        assert (await post(path, {"series": "1"}))["step_id"] == "meter"
        missing = await client.post(base + path, json={"label": "Load", "parent": "999"})
        assert missing.status == 400
        assert (await post(path, {"label": "Feed A", "parent": "grid"}))["type"] == "create_entry"
        menu = await post("/options/flow", {"handler": entry.entry_id})
        path = "/options/flow/" + menu["flow_id"]
        await post(path, {"next_step_id": "add"})
        for unavailable in ("1", "3"):  # Already assigned and non-energy, respectively.
            rejected = await client.post(base + path, json={"series": unavailable})
            assert rejected.status == 400
        flow = await hass.config_entries.flow.async_init(
            "timescale_bridge", context={"source": "reconfigure", "entry_id": entry.entry_id}
        )
        updated = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {**entry.data, "password": "replacement-fixture"}
        )
        assert updated["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()
        assert entry.options["meters"][0]["label"] == "Feed A"
