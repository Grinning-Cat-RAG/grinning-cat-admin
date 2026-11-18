"""The plugin UI package mounted in the ADMIN app shell.

These tests drive ``app.main`` with a fake registry: no plugin is imported and
no CAT call is made. They cover the sidebar entry contract, the dispatch
branch, the error isolation and the agent-change invalidation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import streamlit as st

from app import main
from app.plugins_ui import registry as plugin_registry
from app.plugins_ui.manifest import Permission
from app.plugins_ui.registry import AdminEntry

PAGE_KEY = "plugin:cat_batch:batch-update"

ME = {
    "username": "admin",
    "id": "user-1",
    "agents": [
        {
            "agent_name": "system",
            "user": {"id": "user-1", "permissions": {"SYSTEM": ["READ"]}},
        },
        {
            "agent_name": "cat_batch",
            "user": {"id": "user-1", "permissions": {"PLUGIN": ["READ"]}},
        },
    ],
}


def _entry(
    plugin_root: Path,
    page_key: str = PAGE_KEY,
    label: str = "🧪 Batch agent update",
    permission: Permission | None = None,
) -> AdminEntry:
    return AdminEntry(
        page_key=page_key,
        label=label,
        icon="🧪",
        permission=permission or Permission(resource="SYSTEM", permission="READ"),
        plugin_id="cat_batch",
        component_id="batch-update",
        module_path=plugin_root / "admin/pages/batch_update.py",
        plugin_dir=plugin_root,
    )


@pytest.fixture
def sidebar(monkeypatch, session_state):
    """Render the sidebar and collect the button labels, in order."""
    buttons: list[str] = []

    session_state.update(
        {"token": "jwt-token", "agent_id": "system", "selected_page": "plugins"}
    )
    monkeypatch.setattr(main, "has_access", lambda *args, **kwargs: True)
    monkeypatch.setattr(main, "is_system_agent_selected", lambda: True)
    monkeypatch.setattr(main, "_build_agents_toggle_select", lambda *a, **k: None)
    monkeypatch.setattr(st, "markdown", lambda *a, **k: None)
    monkeypatch.setattr(st.sidebar, "markdown", lambda *a, **k: None)
    monkeypatch.setattr(st, "divider", lambda *a, **k: None)
    monkeypatch.setattr(st, "info", lambda *a, **k: None)
    monkeypatch.setattr(st, "button", lambda label, *a, **k: buttons.append(label) or False)
    return {"buttons": buttons, "state": session_state}


# --- sidebar entries --------------------------------------------------------


def test_menu_group_entry_shape_and_permission_gate(
    monkeypatch, session_state, plugin_root: Path
) -> None:
    session_state.update({"agent_id": "system"})
    entries = [
        _entry(plugin_root),
        _entry(
            plugin_root,
            page_key="plugin:cat_batch:agent-tools",
            label="🧰 Agent tools",
            permission=Permission(resource="PLUGIN", permission="READ"),
        ),
    ]
    monkeypatch.setattr(plugin_registry, "entries", lambda agent_id: entries)
    monkeypatch.setattr(
        main, "has_access", lambda resource, role, me, **kwargs: resource == "SYSTEM"
    )

    group = main._plugin_ui_menu_group(ME)

    assert group["🧪 Batch agent update"] == {
        "page": PAGE_KEY,
        "label": "🧪 Batch agent update",
        "allowed": True,
    }
    assert group["🧰 Agent tools"]["page"] == "plugin:cat_batch:agent-tools"
    assert group["🧰 Agent tools"]["allowed"] is False


def test_menu_group_is_empty_in_api_key_mode(monkeypatch, session_state) -> None:
    session_state.update({"agent_id": "system"})
    monkeypatch.setattr(
        plugin_registry, "entries", lambda agent_id: pytest.fail("must not discover")
    )

    assert main._plugin_ui_menu_group(None) == {}


def test_menu_group_is_empty_when_discovery_fails(
    monkeypatch, session_state
) -> None:
    session_state.update({"agent_id": "system"})

    def boom(agent_id):
        raise ConnectionError("CAT is unreachable")

    monkeypatch.setattr(plugin_registry, "entries", boom)

    assert main._plugin_ui_menu_group(ME) == {}


def test_sidebar_places_plugin_pages_before_system(
    monkeypatch, sidebar, plugin_root: Path
) -> None:
    monkeypatch.setattr(plugin_registry, "entries", lambda agent_id: [_entry(plugin_root)])

    main._render_sidebar_navigation(ME)

    assert "🧪 Batch agent update" in sidebar["buttons"]
    assert sidebar["buttons"].index("🧪 Batch agent update") < sidebar["buttons"].index(
        "⚙️ System"
    )


def test_sidebar_resets_a_plugin_page_that_vanished(
    monkeypatch, sidebar
) -> None:
    sidebar["state"]["selected_page"] = PAGE_KEY
    monkeypatch.setattr(plugin_registry, "entries", lambda agent_id: [])

    main._render_sidebar_navigation(ME)

    assert sidebar["state"]["selected_page"] is None


# --- dispatch ---------------------------------------------------------------


def test_render_page_dispatches_to_the_plugin(
    monkeypatch, session_state, plugin_root: Path
) -> None:
    session_state.update({"agent_id": "system", "selected_page": PAGE_KEY})
    entry = _entry(plugin_root)
    calls = []
    monkeypatch.setattr(plugin_registry, "entries", lambda agent_id: [entry])
    monkeypatch.setattr(
        plugin_registry, "dispatch", lambda page_key, ctx: calls.append((page_key, ctx))
    )
    monkeypatch.setattr(main, "has_access", lambda *a, **k: True)
    monkeypatch.setattr(main, "welcome", lambda *a, **k: pytest.fail("welcome must not run"))

    asyncio.run(main._render_page(ME))

    assert calls[0][0] == PAGE_KEY
    ctx = calls[0][1]
    assert ctx.agent_id == "system"
    assert ctx.me is ME
    assert ctx.plugin_id == "cat_batch"


def test_render_page_skips_a_page_the_user_cannot_access(
    monkeypatch, session_state, plugin_root: Path
) -> None:
    session_state.update({"agent_id": "system", "selected_page": PAGE_KEY})
    monkeypatch.setattr(plugin_registry, "entries", lambda agent_id: [_entry(plugin_root)])
    monkeypatch.setattr(
        plugin_registry, "dispatch", lambda *a, **k: pytest.fail("must not dispatch")
    )
    monkeypatch.setattr(main, "has_access", lambda *a, **k: False)
    monkeypatch.setattr(main, "welcome", lambda *a, **k: None)

    asyncio.run(main._render_page(ME))

    assert session_state["selected_page"] is None


def test_render_page_falls_back_when_the_entry_is_gone(
    monkeypatch, session_state
) -> None:
    session_state.update({"agent_id": "system", "selected_page": PAGE_KEY})
    monkeypatch.setattr(plugin_registry, "entries", lambda agent_id: [])
    monkeypatch.setattr(
        plugin_registry, "dispatch", lambda *a, **k: pytest.fail("must not dispatch")
    )
    monkeypatch.setattr(main, "welcome", lambda *a, **k: None)

    asyncio.run(main._render_page(ME))

    assert session_state["selected_page"] is None


def test_plugin_render_failure_is_isolated_and_skipped(
    monkeypatch, session_state, plugin_root: Path
) -> None:
    session_state.update({"agent_id": "system", "selected_page": PAGE_KEY})
    entry = _entry(plugin_root)
    errors: list[str] = []
    calls: list[str] = []

    def boom(page_key, ctx):
        calls.append(page_key)
        raise RuntimeError("kaboom")

    monkeypatch.setattr(plugin_registry, "entries", lambda agent_id: [entry])
    monkeypatch.setattr(plugin_registry, "dispatch", boom)
    monkeypatch.setattr(main, "has_access", lambda *a, **k: True)
    monkeypatch.setattr(st, "error", lambda msg, *a, **k: errors.append(str(msg)))

    asyncio.run(main._render_page(ME))

    assert calls == [PAGE_KEY]
    assert len(errors) == 1
    assert "cat_batch" in errors[0]
    assert PAGE_KEY in session_state["_plugin_ui_failed"]

    # the next rerun must not retry the broken page
    monkeypatch.setattr(main, "welcome", lambda *a, **k: None)
    asyncio.run(main._render_page(ME))

    assert calls == [PAGE_KEY]
    assert session_state["selected_page"] is None


def test_failed_plugin_page_is_hidden_from_the_sidebar(
    monkeypatch, sidebar, plugin_root: Path
) -> None:
    sidebar["state"]["_plugin_ui_failed"] = {PAGE_KEY}
    monkeypatch.setattr(plugin_registry, "entries", lambda agent_id: [_entry(plugin_root)])

    main._render_sidebar_navigation(ME)

    assert "🧪 Batch agent update" not in sidebar["buttons"]


# --- agent change -----------------------------------------------------------


def test_agent_change_invalidates_the_previous_agent(
    monkeypatch, session_state
) -> None:
    session_state.update({"agent_id": "system", "token": "jwt-token"})
    invalidated: list[str] = []
    monkeypatch.setattr(
        main, "build_agents_options_select", lambda *a, **k: {"cat_batch": "cat_batch"}
    )
    monkeypatch.setattr(st, "selectbox", lambda *a, **k: "cat_batch")
    monkeypatch.setattr(st, "divider", lambda *a, **k: None)
    monkeypatch.setattr(main, "_reset_agent_scoped_state", session_state.clear)
    monkeypatch.setattr(
        main, "_invalidate_plugin_ui", lambda agent_id: invalidated.append(agent_id)
    )
    monkeypatch.setattr(st, "rerun", lambda: None)

    main._build_agents_toggle_select("k", ME)

    assert invalidated == ["system"]
    assert session_state["agent_id"] == "cat_batch"
