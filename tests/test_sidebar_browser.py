"""Exercise the installed HA frontend with a loopback server and synthetic meters."""

import json
import shutil
import time
from unittest.mock import patch

import pytest

ha_test = pytest.importorskip("pytest_homeassistant.common")
playwright = pytest.importorskip("playwright.async_api")


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir):
    return hass_tmp_config_dir


@pytest.mark.skipif(not shutil.which("chromium"), reason="requires Chromium")
async def test_electricity_sidebar(hass, hass_client, hass_access_token, database, sample):
    domain = "timescale_bridge"
    meters = [
        {"series_id": 2, "label": "Grid B", "parent_id": None},
        {"series_id": 4, "label": "Branch", "parent_id": 1},
        {"series_id": 5, "label": "Leaf", "parent_id": 4},
        {"series_id": 1, "label": "Grid A", "parent_id": None},
    ]
    sample["catalog"] = [
        dict(id=m["series_id"], device=f"fixture_{i}", metric="energy", subtype="")
        for i, m in enumerate(meters)
    ]
    values = {1: 100, 2: 50, 4: 60, 5: 15}
    missing = False

    async def points(query, series):
        return [
            dict(bucket_start=t, value=values[series["id"]], count=1)
            for i, t in enumerate(query.buckets())
            if not (missing and i == 5 and series["id"] == 1)
        ]

    database.points.side_effect = points
    entry = ha_test.MockConfigEntry(domain=domain, data={}, options={"meters": meters})
    entry.add_to_hass(hass)
    with (
        patch("custom_components.timescale_bridge.Database.open", return_value=database),
        patch("homeassistant.components.onboarding.async_is_onboarded", return_value=True),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        client = await hass_client()
        origin = str(client.make_url("/")).rstrip("/")
        # The isolated HA fixture issues a fresh session for this browser only.
        session = dict(
            access_token=hass_access_token,
            expires=time.time() * 1000 + 300000,
            hassUrl=origin,
            clientId=origin + "/",
            token_type="Bearer",
        )
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(executable_path=shutil.which("chromium"))
            page = await browser.new_page(timezone_id="America/Los_Angeles")
            await page.add_init_script(
                "localStorage.setItem('hassTokens', " + json.dumps(json.dumps(session)) + ")"
            )
            received = []
            page.on("response", lambda response: received.append((response.url, response.status)))
            await page.goto(origin + "/config", wait_until="domcontentloaded")
            await page.locator('ha-sidebar a[href="/timescale-energy"]').click(timeout=12000)
            await playwright.expect(page).to_have_url(origin + "/timescale-energy")
            panel = page.locator("timescale-energy-panel")
            await playwright.expect(panel.locator(".headline")).to_have_text(
                "7.2 kWh", timeout=25000
            )
            assert (
                origin + "/timescale_bridge_static/0.3.0-beta.1/timescale-energy-panel.js",
                200,
            ) in received
            assert await panel.locator("svg rect").count() == 48
            await playwright.expect(
                panel.get_by_text("Untracked at grid feeds: 4.32 kWh")
            ).to_be_visible()
            await playwright.expect(
                panel.get_by_role("row", name="Branch Under Grid A 2.88 kWh 2.16 kWh")
            ).to_be_visible()
            await panel.get_by_label("Date", exact=True).fill("2026-04-01")
            await panel.get_by_label("Period", exact=True).select_option("month")
            await playwright.expect(panel.locator(".headline")).to_have_text("216 kWh")
            assert await panel.locator("svg rect").count() == 60
            await panel.get_by_role("button", name="Previous period").click()
            await playwright.expect(panel.get_by_label("Date", exact=True)).to_have_value(
                "2026-03-01"
            )
            await playwright.expect(panel.locator(".headline")).to_have_text("223.2 kWh")
            await panel.get_by_label("Period", exact=True).select_option("day")
            await playwright.expect(panel.locator(".headline")).to_have_text("7.2 kWh")
            await panel.get_by_label("Period", exact=True).select_option("week")
            await playwright.expect(panel.locator(".headline")).to_have_text("50.4 kWh")
            missing = True
            await panel.get_by_role("button", name="Refresh").click()
            await playwright.expect(panel.locator(".headline")).to_have_text("Incomplete")
            await page.set_viewport_size({"width": 390, "height": 820})
            await playwright.expect(panel.locator("ha-menu-button")).to_be_visible()
            assert await panel.evaluate("(p) => p.scrollWidth <= p.clientWidth")
            database.lookup.side_effect = TimeoutError()
            await panel.get_by_role("button", name="Refresh").click()
            await playwright.expect(
                panel.get_by_text("Electricity usage unavailable")
            ).to_be_visible()
            hass.config_entries.async_update_entry(entry, options={"meters": []})
            await panel.get_by_role("button", name="Refresh").click()
            await playwright.expect(
                panel.get_by_text("No electricity meters configured")
            ).to_be_visible()
            await browser.close()
