"""Cache lifetime and per-agent isolation of the plugin page registry.

The registry must honour ``GRINNING_CAT_PLUGIN_UI_REFRESH_SECONDS`` (the TTL
lives in ``discover``) and must never let one agent's session clear another
agent's index.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

from app.plugins_ui import discovery
from app.plugins_ui.registry import PluginPageRegistry


def _registry(plugin_root: Path, client) -> PluginPageRegistry:
    return PluginPageRegistry(
        dirs=[plugin_root.parent],
        client_factory=lambda: client,
        require_installed=True,
    )


def test_entries_expire_after_the_ttl(
    monkeypatch, plugin_root: Path, fake_client
) -> None:
    monkeypatch.setenv("GRINNING_CAT_PLUGIN_UI_REFRESH_SECONDS", "1")
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        discovery, "time", SimpleNamespace(monotonic=lambda: clock["now"])
    )
    registry = _registry(plugin_root, fake_client)

    assert len(registry.entries("system")) == 1

    shutil.rmtree(plugin_root)
    clock["now"] += 1.1

    assert registry.entries("system") == []


def test_zero_ttl_disables_the_cache(
    monkeypatch, plugin_root: Path, fake_client
) -> None:
    monkeypatch.setenv("GRINNING_CAT_PLUGIN_UI_REFRESH_SECONDS", "0")
    registry = _registry(plugin_root, fake_client)

    assert len(registry.entries("system")) == 1

    shutil.rmtree(plugin_root)

    assert registry.entries("system") == []


def test_two_agents_coexist(plugin_root: Path, fake_client) -> None:
    registry = _registry(plugin_root, fake_client)

    system_entries = registry.entries("system")
    agent_entries = registry.entries("agent-x")

    assert system_entries and agent_entries
    assert registry.get(system_entries[0].page_key, "system") is system_entries[0]
    assert registry.get(agent_entries[0].page_key, "agent-x") is agent_entries[0]

    # re-fetching one agent must not clear the other agent's index
    registry.entries("system")

    assert registry.get(agent_entries[0].page_key, "agent-x") is agent_entries[0]


def test_dispatch_with_a_resolved_entry_survives_another_agent(
    plugin_root: Path, fake_client
) -> None:
    (plugin_root / "admin/pages/batch_update.py").write_text(
        "def render(ctx):\n    return ctx.plugin_id\n", encoding="utf-8"
    )
    registry = _registry(plugin_root, fake_client)
    entry = registry.entries("system")[0]

    # another Streamlit session on another agent fetches its own entries
    registry.entries("agent-x")

    assert registry.dispatch(entry, SimpleNamespace(plugin_id="cat_batch")) == "cat_batch"


def test_dispatch_by_page_key_after_another_agent(
    plugin_root: Path, fake_client
) -> None:
    (plugin_root / "admin/pages/batch_update.py").write_text(
        "def render(ctx):\n    return ctx.plugin_id\n", encoding="utf-8"
    )
    registry = _registry(plugin_root, fake_client)
    page_key = registry.entries("system")[0].page_key

    registry.entries("agent-x")

    assert (
        registry.dispatch(page_key, SimpleNamespace(plugin_id="cat_batch"), "system")
        == "cat_batch"
    )
