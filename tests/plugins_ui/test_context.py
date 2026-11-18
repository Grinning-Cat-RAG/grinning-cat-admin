"""Tests for ``app.plugins_ui.context``."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from app.plugins_ui.context import build_context
from app.plugins_ui.manifest import Permission
from app.plugins_ui.registry import AdminEntry


def _entry(plugin_root: Path) -> AdminEntry:
    return AdminEntry(
        page_key="plugin:cat_batch:batch-update",
        label="🧪 Batch agent update",
        icon="🧪",
        permission=Permission(resource="SYSTEM", permission="READ"),
        plugin_id="cat_batch",
        component_id="batch-update",
        module_path=plugin_root / "admin/pages/batch_update.py",
        plugin_dir=plugin_root,
    )


def test_build_context_fields(plugin_root: Path, fake_client) -> None:
    import app.utils

    me = {"user": {"id": 7}, "agents": []}

    ctx = build_context(_entry(plugin_root), "system", me, client=fake_client)

    assert ctx.plugin_id == "cat_batch"
    assert ctx.component_id == "batch-update"
    assert ctx.plugin_dir == plugin_root
    assert ctx.client is fake_client
    assert ctx.agent_id == "system"
    assert ctx.me is me
    assert ctx.user == {"id": 7}
    assert ctx.has_access is app.utils.has_access
    assert ctx.render_json_form is app.utils.render_json_form
    assert callable(ctx.log)
    ctx.log("hello")


def test_build_context_creates_client_from_configuration(
    plugin_root: Path, monkeypatch
) -> None:
    import app.utils
    from grinning_cat_python_sdk import Configuration, GrinningCatClient

    configuration = Configuration(
        host="localhost",
        port=1865,
        auth_key=None,
        secure_connection=False,
    )
    monkeypatch.setattr(
        app.utils, "build_client_configuration", lambda: configuration
    )

    ctx = build_context(_entry(plugin_root), "system", None)

    assert isinstance(ctx.client, GrinningCatClient)
    assert ctx.user is None


def test_context_is_frozen(plugin_root: Path, fake_client) -> None:
    ctx = build_context(_entry(plugin_root), "system", None, client=fake_client)

    with pytest.raises(FrozenInstanceError):
        ctx.agent_id = "other"
