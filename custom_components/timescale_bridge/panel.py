"""Serve the integration's local frontend and manage its sidebar registration."""

import json
import logging
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig

PATH = "timescale-energy"
_ASSETS = "timescale_bridge_panel_assets"
_OWNER = "timescale_bridge_panel_owner"
_ROOT = Path(__file__).parent
_LOGGER = logging.getLogger(__name__)


def _asset_path():
    version = json.loads((_ROOT / "manifest.json").read_text())["version"]
    return f"/timescale_bridge_static/{version}"


async def async_add_panel(hass):
    """Register static routes once per server and a panel once per active bridge."""
    if frontend.async_panel_exists(hass, PATH):
        if hass.data.get(_OWNER) is not hass.data[frontend.DATA_PANELS][PATH]:
            _LOGGER.warning(
                "Sidebar path /%s is already in use. Bridge queries remain available", PATH
            )
        return
    if _ASSETS not in hass.data:
        url = await hass.async_add_executor_job(_asset_path)
        await hass.http.async_register_static_paths(
            [StaticPathConfig(url, str(_ROOT / "frontend"), True)]
        )
        hass.data[_ASSETS] = url
    try:
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=PATH,
            webcomponent_name="timescale-energy-panel",
            sidebar_title="Timescale Energy",
            sidebar_icon="mdi:lightning-bolt",
            module_url=f"{hass.data[_ASSETS]}/timescale-energy-panel.js",
        )
    except ValueError:
        if not frontend.async_panel_exists(hass, PATH):
            raise
        _LOGGER.warning("Sidebar path /%s was registered by another panel", PATH)
        return
    hass.data[_OWNER] = hass.data[frontend.DATA_PANELS][PATH]


def remove_panel(hass):
    """Only remove the panel object this bridge registered."""
    owner = hass.data.pop(_OWNER, None)
    if owner is not None and hass.data.get(frontend.DATA_PANELS, {}).get(PATH) is owner:
        frontend.async_remove_panel(hass, PATH)
