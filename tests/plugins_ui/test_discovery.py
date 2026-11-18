"""Tests for ``app.plugins_ui.discovery``."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

from app.plugins_ui import discovery
from app.plugins_ui.discovery import discover


def test_discover_example_yields_two_admin_components(
    tmp_path: Path, plugin_root: Path, fake_client
) -> None:
    system = discover([tmp_path], "system", True, fake_client)
    agent = discover([tmp_path], "cat_batch", True, fake_client)

    assert [item.component.destination for item in system] == ["admin_system"]
    assert [item.component.destination for item in agent] == ["admin_agent"]
    assert len(system) + len(agent) == 2
    assert all(
        item.component.destination in {"admin_system", "admin_agent"}
        for item in system + agent
    )


def test_discover_system_agent_only_system_components(
    tmp_path: Path, plugin_root: Path, fake_client
) -> None:
    components = discover([tmp_path], "system", True, fake_client)

    assert [item.component.id for item in components] == ["batch-update"]
    assert components[0].page_key == "plugin:cat_batch:batch-update"
    assert components[0].module_path == (
        plugin_root / "admin/pages/batch_update.py"
    ).resolve()


def test_discover_real_agent_only_agent_components(
    tmp_path: Path, plugin_root: Path, fake_client
) -> None:
    components = discover([tmp_path], "cat_batch", True, fake_client)

    assert [item.component.id for item in components] == ["agent-tools"]
    assert components[0].page_key == "plugin:cat_batch:agent-tools"


def test_discover_inactive_plugin_yields_nothing(
    tmp_path: Path, plugin_root: Path, fake_client_class
) -> None:
    client = fake_client_class(active=False)

    assert discover([tmp_path], "system", True, client) == []


def test_discover_plugin_not_installed_yields_nothing(
    tmp_path: Path, plugin_root: Path, fake_client_class
) -> None:
    client = fake_client_class(plugin_ids=())

    assert discover([tmp_path], "system", True, client) == []


def test_discover_cat_unreachable_requires_installed(
    tmp_path: Path, plugin_root: Path, broken_client
) -> None:
    assert discover([tmp_path], "system", True, broken_client) == []


def test_discover_cat_unreachable_without_require_installed(
    tmp_path: Path, plugin_root: Path, broken_client
) -> None:
    components = discover([tmp_path], "system", False, broken_client)

    assert [item.component.id for item in components] == ["batch-update"]


def test_discover_require_installed_defaults_to_env(
    tmp_path: Path, plugin_root: Path, broken_client, monkeypatch
) -> None:
    monkeypatch.setenv("GRINNING_CAT_PLUGIN_UI_REQUIRE_INSTALLED", "false")

    components = discover([tmp_path], "system", None, broken_client)

    assert [item.component.id for item in components] == ["batch-update"]


def test_discover_skips_plugin_id_mismatch(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any], fake_client
) -> None:
    make_plugin(tmp_path, name="wrong_name", manifest=canonical_manifest)

    assert discover([tmp_path], "system", True, fake_client) == []


def test_discover_skips_directory_without_ui_json(
    tmp_path: Path, fake_client
) -> None:
    (tmp_path / "empty_plugin").mkdir()

    assert discover([tmp_path], "system", True, fake_client) == []


def test_discover_dedupes_plugin_across_dirs(
    tmp_path: Path, make_plugin, fake_client
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    make_plugin(first)
    make_plugin(second)

    components = discover([first, second], "system", True, fake_client)

    assert len(components) == 1


def test_discover_does_not_import_modules(
    tmp_path: Path, make_plugin, fake_client
) -> None:
    plugin_dir = make_plugin(tmp_path)
    (plugin_dir / "admin/pages/batch_update.py").write_text(
        "raise RuntimeError('imported during discovery')\n", encoding="utf-8"
    )

    components = discover([tmp_path], "system", True, fake_client)

    assert len(components) == 1
    assert not any(
        name.startswith("_grinning_cat_admin_plugin_") for name in sys.modules
    )


def test_discover_cache_hit(tmp_path: Path, plugin_root: Path, fake_client) -> None:
    discover([tmp_path], "system", True, fake_client)
    discover([tmp_path], "system", True, fake_client)

    assert len(fake_client.admins.calls) == 1


def test_discover_cache_expires(
    tmp_path: Path, plugin_root: Path, fake_client, monkeypatch
) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(discovery.time, "monotonic", lambda: clock["now"])

    discover([tmp_path], "system", True, fake_client)
    clock["now"] += 61
    discover([tmp_path], "system", True, fake_client)

    assert len(fake_client.admins.calls) == 2


def test_discover_cache_disabled_with_zero(
    tmp_path: Path, plugin_root: Path, fake_client, monkeypatch
) -> None:
    monkeypatch.setenv("GRINNING_CAT_PLUGIN_UI_REFRESH_SECONDS", "0")

    discover([tmp_path], "system", True, fake_client)
    discover([tmp_path], "system", True, fake_client)

    assert len(fake_client.admins.calls) == 2


def test_discover_clear_cache(
    tmp_path: Path, plugin_root: Path, fake_client
) -> None:
    discover([tmp_path], "system", True, fake_client)
    discovery.clear_cache()
    discover([tmp_path], "system", True, fake_client)

    assert len(fake_client.admins.calls) == 2


def test_discover_sorts_by_order(
    tmp_path: Path, make_plugin, canonical_manifest: dict[str, Any], fake_client
) -> None:
    manifest = copy.deepcopy(canonical_manifest)
    template = manifest["components"][0]
    manifest["components"] = [
        {**template, "id": "second", "order": 20},
        {**template, "id": "first", "order": 10},
    ]
    make_plugin(tmp_path, manifest=manifest)

    components = discover([tmp_path], "system", True, fake_client)

    assert [item.component.id for item in components] == ["first", "second"]


def test_discover_ignores_files_in_root(
    tmp_path: Path, plugin_root: Path, fake_client
) -> None:
    (tmp_path / "stray.txt").write_text("x", encoding="utf-8")

    assert len(discover([tmp_path], "system", True, fake_client)) == 1


def test_discover_missing_directory_is_ignored(
    tmp_path: Path, fake_client
) -> None:
    assert discover([tmp_path / "nope"], "system", True, fake_client) == []
