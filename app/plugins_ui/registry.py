"""Registry of the plugin pages exposed to the ADMIN host.

The registry turns discovered components into sidebar entries and imports a
page module only when the page is actually rendered. Import and render errors
are always converted into :class:`PluginLoadError`, so a broken plugin can
never crash the ADMIN app.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Sequence

from app.plugins_ui.discovery import AdminComponent, clear_cache, discover
from app.plugins_ui.manifest import MAX_ADMIN_MODULE_BYTES, Permission

PLUGIN_UI_DIRS_ENV = "GRINNING_CAT_PLUGIN_UI_DIRS"


class PluginLoadError(Exception):
    """A plugin page could not be imported or rendered."""


@dataclass(frozen=True)
class AdminEntry:
    """A sidebar entry backed by a plugin page module."""

    page_key: str
    label: str
    icon: str
    permission: Permission | None
    plugin_id: str
    component_id: str
    module_path: Path
    plugin_dir: Path


def _log(message: str) -> None:
    print(f"[plugin-ui] {message}")


def _entry_from(component: AdminComponent) -> AdminEntry:
    icon = component.component.icon or ""
    label = f"{icon} {component.component.title}".strip()
    return AdminEntry(
        page_key=component.page_key,
        label=label,
        icon=icon,
        permission=component.component.permission,
        plugin_id=component.plugin_id,
        component_id=component.component.id,
        module_path=component.module_path,
        plugin_dir=component.plugin_dir,
    )


def _dirs_from_env() -> list[Path]:
    raw = os.getenv(PLUGIN_UI_DIRS_ENV, "")
    return [Path(part) for part in raw.split(os.pathsep) if part.strip()]


class PluginPageRegistry:
    """Lazy, per-agent registry of plugin pages.

    ``entries(agent_id)`` runs discovery, whose TTL cache honours
    ``GRINNING_CAT_PLUGIN_UI_REFRESH_SECONDS`` (``0`` disables it), and keeps a
    per-agent index of the resolved entries. ``invalidate`` drops the index and
    the discovery cache so a settings change is visible immediately.
    """

    def __init__(
        self,
        dirs: Sequence[Path | str] | None = None,
        client_factory: Callable[[], Any] | None = None,
        require_installed: bool | None = None,
    ) -> None:
        self._dirs = [Path(directory) for directory in dirs] if dirs is not None else None
        self._client_factory = client_factory
        self._require_installed = require_installed
        self._index: dict[str, dict[str, AdminEntry]] = {}
        self._modules: dict[tuple[str, str], ModuleType] = {}
        self._lock = threading.Lock()

    def _resolve_dirs(self) -> list[Path]:
        return list(self._dirs) if self._dirs is not None else _dirs_from_env()

    def _resolve_client(self) -> Any | None:
        # ``discover`` builds its own client only on a cache miss, so a cache
        # hit does not pay for a client construction on every rerun.
        return self._client_factory() if self._client_factory is not None else None

    def entries(self, agent_id: str) -> list[AdminEntry]:
        """Return the entries visible for ``agent_id`` (never raises).

        The TTL lives in :func:`discover`, so a plugin removed from disk
        disappears after ``GRINNING_CAT_PLUGIN_UI_REFRESH_SECONDS``. The
        per-agent index is rebuilt from the (possibly cached) components, and
        fetching one agent's entries never clears another agent's index.
        """
        try:
            components = discover(
                self._resolve_dirs(),
                agent_id,
                self._require_installed,
                self._resolve_client(),
            )
        except Exception as error:  # noqa: BLE001 - discovery must never break the app
            _log(f"discovery failed for agent '{agent_id}': {error}")
            components = []

        entries = [_entry_from(component) for component in components]
        with self._lock:
            self._index[agent_id] = {entry.page_key: entry for entry in entries}
        return list(entries)

    def get(self, page_key: str, agent_id: str | None = None) -> AdminEntry | None:
        """Return the entry for ``page_key``.

        With ``agent_id`` the lookup is scoped to that agent; without it every
        per-agent index is searched. The same plugin page resolves to the same
        module path for every agent, so the first match is equivalent.
        """
        with self._lock:
            if agent_id is not None:
                return self._index.get(agent_id, {}).get(page_key)
            for index in self._index.values():
                entry = index.get(page_key)
                if entry is not None:
                    return entry
            return None

    def __contains__(self, page_key: object) -> bool:
        return isinstance(page_key, str) and self.get(page_key) is not None

    def load(self, entry: AdminEntry | str) -> ModuleType:
        """Import the page module for ``entry``, caching by path and digest."""
        if isinstance(entry, str):
            resolved = self.get(entry)
            if resolved is None:
                raise PluginLoadError(f"unknown plugin page: {entry}")
            entry = resolved
        page_key = entry.page_key

        path = entry.module_path
        try:
            size = path.stat().st_size
        except OSError as error:
            raise PluginLoadError(f"cannot read plugin page '{page_key}': {error}") from error
        if size > MAX_ADMIN_MODULE_BYTES:
            raise PluginLoadError(
                f"plugin page '{page_key}' is {size} bytes, "
                f"the limit is {MAX_ADMIN_MODULE_BYTES}"
            )

        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise PluginLoadError(f"cannot read plugin page '{page_key}': {error}") from error

        cache_key = (str(path), digest)
        with self._lock:
            cached = self._modules.get(cache_key)
        if cached is not None:
            return cached

        module_name = (
            f"_grinning_cat_admin_plugin_{entry.plugin_id}_"
            f"{entry.component_id}_{digest[:12]}"
        )
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise PluginLoadError(f"cannot build an import spec for '{page_key}'")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except PluginLoadError:
            raise
        except Exception as error:  # noqa: BLE001 - plugin code is untrusted at this boundary
            raise PluginLoadError(f"cannot import plugin page '{page_key}': {error}") from error

        with self._lock:
            self._modules[cache_key] = module
        return module

    def dispatch(self, entry: AdminEntry | str, ctx: Any) -> Any:
        """Import the page and call its ``render(ctx)``.

        ``entry`` may be the already-resolved :class:`AdminEntry`: the app
        passes it so a concurrent session on another agent cannot invalidate
        the lookup between the sidebar and the render.
        """
        if isinstance(entry, str):
            resolved = self.get(entry)
            if resolved is None:
                raise PluginLoadError(f"unknown plugin page: {entry}")
            entry = resolved
        page_key = entry.page_key
        module = self.load(entry)
        render = getattr(module, "render", None)
        if not callable(render):
            raise PluginLoadError(f"plugin page '{page_key}' has no callable render(ctx)")
        try:
            return render(ctx)
        except Exception as error:  # noqa: BLE001 - a plugin render must not crash the app
            raise PluginLoadError(f"plugin page '{page_key}' failed to render: {error}") from error

    def invalidate(self, agent_id: str | None = None) -> None:
        """Drop cached entries (and the discovery cache) for one or all agents."""
        with self._lock:
            if agent_id is None:
                self._index.clear()
            else:
                self._index.pop(agent_id, None)
        clear_cache()


_default_registry: PluginPageRegistry | None = None
_default_lock = threading.Lock()


def get_registry() -> PluginPageRegistry:
    """Return the process-wide registry used by the app."""
    global _default_registry
    with _default_lock:
        if _default_registry is None:
            _default_registry = PluginPageRegistry()
        return _default_registry


def reset_registry() -> None:
    """Drop the process-wide registry (used by tests)."""
    global _default_registry
    with _default_lock:
        _default_registry = None


def entries(agent_id: str) -> list[AdminEntry]:
    return get_registry().entries(agent_id)


def get(page_key: str) -> AdminEntry | None:
    return get_registry().get(page_key)


def load(entry: AdminEntry | str) -> ModuleType:
    return get_registry().load(entry)


def dispatch(entry: AdminEntry | str, ctx: Any) -> Any:
    return get_registry().dispatch(entry, ctx)


def invalidate(agent_id: str | None = None) -> None:
    get_registry().invalidate(agent_id)
