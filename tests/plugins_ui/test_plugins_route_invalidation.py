"""Every plugin mutation in the Plugins page must invalidate the UI registry.

A toggle or an uninstall changes which plugin pages are visible; without the
invalidation the sidebar would keep showing them until the discovery TTL
expires (60s by default).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import streamlit as st

from app.plugins_ui import registry as plugin_registry
from app.plugins_ui.registry import PluginPageRegistry
from app.routes import plugins

ME = {"username": "admin", "agents": []}


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _Spinner:
    def empty(self):
        return None


def _columns(spec):
    count = len(spec) if isinstance(spec, (list, tuple)) else spec
    return [_Ctx() for _ in range(count)]


def _registry(plugin_root: Path, client) -> PluginPageRegistry:
    return PluginPageRegistry(
        dirs=[plugin_root.parent],
        client_factory=lambda: client,
        require_installed=True,
    )


def _stub_layout(monkeypatch, *, button=True):
    monkeypatch.setattr(st, "columns", _columns)
    monkeypatch.setattr(st, "button", lambda *a, **k: button)
    for name in (
        "header",
        "subheader",
        "divider",
        "warning",
        "toast",
        "error",
        "markdown",
        "json",
        "write",
    ):
        monkeypatch.setattr(st, name, lambda *a, **k: None)
    monkeypatch.setattr(st, "expander", lambda *a, **k: _Ctx())
    monkeypatch.setattr(st, "rerun", lambda: None)


def test_per_agent_toggle_invalidates_the_registry_immediately(
    monkeypatch, session_state, plugin_root: Path, fake_client
) -> None:
    """The toggle path must drop the cache, so a plugin removed from disk
    disappears at once instead of after the TTL."""
    registry = _registry(plugin_root, fake_client)
    assert len(registry.entries("system")) == 1
    monkeypatch.setattr(plugin_registry, "_default_registry", registry)

    session_state.update({"agent_id": "system"})
    _stub_layout(monkeypatch)
    monkeypatch.setattr(plugins, "get_settings", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(plugins, "show_overlay_spinner", lambda *a, **k: _Spinner())

    toggled = []

    class _Plugins:
        @staticmethod
        def get_available_plugins(agent_id, plugin_id):
            return SimpleNamespace(
                installed=[SimpleNamespace(id=plugin_id, local_info={"active": True})]
            )

        @staticmethod
        def get_plugin_settings(plugin_id, agent_id):
            return {}

        @staticmethod
        def put_toggle_plugin(plugin_id, agent_id):
            toggled.append((plugin_id, agent_id))

    class _Client:
        def __init__(self, *_args, **_kwargs):
            self.plugins = _Plugins()

    monkeypatch.setattr(plugins, "GrinningCatClient", _Client)

    # the plugin is gone from disk: only the invalidation can hide it now
    shutil.rmtree(plugin_root)

    plugins.manage_plugin.__wrapped__("cat_batch", [])

    assert toggled == [("cat_batch", "system")]
    assert registry.entries("system") == []


def test_system_toggle_invalidates_every_agent(monkeypatch, session_state) -> None:
    calls = []
    monkeypatch.setattr(
        plugins, "_invalidate_plugin_ui", lambda agent_id=None: calls.append(agent_id)
    )
    monkeypatch.setattr(plugins, "has_access", lambda *a, **k: True)
    monkeypatch.setattr(plugins, "show_overlay_spinner", lambda *a, **k: _Spinner())
    _stub_layout(monkeypatch)
    # only the toggle button is pressed: "View Details" would open a dialog
    monkeypatch.setattr(
        st, "button", lambda *a, **k: k.get("key") == "toggle_cat_batch"
    )

    toggled = []
    client = SimpleNamespace(
        admins=SimpleNamespace(put_toggle_plugin=lambda plugin_id: toggled.append(plugin_id))
    )
    plugin = SimpleNamespace(
        id="cat_batch",
        name="Cat Batch",
        thumb="data:image/png;base64,x",
        local_info={"active": True},
        model_dump=lambda: {},
    )

    plugins._render_installed_plugin_admins(plugin, client, [], ME, ["cat_batch"])

    assert toggled == ["cat_batch"]
    assert calls == [None]


def test_uninstall_invalidates_every_agent(monkeypatch, session_state) -> None:
    calls = []
    monkeypatch.setattr(
        plugins, "_invalidate_plugin_ui", lambda agent_id=None: calls.append(agent_id)
    )
    monkeypatch.setattr(plugins, "has_access", lambda *a, **k: True)
    monkeypatch.setattr(plugins, "_list_plugins_installed", lambda *a, **k: None)
    monkeypatch.setattr(plugins, "show_overlay_spinner", lambda *a, **k: _Spinner())
    _stub_layout(monkeypatch)
    session_state["plugin_to_uninstall"] = "cat_batch"

    deleted = []
    client = SimpleNamespace(
        admins=SimpleNamespace(
            get_available_plugins=lambda **kwargs: SimpleNamespace(registry=[]),
            delete_plugin=lambda plugin_id: deleted.append(plugin_id),
        ),
        custom=SimpleNamespace(get_custom=lambda *a, **k: []),
    )

    plugins._list_plugins_admins(client, "", ME)

    assert deleted == ["cat_batch"]
    assert calls == [None]
