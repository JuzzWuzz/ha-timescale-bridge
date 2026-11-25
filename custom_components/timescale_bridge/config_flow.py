"""Administrator setup and meter topology editor."""

import asyncpg
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import CONFIG_SCHEMA, DOMAIN
from .database import Database
from .model import BridgeError

CONNECTION_ERRORS = (
    BridgeError,
    OSError,
    TimeoutError,
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
)


class SetupFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return MeterOptions()

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        yaml = self.hass.data.get("timescale_bridge_yaml")
        if yaml:
            return await self.async_step_adopt()
        return await self._connection_form("user", user_input)

    async def async_step_reconfigure(self, user_input=None):
        return await self._connection_form("reconfigure", user_input)

    async def _connection_form(self, step, user_input):
        entry = self._get_reconfigure_entry() if step == "reconfigure" else None
        defaults = entry.data if entry else {}
        error = {}
        if user_input is not None:
            try:
                settings = CONFIG_SCHEMA({DOMAIN: user_input})[DOMAIN]
                probe = await Database.open(settings)
                await probe.close()
            except vol.Invalid:
                error["base"] = "invalid_settings"
            except CONNECTION_ERRORS:
                error["base"] = "cannot_connect"
            else:
                if entry:
                    return self.async_update_reload_and_abort(entry, data=settings)
                return self.async_create_entry(title="Timescale Bridge", data=settings)
        return self.async_show_form(
            step_id=step,
            errors=error,
            data_schema=vol.Schema(
                {
                    vol.Required("host", default=defaults.get("host", "")): str,
                    vol.Required("port", default=defaults.get("port", 5432)): int,
                    vol.Required("database", default=defaults.get("database", "")): str,
                    vol.Required("username", default=defaults.get("username", "")): str,
                    vol.Required("password"): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                    vol.Required("sslmode", default=defaults.get("sslmode", "verify-full")): vol.In(
                        ["verify-full", "disable"]
                    ),
                }
            ),
        )

    async def async_step_adopt(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(
                title="Timescale Bridge", data=dict(self.hass.data["timescale_bridge_yaml"])
            )
        return self.async_show_form(step_id="adopt", data_schema=vol.Schema({}))


class MeterOptions(config_entries.OptionsFlow):
    """One atomic edit per options flow. IDs are catalog series IDs."""

    cursor = 0
    selected = None

    @property
    def meters(self):
        return self.config_entry.options.get("meters", [])

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(step_id="init", menu_options=["add", "edit", "remove"])

    async def async_step_add(self, user_input=None):
        bridge = self.hass.data.get(DOMAIN)
        if not bridge:
            return self.async_abort(reason="cannot_connect")
        try:
            page = await bridge.request(
                {"type": DOMAIN + "/catalog", "after_id": self.cursor, "limit": 200}
            )
        except BridgeError:
            return self.async_abort(reason="cannot_connect")
        assigned = {node["series_id"] for node in self.meters}
        choices = {
            str(r["id"]): f"{r['device']} / {r['metric']} / {r['subtype']} (#{r['id']})"
            for r in page["series"]
            if r["metric"] == "energy" and r["id"] not in assigned
        }
        if page["next_after_id"] is not None:
            choices["next"] = "Next catalog page"
        if user_input:
            value = user_input["series"]
            if value not in choices:
                return self.async_abort(reason="stale")
            if value == "next":
                self.cursor = page["next_after_id"]
                return await self.async_step_add()
            self.selected = int(value)
            self.original = None
            return await self.async_step_meter()
        if not choices:
            return self.async_abort(reason="no_series")
        return self.async_show_form(
            step_id="add", data_schema=vol.Schema({vol.Required("series"): vol.In(choices)})
        )

    async def async_step_edit(self, user_input=None):
        return await self._choose("edit", user_input)

    async def async_step_remove(self, user_input=None):
        return await self._choose("remove", user_input)

    async def _choose(self, step, user_input):
        choices = {str(n["series_id"]): f"{n['label']} (#{n['series_id']})" for n in self.meters}
        if not choices:
            return self.async_abort(reason="no_meters")
        if user_input:
            self.selected = int(user_input["series"])
            self.original = next((n for n in self.meters if n["series_id"] == self.selected), None)
            if not self.original:
                return self.async_abort(reason="stale")
            if step == "edit":
                return await self.async_step_meter()
            if any(n["parent_id"] == self.selected for n in self.meters):
                return self.async_abort(reason="has_children")
            return self._save([n for n in self.meters if n["series_id"] != self.selected])
        return self.async_show_form(
            step_id=step, data_schema=vol.Schema({vol.Required("series"): vol.In(choices)})
        )

    def _save(self, nodes):
        return self.async_create_entry(
            title="", data={**self.config_entry.options, "meters": nodes}
        )

    async def async_step_meter(self, user_input=None):
        old = self.original or {}
        nodes = {n["series_id"]: n for n in self.meters}
        parents = {"grid": "Independent grid feed"}
        parents.update(
            {str(i): f"{n['label']} (#{i})" for i, n in nodes.items() if i != self.selected}
        )
        errors = {}
        if user_input:
            if nodes.get(self.selected) != self.original:
                return self.async_abort(reason="stale")
            label = user_input["label"].strip()
            value = user_input["parent"]
            if not label:
                errors["label"] = "empty_label"
            elif value not in parents:
                errors["parent"] = "invalid_parent"
            else:
                parent = None if value == "grid" else int(value)
                ancestor = parent
                visited = {self.selected}
                while ancestor is not None and ancestor not in visited:
                    visited.add(ancestor)
                    ancestor = nodes[ancestor]["parent_id"]
                if ancestor is None:
                    nodes[self.selected] = {
                        "series_id": self.selected,
                        "label": label,
                        "parent_id": parent,
                    }
                    return self._save(list(nodes.values()))
                errors["parent"] = "invalid_parent"
        return self.async_show_form(
            step_id="meter",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("label", default=old.get("label", "")): str,
                    vol.Required("parent", default=str(old.get("parent_id") or "grid")): vol.In(
                        parents
                    ),
                }
            ),
        )
