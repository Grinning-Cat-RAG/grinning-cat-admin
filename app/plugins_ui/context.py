"""Runtime context handed to a plugin ADMIN page's ``render(ctx)``.

The context is the only surface a plugin page needs: the CAT client, the
selected agent, the logged-in user, the host helpers and a logger. It is frozen
so a page cannot replace the injected dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Callable

from app.plugins_ui.registry import AdminEntry


@dataclass(frozen=True)
class PluginPageContext:
    """Everything a plugin page may use while rendering."""

    plugin_id: str
    component_id: str
    plugin_dir: Path
    client: Any
    agent_id: str
    me: dict | None
    user: dict | None
    has_access: Callable[..., bool]
    render_json_form: Callable[..., dict]
    log: Callable[..., None]


def _default_log(plugin_id: str) -> Callable[..., None]:
    def _log(message: str, *args: Any) -> None:
        print(f"[plugin-ui:{plugin_id}] {message}", *args)

    return _log


def build_context(
    entry: AdminEntry,
    agent_id: str,
    me: dict | None,
    client: Any | None = None,
    log: Callable[..., None] | None = None,
) -> PluginPageContext:
    """Build the context for ``entry``.

    The CAT client is created from the current session configuration unless one
    is supplied (tests pass a fake client).
    """
    from grinning_cat_python_sdk import GrinningCatClient

    from app.utils import build_client_configuration, has_access, render_json_form

    if client is None:
        client = GrinningCatClient(build_client_configuration())

    user = me.get("user") if isinstance(me, dict) else None
    cookie_me = me if isinstance(me, dict) else {}
    return PluginPageContext(
        plugin_id=entry.plugin_id,
        component_id=entry.component_id,
        plugin_dir=entry.plugin_dir,
        client=client,
        agent_id=agent_id,
        me=me,
        user=user,
        # The host helper takes the logged-in user positionally; bind it here so
        # a page can call ``ctx.has_access(resource, permission)``.
        has_access=partial(has_access, cookie_me=cookie_me),
        render_json_form=render_json_form,
        log=log or _default_log(entry.plugin_id),
    )
