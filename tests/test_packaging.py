"""Check the installable layout with target-version Marketplace validators."""

import json
from pathlib import Path

import pytest

pytest.importorskip("pytest_homeassistant")

from homeassistant.components.marketplace.repositories.integration import _check_loadable_manifest
from homeassistant.components.marketplace.utils.validate import REPOSITORY_MANIFEST_VALUES

ROOT = Path(__file__).resolve().parents[1]


def test_marketplace_component_layout_and_metadata():
    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert hacs["name"] == "Timescale Bridge"
    for key, value in hacs.items():
        assert REPOSITORY_MANIFEST_VALUES[key](value) == value
    assert not hacs.get("content_in_root", False)
    assert not hacs.get("zip_release", False)
    components = ROOT / "custom_components"
    domains = [
        path.name for path in components.iterdir() if path.is_dir() and path.name != "__pycache__"
    ]
    assert domains == ["timescale_bridge"]
    component = components / "timescale_bridge"
    _check_loadable_manifest(component, "timescale_bridge")
    manifest = json.loads((component / "manifest.json").read_text())
    assert manifest["version"] == "0.3.0-beta.1"
    assert manifest["requirements"] == ["asyncpg==0.31.0"]
    assert (component / "__init__.py").is_file()
    for key in ("documentation", "issue_tracker", "name"):
        assert manifest[key]
