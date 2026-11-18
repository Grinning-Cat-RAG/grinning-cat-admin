"""Shared fixtures for the plugin UI loader tests.

Everything is hermetic: plugin directories live under ``tmp_path`` and the CAT
client is a fake object, so no network, Redis or real plugin is involved.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.plugins_ui import discovery, registry  # noqa: E402

CORE_EXAMPLE = (
    Path("/root/git/grinning-cat-core/docs/plugins/plugin-ui/examples/ui.json")
)

# Copy of the canonical example in grinning-cat-core/docs/plugins/plugin-ui.
CANONICAL_MANIFEST: dict[str, Any] = {
    "schema": 1,
    "plugin_id": "cat_batch",
    "components": [
        {
            "id": "batch-update",
            "title": "Batch agent update",
            "icon": "🧪",
            "destination": "admin_system",
            "module": "admin/pages/batch_update.py",
            "permission": {"resource": "SYSTEM", "permission": "READ"},
            "order": 10,
        },
        {
            "id": "agent-tools",
            "title": "Agent tools",
            "icon": "🧰",
            "destination": "admin_agent",
            "module": "admin/pages/agent_tools.py",
            "permission": {"resource": "PLUGIN", "permission": "READ"},
            "order": 20,
        },
        {
            "id": "ingest-progress",
            "title": "Ingest progress",
            "icon": "⏳",
            "destination": "instructor",
            "module": "rita/dist/ingest-progress.js",
            "slot": "instructor.tab",
            "launch": {"label": "Ingest", "icon": "⏳", "mode": "panel"},
            "cat_endpoints": ["GET /custom/cat_batch/ingest_status"],
            "order": 10,
        },
        {
            "id": "student-tools",
            "title": "Tools",
            "icon": "🧠",
            "destination": "chatbot",
            "module": "rita/dist/student-tools.js",
            "slot": "chatbot.sidebar",
            "launch": {"label": "Tools", "icon": "🧠", "mode": "panel"},
            "cat_endpoints": ["GET /custom/cat_batch/student_status"],
            "order": 10,
        },
    ],
}

DEFAULT_ADMIN_MODULES = (
    "admin/pages/batch_update.py",
    "admin/pages/agent_tools.py",
)


def write_plugin(
    root: Path,
    name: str = "cat_batch",
    manifest: dict[str, Any] | None = None,
    admin_modules: tuple[str, ...] | None = None,
    rita_assets: tuple[str, ...] = (),
) -> Path:
    """Create a plugin directory with ``ui.json`` and its module files."""
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)

    data = copy.deepcopy(manifest if manifest is not None else CANONICAL_MANIFEST)
    (plugin_dir / "ui.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )

    modules = DEFAULT_ADMIN_MODULES if admin_modules is None else admin_modules
    for relative in modules:
        path = plugin_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def render(ctx):\n    return None\n", encoding="utf-8")

    for relative in rita_assets:
        path = plugin_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export function mount() {}\n", encoding="utf-8")

    return plugin_dir


class FakePluginItem:
    def __init__(self, plugin_id: str, active: bool = True) -> None:
        self.id = plugin_id
        self.local_info = {"active": active}


class FakeCollection:
    def __init__(self, items: list[FakePluginItem]) -> None:
        self.installed = items


class FakeEndpoint:
    def __init__(self, plugin_ids: tuple[str, ...], active: bool = True) -> None:
        self.plugin_ids = plugin_ids
        self.active = active
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def get_available_plugins(self, *args: Any, **kwargs: Any) -> FakeCollection:
        self.calls.append((args, kwargs))
        return FakeCollection(
            [FakePluginItem(plugin_id, self.active) for plugin_id in self.plugin_ids]
        )


class FakeClient:
    """Minimal stand-in for ``GrinningCatClient``."""

    def __init__(
        self,
        plugin_ids: tuple[str, ...] = ("cat_batch",),
        system_plugin_ids: tuple[str, ...] | None = None,
        active: bool = True,
    ) -> None:
        self.plugins = FakeEndpoint(plugin_ids, active)
        self.admins = FakeEndpoint(
            system_plugin_ids if system_plugin_ids is not None else plugin_ids,
            active,
        )


class BrokenClient:
    """A client whose CAT endpoints always fail."""

    def __init__(self) -> None:
        self.plugins = self
        self.admins = self

    def get_available_plugins(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("CAT is unreachable")


@pytest.fixture(autouse=True)
def _clean_caches():
    discovery.clear_cache()
    registry.reset_registry()
    yield
    discovery.clear_cache()
    registry.reset_registry()


@pytest.fixture
def canonical_manifest() -> dict[str, Any]:
    return copy.deepcopy(CANONICAL_MANIFEST)


@pytest.fixture
def make_plugin():
    return write_plugin


@pytest.fixture
def plugin_root(tmp_path: Path, make_plugin) -> Path:
    return make_plugin(tmp_path)


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def fake_client_class():
    return FakeClient


@pytest.fixture
def broken_client() -> BrokenClient:
    return BrokenClient()


@pytest.fixture
def core_example_path() -> Path:
    if not CORE_EXAMPLE.is_file():
        pytest.skip(f"canonical example not available: {CORE_EXAMPLE}")
    return CORE_EXAMPLE
