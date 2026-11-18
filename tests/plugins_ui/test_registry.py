"""Tests for ``app.plugins_ui.registry``."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.plugins_ui.manifest import MAX_ADMIN_MODULE_BYTES
from app.plugins_ui.registry import PluginLoadError, PluginPageRegistry


def _registry(plugin_root: Path, client, require_installed: bool = True) -> PluginPageRegistry:
    return PluginPageRegistry(
        dirs=[plugin_root.parent],
        client_factory=lambda: client,
        require_installed=require_installed,
    )


def test_registry_entries_and_get(plugin_root: Path, fake_client) -> None:
    registry = _registry(plugin_root, fake_client)

    entries = registry.entries("system")

    assert len(entries) == 1
    entry = entries[0]
    assert entry.page_key == "plugin:cat_batch:batch-update"
    assert entry.label == "🧪 Batch agent update"
    assert entry.icon == "🧪"
    assert entry.permission is not None
    assert entry.permission.resource == "SYSTEM"
    assert entry.permission.permission == "READ"
    assert entry.plugin_id == "cat_batch"
    assert entry.component_id == "batch-update"
    assert entry.module_path.name == "batch_update.py"
    assert entry.plugin_dir == plugin_root
    assert registry.get(entry.page_key) is entry
    assert entry.page_key in registry


def test_registry_entries_switch_agent(plugin_root: Path, fake_client) -> None:
    registry = _registry(plugin_root, fake_client)

    system_entries = registry.entries("system")
    agent_entries = registry.entries("cat_batch")

    assert system_entries[0].page_key != agent_entries[0].page_key
    assert registry.get(system_entries[0].page_key) is None
    assert registry.get(agent_entries[0].page_key) is agent_entries[0]


def test_registry_load_and_dispatch(plugin_root: Path, fake_client) -> None:
    (plugin_root / "admin/pages/batch_update.py").write_text(
        "def render(ctx):\n"
        "    return {'plugin': ctx.plugin_id, 'component': ctx.component_id}\n",
        encoding="utf-8",
    )
    registry = _registry(plugin_root, fake_client)
    page_key = registry.entries("system")[0].page_key

    module = registry.load(page_key)
    ctx = SimpleNamespace(plugin_id="cat_batch", component_id="batch-update")

    assert callable(module.render)
    assert registry.dispatch(page_key, ctx) == {
        "plugin": "cat_batch",
        "component": "batch-update",
    }


def test_registry_load_caches_module(plugin_root: Path, fake_client) -> None:
    registry = _registry(plugin_root, fake_client)
    page_key = registry.entries("system")[0].page_key

    assert registry.load(page_key) is registry.load(page_key)


def test_registry_dispatch_import_failure(plugin_root: Path, fake_client) -> None:
    (plugin_root / "admin/pages/batch_update.py").write_text(
        "raise RuntimeError('boom')\n", encoding="utf-8"
    )
    registry = _registry(plugin_root, fake_client)
    page_key = registry.entries("system")[0].page_key

    with pytest.raises(PluginLoadError, match="cannot import"):
        registry.dispatch(page_key, object())


def test_registry_dispatch_render_failure(plugin_root: Path, fake_client) -> None:
    (plugin_root / "admin/pages/batch_update.py").write_text(
        "def render(ctx):\n    raise ValueError('nope')\n", encoding="utf-8"
    )
    registry = _registry(plugin_root, fake_client)
    page_key = registry.entries("system")[0].page_key

    with pytest.raises(PluginLoadError, match="failed to render"):
        registry.dispatch(page_key, object())


def test_registry_dispatch_missing_render(plugin_root: Path, fake_client) -> None:
    (plugin_root / "admin/pages/batch_update.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    registry = _registry(plugin_root, fake_client)
    page_key = registry.entries("system")[0].page_key

    with pytest.raises(PluginLoadError, match="no callable render"):
        registry.dispatch(page_key, object())


def test_registry_unknown_page_key(plugin_root: Path, fake_client) -> None:
    registry = _registry(plugin_root, fake_client)

    assert registry.get("plugin:nope:nope") is None
    assert "plugin:nope:nope" not in registry
    with pytest.raises(PluginLoadError, match="unknown plugin page"):
        registry.load("plugin:nope:nope")
    with pytest.raises(PluginLoadError, match="unknown plugin page"):
        registry.dispatch("plugin:nope:nope", object())


def test_registry_invalidate(plugin_root: Path, fake_client) -> None:
    registry = _registry(plugin_root, fake_client)

    registry.entries("system")
    registry.invalidate("system")
    registry.entries("system")

    assert len(fake_client.admins.calls) == 2


def test_registry_entries_never_raise(plugin_root: Path) -> None:
    def broken_factory():
        raise RuntimeError("no client")

    registry = PluginPageRegistry(
        dirs=[plugin_root.parent],
        client_factory=broken_factory,
        require_installed=True,
    )

    assert registry.entries("system") == []


def test_registry_rejects_module_grown_after_discovery(
    plugin_root: Path, fake_client
) -> None:
    registry = _registry(plugin_root, fake_client)
    page_key = registry.entries("system")[0].page_key
    (plugin_root / "admin/pages/batch_update.py").write_text(
        "#" * (MAX_ADMIN_MODULE_BYTES + 1), encoding="utf-8"
    )

    with pytest.raises(PluginLoadError, match="limit is"):
        registry.load(page_key)
