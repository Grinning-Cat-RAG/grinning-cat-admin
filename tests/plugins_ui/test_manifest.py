"""Contract tests for ``app.plugins_ui.manifest``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.plugins_ui.manifest import (
    MAX_ADMIN_MODULE_BYTES,
    MAX_MANIFEST_BYTES,
    PluginUiError,
    load_manifest,
)


def _component(manifest: dict[str, Any], component_id: str) -> dict[str, Any]:
    return next(
        component
        for component in manifest["components"]
        if component["id"] == component_id
    )


def test_load_manifest_accepts_canonical_example(plugin_root: Path) -> None:
    manifest = load_manifest(plugin_root)

    assert manifest.plugin_id == "cat_batch"
    assert manifest.schema_version == 1
    assert len(manifest.components) == 4
    assert sorted(component.destination for component in manifest.components) == [
        "admin_agent",
        "admin_system",
        "chatbot",
        "instructor",
    ]


def test_load_manifest_accepts_core_examples_file(
    tmp_path: Path, core_example_path: Path
) -> None:
    plugin_dir = tmp_path / "cat_batch"
    plugin_dir.mkdir()
    (plugin_dir / "ui.json").write_bytes(core_example_path.read_bytes())
    for relative in ("admin/pages/batch_update.py", "admin/pages/agent_tools.py"):
        path = plugin_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def render(ctx):\n    return None\n", encoding="utf-8")

    manifest = load_manifest(plugin_dir)

    assert manifest.plugin_id == "cat_batch"
    assert len(manifest.components) == 4


def test_load_manifest_rejects_traversal_module(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    _component(canonical_manifest, "batch-update")["module"] = "../../etc/passwd"
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match="must not contain"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_rejects_absolute_module(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    _component(canonical_manifest, "batch-update")["module"] = "/etc/passwd"
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match="must be relative"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_rejects_plugin_id_mismatch(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    make_plugin(tmp_path, name="other_name", manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match="does not match the directory name"):
        load_manifest(tmp_path / "other_name")


def test_load_manifest_rejects_unknown_top_level_key(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    canonical_manifest["unexpected"] = 1
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match="unexpected"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_rejects_unknown_component_key(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    _component(canonical_manifest, "batch-update")["unexpected"] = 1
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match="unexpected"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_rejects_widget_destination(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    _component(canonical_manifest, "batch-update")["destination"] = "widget"
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match="destination"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_rejects_instructor_with_py_module(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    _component(canonical_manifest, "ingest-progress")["module"] = "admin/pages/ingest.py"
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match=r"\.js or \.mjs"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_rejects_admin_with_js_module(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    _component(canonical_manifest, "batch-update")["module"] = "admin/pages/batch_update.js"
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match=r"\.py file"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_rejects_oversized_manifest(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    plugin_dir = make_plugin(tmp_path, manifest=canonical_manifest)
    payload = json.dumps(canonical_manifest, ensure_ascii=False)
    (plugin_dir / "ui.json").write_text(
        payload + " " * (MAX_MANIFEST_BYTES + 1), encoding="utf-8"
    )

    with pytest.raises(PluginUiError, match="limit is"):
        load_manifest(plugin_dir)


def test_load_manifest_rejects_oversized_admin_module(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    plugin_dir = make_plugin(tmp_path, manifest=canonical_manifest)
    (plugin_dir / "admin/pages/batch_update.py").write_text(
        "#" * (MAX_ADMIN_MODULE_BYTES + 1), encoding="utf-8"
    )

    with pytest.raises(PluginUiError, match="admin module is"):
        load_manifest(plugin_dir)


def test_load_manifest_rejects_missing_admin_module(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    make_plugin(tmp_path, manifest=canonical_manifest, admin_modules=())

    with pytest.raises(PluginUiError, match="admin module not found"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_rejects_duplicate_component_ids(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    canonical_manifest["components"][1]["id"] = "batch-update"
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match="duplicate component id"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_rejects_too_many_components(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    template = canonical_manifest["components"][0]
    canonical_manifest["components"] = [
        {**template, "id": f"page-{index}"} for index in range(21)
    ]
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match="at most 20"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_rejects_admin_without_permission(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    del _component(canonical_manifest, "batch-update")["permission"]
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match="permission is required"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_rejects_admin_with_slot(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    _component(canonical_manifest, "batch-update")["slot"] = "instructor.tab"
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match="slot is forbidden"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_rejects_admin_with_cat_endpoints(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    _component(canonical_manifest, "batch-update")["cat_endpoints"] = []
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match="cat_endpoints is forbidden"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_rejects_admin_with_launch(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    _component(canonical_manifest, "batch-update")["launch"] = {
        "label": "Open",
        "icon": "🧪",
        "mode": "panel",
    }
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match="launch is forbidden"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_rejects_rita_without_launch(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    del _component(canonical_manifest, "ingest-progress")["launch"]
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match="launch is required"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_rejects_rita_without_cat_endpoints(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    del _component(canonical_manifest, "ingest-progress")["cat_endpoints"]
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match="cat_endpoints is required"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_rejects_rita_with_permission(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    _component(canonical_manifest, "ingest-progress")["permission"] = {
        "resource": "PLUGIN",
        "permission": "READ",
    }
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match="permission is forbidden"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_accepts_rita_capabilities(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    _component(canonical_manifest, "ingest-progress")["rita"] = {
        "read": ["lti.context", "plugin.settings"],
        "write": ["plugin.settings.update"],
    }
    make_plugin(tmp_path, manifest=canonical_manifest)

    manifest = load_manifest(tmp_path / "cat_batch")

    component = next(item for item in manifest.components if item.id == "ingest-progress")
    assert component.rita is not None
    assert component.rita.read == ["lti.context", "plugin.settings"]
    assert component.rita.write == ["plugin.settings.update"]


def test_load_manifest_rejects_rita_on_admin_component(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    _component(canonical_manifest, "batch-update")["rita"] = {"read": ["lti.context"]}
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match="rita is forbidden"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_rejects_invalid_rita_capability_name(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    _component(canonical_manifest, "ingest-progress")["rita"] = {"read": ["Bad-Name"]}
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match="should match pattern"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_rejects_too_many_rita_capabilities(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    _component(canonical_manifest, "ingest-progress")["rita"] = {
        "read": [f"capability.{index}" for index in range(21)]
    }
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match="at most 20"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_rejects_slot_of_another_destination(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    _component(canonical_manifest, "ingest-progress")["slot"] = "chatbot.sidebar"
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match="not valid for destination"):
        load_manifest(tmp_path / "cat_batch")


@pytest.mark.parametrize(
    "endpoint",
    [
        "DELETE /custom/x",
        "GET custom/x",
        "GET",
        "GET /custom/x?y=1",
        "GET /custom/x#frag",
        "GET /custom//x",
        "GET /custom/../x",
        "GET http://evil/x",
        "GET /custom/x*y",
        "GET /custom/x\r\nHost: evil",
        "GET /custom/x ",
        "GET " + "/a" * 300,
    ],
)
def test_load_manifest_rejects_bad_endpoint(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any], endpoint: str
) -> None:
    _component(canonical_manifest, "ingest-progress")["cat_endpoints"] = [endpoint]
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_accepts_empty_cat_endpoints(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    _component(canonical_manifest, "ingest-progress")["cat_endpoints"] = []
    make_plugin(tmp_path, manifest=canonical_manifest)

    manifest = load_manifest(tmp_path / "cat_batch")

    component = next(
        item for item in manifest.components if item.id == "ingest-progress"
    )
    assert component.cat_endpoints == []


def test_load_manifest_rejects_missing_ui_json(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "cat_batch"
    plugin_dir.mkdir()

    with pytest.raises(PluginUiError, match="ui.json not found"):
        load_manifest(plugin_dir)


def test_load_manifest_rejects_invalid_json(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "cat_batch"
    plugin_dir.mkdir()
    (plugin_dir / "ui.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(PluginUiError, match="not valid UTF-8 JSON"):
        load_manifest(plugin_dir)


def test_load_manifest_rejects_wrong_schema(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any]
) -> None:
    canonical_manifest["schema"] = 2
    make_plugin(tmp_path, manifest=canonical_manifest)

    with pytest.raises(PluginUiError, match="schema"):
        load_manifest(tmp_path / "cat_batch")


def test_load_manifest_rejects_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(PluginUiError, match="plugin directory not found"):
        load_manifest(tmp_path / "missing")
