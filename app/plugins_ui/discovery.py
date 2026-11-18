"""Discovery of the ADMIN components exported by installed plugins.

Each plugin directory may contain a ``ui.json`` manifest. Discovery validates
the manifest, keeps only the components addressed to the ADMIN host and gates
them by destination:

* ``admin_system`` components exist only for the ``system`` agent and only when
  the plugin is active for ``system``;
* ``admin_agent`` components exist only for a real agent and only when the
  plugin is active for that agent;
* ``instructor`` and ``chatbot`` components are never returned here.

Plugin modules are never imported during discovery.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from app.constants import DEFAULT_SYSTEM_KEY
from app.plugins_ui.manifest import Component, PluginUiError, load_manifest

REFRESH_SECONDS_ENV = "GRINNING_CAT_PLUGIN_UI_REFRESH_SECONDS"
REQUIRE_INSTALLED_ENV = "GRINNING_CAT_PLUGIN_UI_REQUIRE_INSTALLED"
DEFAULT_REFRESH_SECONDS = 60

_CACHE: dict[tuple[str, tuple[str, ...]], tuple[float, list["AdminComponent"]]] = {}
_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class AdminComponent:
    """One ADMIN component found on disk."""

    plugin_id: str
    component: Component
    plugin_dir: Path
    module_path: Path

    @property
    def page_key(self) -> str:
        return f"plugin:{self.plugin_id}:{self.component.id}"


def _log(message: str) -> None:
    print(f"[plugin-ui] {message}")


def refresh_seconds() -> int:
    """TTL of the discovery cache; ``0`` disables the cache."""
    raw = os.getenv(REFRESH_SECONDS_ENV, str(DEFAULT_REFRESH_SECONDS))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _log(f"invalid {REFRESH_SECONDS_ENV}={raw!r}, using {DEFAULT_REFRESH_SECONDS}")
        return DEFAULT_REFRESH_SECONDS
    return max(value, 0)


def require_installed_from_env() -> bool:
    """Whether a plugin must be active in CAT to be discovered (default true)."""
    raw = os.getenv(REQUIRE_INSTALLED_ENV)
    if raw is None:
        return True
    return str(raw).strip().lower() in ("1", "true")


def clear_cache() -> None:
    """Drop the discovery cache (used by tests and by registry invalidation)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _is_active(plugin: Any) -> bool:
    local_info = getattr(plugin, "local_info", None)
    if isinstance(local_info, dict):
        return bool(local_info.get("active"))
    return bool(getattr(local_info, "active", False))


def _default_client() -> Any:
    from grinning_cat_python_sdk import GrinningCatClient

    from app.utils import build_client_configuration

    return GrinningCatClient(build_client_configuration())


def _active_plugin_ids(
    agent_id: str,
    require_installed: bool,
    client: Any | None,
) -> tuple[bool, set[str] | None]:
    """Return ``(cat_available, active_ids)``.

    ``active_ids`` is ``None`` when the check is disabled: in that case every
    discovered plugin is kept (development/QA mode, not a security gate).
    """
    if not require_installed:
        return True, None

    try:
        active_client = client if client is not None else _default_client()
        if agent_id == DEFAULT_SYSTEM_KEY:
            collection = active_client.admins.get_available_plugins()
        else:
            collection = active_client.plugins.get_available_plugins(agent_id)
    except Exception as error:  # noqa: BLE001 - CAT may be down or misconfigured
        _log(f"cannot read installed plugins for agent '{agent_id}': {error}")
        return False, None

    installed = getattr(collection, "installed", None) or []
    return True, {plugin.id for plugin in installed if _is_active(plugin)}


def _destination_allowed(destination: str, agent_id: str) -> bool:
    if destination == "admin_system":
        return agent_id == DEFAULT_SYSTEM_KEY
    if destination == "admin_agent":
        return agent_id != DEFAULT_SYSTEM_KEY
    return False


def _scan_directory(
    directory: Path,
    agent_id: str,
    active_ids: set[str] | None,
    seen_plugins: set[str],
) -> list[AdminComponent]:
    found: list[AdminComponent] = []
    try:
        children = sorted(directory.iterdir(), key=lambda path: path.name)
    except OSError as error:
        _log(f"cannot scan plugin directory '{directory}': {error}")
        return found

    for child in children:
        if not child.is_dir():
            continue
        if not (child / "ui.json").is_file():
            continue
        try:
            manifest = load_manifest(child)
        except PluginUiError as error:
            _log(f"skipping plugin '{child.name}': {error}")
            continue

        if manifest.plugin_id != child.name:
            _log(
                f"skipping plugin '{child.name}': plugin_id is '{manifest.plugin_id}'"
            )
            continue
        if manifest.plugin_id in seen_plugins:
            continue
        seen_plugins.add(manifest.plugin_id)

        if active_ids is not None and manifest.plugin_id not in active_ids:
            continue

        for component in manifest.components:
            if not _destination_allowed(component.destination, agent_id):
                continue
            found.append(
                AdminComponent(
                    plugin_id=manifest.plugin_id,
                    component=component,
                    plugin_dir=child,
                    module_path=(child / component.module).resolve(),
                )
            )
    return found


def _discover_uncached(
    dirs: Sequence[Path],
    agent_id: str,
    require_installed: bool,
    client: Any | None,
) -> list[AdminComponent]:
    cat_available, active_ids = _active_plugin_ids(agent_id, require_installed, client)
    if not cat_available:
        return []

    seen_plugins: set[str] = set()
    components: list[AdminComponent] = []
    for directory in dirs:
        if not directory.is_dir():
            continue
        components.extend(
            _scan_directory(directory, agent_id, active_ids, seen_plugins)
        )

    components.sort(
        key=lambda item: (
            item.component.order if item.component.order is not None else 0,
            item.plugin_id,
            item.component.id,
        )
    )
    return components


def discover(
    dirs: Sequence[Path | str],
    agent_id: str,
    require_installed: bool | None = None,
    client: Any | None = None,
) -> list[AdminComponent]:
    """Return the ADMIN components visible for ``agent_id``.

    ``require_installed`` defaults to ``GRINNING_CAT_PLUGIN_UI_REQUIRE_INSTALLED``
    (true). When CAT is unreachable and the check is required, the result is an
    empty list and a warning is logged.
    """
    if require_installed is None:
        require_installed = require_installed_from_env()

    resolved_dirs = [Path(directory).resolve() for directory in dirs]
    cache_key = (agent_id, tuple(sorted(str(directory) for directory in resolved_dirs)))
    ttl = refresh_seconds()

    if ttl > 0:
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < ttl:
            return list(cached[1])

    components = _discover_uncached(
        resolved_dirs, agent_id, require_installed, client
    )

    if ttl > 0:
        with _CACHE_LOCK:
            _CACHE[cache_key] = (time.monotonic(), components)
    return list(components)
