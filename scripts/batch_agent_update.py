"""Batch-update Grinning Cat agents through the official REST API.

The tool selects agents by a case-insensitive substring of their ``agent_id``
(or every non-system agent with ``--all``) and applies the requested changes to
each of them. It is a dry-run by default: it prints the plan and exits with
code 2 without writing anything, and only ``--yes`` turns that plan into
writes. Exit codes: 0 = every operation succeeded, 1 = at least one agent or
operation failed, 2 = dry-run, abort or usage error.
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.env import get_env, get_env_bool  # noqa: E402

import yaml  # noqa: E402
from grinning_cat_python_sdk import Configuration, GrinningCatClient  # noqa: E402
from grinning_cat_python_sdk.models.api.admins import AgentOutput  # noqa: E402

OPERATION_FLAGS = ("llm", "plugin", "metadata", "name", "set", "patch")


class SelectionError(ValueError):
    """The requested agent selection cannot be satisfied."""


def select_agents(
    agents: list[AgentOutput],
    substrings: list[str],
    include_all: bool,
    exclude: list[str],
    include_internal: bool = False,
) -> list[AgentOutput]:
    """Pick the agents to update, preserving the order of ``agents``.

    ``--all`` skips the ``system`` agent and every ``__*`` internal agent,
    unless ``include_internal`` is set (the dump mode wants them).
    A substring that matches nothing is fatal: silently updating a smaller
    set than the operator asked for is the worst failure mode here.
    """
    if include_all:
        if include_internal:
            candidates = list(agents)
        else:
            candidates = [
                a for a in agents if a.agent_id != "system" and not a.agent_id.startswith("__")
            ]
    else:
        candidates = []
        for substring in substrings:
            matches = [a for a in agents if substring.lower() in a.agent_id.lower()]
            if not matches:
                raise SelectionError(f"no agent id contains {substring!r}")
            candidates.extend(matches)

    excluded = [substring.lower() for substring in exclude]
    selected = []
    seen = set()
    for agent in candidates:
        if agent.agent_id in seen:
            continue
        if any(substring in agent.agent_id.lower() for substring in excluded):
            continue
        seen.add(agent.agent_id)
        selected.append(agent)

    if not selected:
        raise SelectionError("no agents selected")
    return selected


def _agent_role(agent_id: str) -> str:
    """Classify an agent id: ``system``, a ``__*`` widget, or a normal agent."""
    if agent_id == "system":
        return "system"
    if agent_id.startswith("__"):
        return "widget"
    return "agent"


def render_plan(
    selected: list[AgentOutput],
    ops: list[str],
    metadata_keys: list[str] | None = None,
) -> list[str]:
    """One ``agent=... ops=... metadata_keys=...`` line per selected agent."""
    ops_label = ",".join(ops)
    keys_label = ",".join(metadata_keys or [])
    return [f"agent={agent.agent_id} ops={ops_label} metadata_keys={keys_label}" for agent in selected]


def _configuration() -> Configuration | None:
    """Build the SDK configuration from the environment.

    Returns ``None`` after writing to stderr when ``GRINNING_CAT_API_KEY`` is
    missing or empty: the caller turns that into exit code 2 instead of a
    traceback.
    """
    auth_key = get_env("GRINNING_CAT_API_KEY")
    if not auth_key:
        print(
            "GRINNING_CAT_API_KEY is not set: export it before running this tool",
            file=sys.stderr,
        )
        return None

    host = get_env("GRINNING_CAT_API_HOST") or "localhost"
    port = get_env("GRINNING_CAT_API_PORT") or "1865"

    return Configuration(
        host=host.replace("https://", "").replace("http://", ""),
        port=int(port),
        auth_key=auth_key,
        secure_connection=get_env_bool("GRINNING_CAT_API_SECURE_CONNECTION"),
    )


def default_client_factory() -> GrinningCatClient | None:
    """Build the real client, or ``None`` when the API key is missing."""
    configuration = _configuration()
    if configuration is None:
        return None
    return GrinningCatClient(configuration)


FACTORY_CATEGORIES = {
    "llm": ("large_language_model", "get_large_language_model_settings", "put_large_language_model_settings"),
    "chunking": ("chunker", "get_chunker_settings", "put_chunker_settings"),
    "vector_database": ("vector_database", "get_vector_database_settings", "put_vector_database_settings"),
    "auth_handler": ("auth_handler", "get_auth_handler_settings", "put_auth_handler_settings"),
    "agentic_workflow": ("agentic_workflow", "get_agentic_workflow_settings", "put_agentic_workflow_settings"),
    "context_retriever": ("context_retriever", "get_context_retriever_settings", "put_context_retriever_settings"),
    "file_manager": ("file_manager", "get_file_manager_settings", "put_file_manager_settings"),
}


def merge_factory_setting(
    client: Any,
    category: str,
    name: str,
    patch: dict[str, Any],
    agent_id: str,
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read-merge-write one factory setting and return the merged value.

    The core PUT replaces the whole value dict, so the current value must be
    read first and merged with the patch: a blind PUT would drop every key the
    patch does not mention. ``current`` lets the caller supply a value it has
    already read (the batch runner reads every state once, before any write).
    """
    try:
        attribute, getter_name, putter_name = FACTORY_CATEGORIES[category]
    except KeyError:
        valid = ", ".join(sorted(FACTORY_CATEGORIES))
        raise ValueError(f"unknown factory category {category!r}; valid categories: {valid}") from None

    endpoint = getattr(client, attribute)
    base: dict[str, Any] = (
        getattr(endpoint, getter_name)(name, agent_id).value if current is None else current
    )
    merged = _deep_merge(base, patch)
    getattr(endpoint, putter_name)(name, agent_id, merged)
    return merged


def set_nested(d: dict[str, Any], dotted_key: str, value: Any) -> None:
    """Set ``dotted_key`` (``a.b.c``) in ``d``, creating missing branches."""
    parts = dotted_key.split(".")
    if not dotted_key or any(not part for part in parts):
        raise ValueError(f"invalid dotted key {dotted_key!r}")

    target = d
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            child = {}
            target[part] = child
        target = child
    target[parts[-1]] = value


@dataclass(frozen=True)
class Operation:
    """One requested change, applied to every selected agent."""

    kind: str
    name: str
    payload: Any
    category: str = ""


SYSTEM_WIDE_CATEGORIES = {"embedder", "ingestion"}
VALID_CATEGORIES = {"metadata", "plugins", *FACTORY_CATEGORIES, *SYSTEM_WIDE_CATEGORIES}
_OPERATION_ORDER = {"metadata": 0, "llm": 1, "plugin": 2, "settings": 3}
_PLUGIN_STATES = {"on": True, "true": True, "1": True, "off": False, "false": False, "0": False}
_YAML_SUFFIXES = {".yml", ".yaml"}

#: ``category -> (client attribute, list getter)`` for the per-agent factories.
FACTORY_LIST_GETTERS = {
    "llm": ("large_language_model", "get_large_language_models_settings"),
    "chunking": ("chunker", "get_chunkers_settings"),
    "vector_database": ("vector_database", "get_vector_databases_settings"),
    "auth_handler": ("auth_handler", "get_auth_handlers_settings"),
    "agentic_workflow": ("agentic_workflow", "get_agentic_workflows_settings"),
    "context_retriever": ("context_retriever", "get_context_retrievers_settings"),
    "file_manager": ("file_manager", "get_file_managers_settings"),
}

#: Every category a dump can contain, in the order the header lists them.
DUMP_CATEGORIES = [
    "metadata",
    *FACTORY_LIST_GETTERS,
    "plugins",
    "active_plugins",
    "embedder",
    "ingestion",
]

_SECRET_KEY = re.compile(r"(api[_-]?key|secret|password|token)", re.IGNORECASE)


def _parse_value(raw: str) -> Any:
    """Decode a CLI value as JSON, falling back to the raw string."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def parse_llm(items: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """Parse ``NAME=JSON`` items; the JSON side must be an object."""
    parsed = []
    for item in items:
        model, separator, raw = item.partition("=")
        if not separator or not model:
            raise ValueError(f"invalid --llm {item!r}: expected NAME=JSON")
        try:
            patch = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid --llm {item!r}: {exc}") from None
        if not isinstance(patch, dict):
            raise ValueError(f"invalid --llm {item!r}: the JSON value must be an object")
        parsed.append((model, patch))
    return parsed


def parse_plugins(items: list[str]) -> list[tuple[str, bool]]:
    """Parse ``NAME=on|off`` items, accepting true/false/1/0 as well."""
    parsed = []
    for item in items:
        name, separator, raw = item.partition("=")
        if not separator or not name:
            raise ValueError(f"invalid --plugin {item!r}: expected NAME=on|off")
        desired = _PLUGIN_STATES.get(raw.strip().lower())
        if desired is None:
            raise ValueError(f"invalid --plugin {item!r}: expected NAME=on|off")
        parsed.append((name, desired))
    return parsed


def parse_metadata_pairs(items: list[str]) -> dict[str, Any]:
    """Parse ``KEY=VALUE`` items, decoding VALUE as JSON when possible."""
    updates = {}
    for item in items:
        key, separator, raw = item.partition("=")
        if not separator or not key:
            raise ValueError(f"invalid --metadata {item!r}: expected KEY=VALUE")
        updates[key] = _parse_value(raw)
    return updates


def parse_set(items: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
    """Parse ``CATEGORY:NAME:KEY=VALUE`` items into per-target patches."""
    patches: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        parts = item.split(":", 2)
        if len(parts) != 3:
            raise ValueError(f"invalid --set {item!r}: expected CATEGORY:NAME:KEY=VALUE")
        category, name, assignment = parts
        key, separator, raw = assignment.partition("=")
        if not category or not name or not separator or not key:
            raise ValueError(f"invalid --set {item!r}: expected CATEGORY:NAME:KEY=VALUE")
        patch = patches.setdefault((category, name), {})
        set_nested(patch, key, _parse_value(raw))
    return patches


def _yaml_documents(text: str) -> list[Any]:
    """Parse every YAML document in ``text`` with the non-typing ``BaseLoader``.

    ``BaseLoader`` keeps every scalar a string, so ``on``, ``0123``, ``1.10`` and
    dates reach :func:`_parse_value` verbatim. Anchors and aliases are allowed:
    the patch file is operator-trusted input, never untrusted user content.
    """
    try:
        documents = list(yaml.load_all(text, Loader=yaml.BaseLoader))
    except yaml.YAMLError as exc:
        raise ValueError(str(exc)) from None
    if not documents:
        raise ValueError("the file is empty")
    if len(documents) > 1:
        raise ValueError("the file contains multiple documents separated by ---")
    return documents


def _parse_leaves(node: Any) -> Any:
    """Decode every scalar leaf with :func:`_parse_value`, keeping the shape."""
    if isinstance(node, dict):
        return {key: _parse_leaves(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_parse_leaves(item) for item in node]
    if isinstance(node, str):
        return _parse_value(node)
    return node


def _comment_syntax_hint(text: str) -> str:
    """Point at the YAML comment character when a line starts with ``//``."""
    if any(line.lstrip().startswith("//") for line in text.splitlines()):
        return " (comment syntax in patch files is '#', not '//')"
    return ""


def load_patch(path: str) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, bool]]:
    """Load a YAML patch file into per-target patches and ``active_plugins``.

    The file must use a ``.yml`` or ``.yaml`` suffix (case-insensitive) and is a
    ``{category: {name: patch}}`` mapping with one extra top-level key,
    ``active_plugins``: ``{name: on|off}``. Comments start with ``#``; anchors
    and aliases are allowed because the file is operator-trusted. Returns
    ``(patches, active_plugins)``.
    """
    if Path(path).suffix.lower() not in _YAML_SUFFIXES:
        raise ValueError(
            f"invalid --patch {path!r}: patch files must be YAML (.yml or .yaml); "
            "JSON and JSONC are not supported"
        )
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read --patch {path!r}: {exc}") from None
    try:
        documents = _yaml_documents(raw)
    except ValueError as exc:
        raise ValueError(f"invalid --patch {path!r}: {exc}{_comment_syntax_hint(raw)}") from None

    data = documents[0]
    if not isinstance(data, dict):
        raise ValueError(f"invalid --patch {path!r}: the top level must be a mapping")

    patches: dict[tuple[str, str], dict[str, Any]] = {}
    active_plugins: dict[str, bool] = {}
    for category, entries in data.items():
        if not isinstance(entries, dict):
            raise ValueError(f"invalid --patch {path!r}: category {category!r} must be a mapping")
        if category == "active_plugins":
            for name, state in entries.items():
                normalized = state.strip().lower() if isinstance(state, str) else ""
                if normalized not in _PLUGIN_STATES:
                    raise ValueError(
                        f"invalid --patch {path!r}: active_plugins:{name} must be on|off "
                        f"(true/false/1/0 also accepted), got {state!r}"
                    )
                active_plugins[name] = _PLUGIN_STATES[normalized]
            continue
        converted = _parse_leaves(entries)
        if category == "metadata":
            patches[("metadata", "")] = converted
            continue
        for name, patch in converted.items():
            if not isinstance(patch, dict):
                raise ValueError(f"invalid --patch {path!r}: {category}:{name} must be a mapping")
            patches[(category, name)] = patch
    return patches, active_plugins


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _validate_categories(
    patches: dict[tuple[str, str], dict[str, Any]], allow_system_wide: bool
) -> None:
    for category, _name in patches:
        if category in SYSTEM_WIDE_CATEGORIES:
            if not allow_system_wide:
                raise ValueError(
                    f"{category} is system-wide: it re-embeds every agent; "
                    "pass --allow-system-wide to apply it"
                )
            continue
        if category not in VALID_CATEGORIES:
            valid = ", ".join(sorted(VALID_CATEGORIES))
            raise ValueError(f"unknown category {category!r}; valid categories: {valid}")


def _build_operations(args: argparse.Namespace) -> tuple[list[Operation], list[Operation]]:
    """Parse every operation before the first write, so a typo aborts the run.

    Returns ``(per_agent, system_wide)``: system-wide categories are applied
    once for the whole deployment, never once per selected agent.
    """
    patches, file_plugins = load_patch(args.patch) if args.patch else ({}, {})
    for key, patch in parse_set(args.set or []).items():
        patches[key] = _deep_merge(patches.get(key, {}), patch)

    metadata_payload: dict[str, Any] = {}
    for (category, _name), patch in patches.items():
        if category == "metadata":
            metadata_payload = _deep_merge(metadata_payload, patch)
    metadata_payload = _deep_merge(metadata_payload, parse_metadata_pairs(args.metadata or []))
    if args.name is not None:
        metadata_payload["name"] = args.name

    _validate_categories(patches, args.allow_system_wide)

    operations = []
    system_wide = []
    if metadata_payload:
        operations.append(Operation(kind="metadata", name="", payload=metadata_payload))
    for model, patch in parse_llm(args.llm or []):
        operations.append(Operation(kind="llm", name=model, payload=patch))
    cli_plugins = parse_plugins(args.plugin or [])
    for name, desired in cli_plugins:
        operations.append(Operation(kind="plugin", name=name, payload=desired))
    # An explicit --plugin WINS over the same name in the patch file.
    cli_plugin_names = {name for name, _desired in cli_plugins}
    for name, desired in file_plugins.items():
        if name not in cli_plugin_names:
            operations.append(Operation(kind="plugin", name=name, payload=desired))
    for (category, name), patch in patches.items():
        if category == "metadata":
            continue
        if category in SYSTEM_WIDE_CATEGORIES:
            system_wide.append(Operation(kind="system", category=category, name=name, payload=patch))
            continue
        operations.append(Operation(kind="settings", category=category, name=name, payload=patch))
    return operations, system_wide


def update_metadata(
    client: Any,
    agent: AgentOutput,
    updates: dict[str, Any],
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge ``updates`` into the agent metadata and PUT the whole dict.

    The core replaces the metadata dict, so the merge is mandatory: a blind
    PUT would drop every key the update does not mention. ``current`` lets the
    caller supply a value it has already read.
    """
    base = dict(agent.metadata) if current is None else current
    merged = _deep_merge(base, updates)
    result = client.utils.put_agent(agent.agent_id, merged)
    if not result.updated:
        raise RuntimeError(f"updated=false for {agent.agent_id}")
    return merged


def reconcile_plugin(
    client: Any,
    agent_id: str,
    name: str,
    desired: bool,
    active: bool | None = None,
) -> str:
    """Bring one plugin to ``desired`` state, tolerating the core's flip toggle.

    The core endpoint flips the active flag instead of setting it, so the
    current state is read first and the result is verified afterwards: a
    repeated command must never toggle twice. ``active`` lets the caller
    supply a state it has already read.
    """
    if active is None:
        collection = client.plugins.get_available_plugins(agent_id)
        item = next((plugin for plugin in collection.installed if plugin.id == name), None)
        if item is None:
            raise RuntimeError(f"plugin {name!r} is not installed for agent {agent_id!r}")
        active = bool(item.local_info.get("active", False))

    if active == desired:
        return "already-on" if desired else "already-off"

    client.plugins.put_toggle_plugin(name, agent_id)

    collection = client.plugins.get_available_plugins(agent_id)
    item = next((plugin for plugin in collection.installed if plugin.id == name), None)
    if item is None:
        raise RuntimeError(f"plugin {name!r} disappeared from agent {agent_id!r} after toggling")
    if bool(item.local_info.get("active", False)) != desired:
        raise RuntimeError(f"toggle did not change state for plugin {name!r} on agent {agent_id!r}")

    return "toggled-on" if desired else "toggled-off"


def _operation_label(operation: Operation) -> str:
    if operation.kind == "settings":
        return f"settings:{operation.category}:{operation.name}"
    if operation.kind == "system":
        return f"system:{operation.category}:{operation.name}"
    if operation.kind in ("llm", "plugin"):
        return f"{operation.kind}:{operation.name}"
    return operation.kind


def _apply_operation(client: Any, agent: AgentOutput, operation: Operation, current: Any) -> str:
    if operation.kind == "metadata":
        update_metadata(client, agent, operation.payload, current=current)
        return "updated"
    if operation.kind == "llm":
        merge_factory_setting(
            client, "llm", operation.name, operation.payload, agent.agent_id, current=current
        )
        return "merged"
    if operation.kind == "plugin":
        return reconcile_plugin(client, agent.agent_id, operation.name, operation.payload, active=current)
    if operation.kind == "settings":
        if operation.category == "plugins":
            client.plugins.put_plugin_settings(
                operation.name, agent.agent_id, _deep_merge(current, operation.payload)
            )
            return "merged"
        merge_factory_setting(
            client, operation.category, operation.name, operation.payload, agent.agent_id, current=current
        )
        return "merged"
    raise NotImplementedError(f"operation {operation.kind!r} is not implemented yet")


def _apply_system_wide(client: Any, operation: Operation) -> str:
    """Read-merge-write one system-scoped factory setting (embedder/ingestion)."""
    if operation.category == "embedder":
        current = client.embedder.get_embedder_settings(operation.name).value
        client.embedder.put_embedder_settings(operation.name, _deep_merge(current, operation.payload))
        return "merged"
    if operation.category == "ingestion":
        current = client.ingestion.get_ingestion_settings(operation.name).value
        client.ingestion.put_ingestion_settings(operation.name, _deep_merge(current, operation.payload))
        return "merged"
    raise NotImplementedError(f"system-wide category {operation.category!r} is not implemented yet")


_READ_FAILED = object()


def _plugin_active(client: Any, agent_id: str, name: str) -> bool:
    collection = client.plugins.get_available_plugins(agent_id)
    item = next((plugin for plugin in collection.installed if plugin.id == name), None)
    return bool(item.local_info.get("active", False)) if item is not None else False


def _read_operation_state(client: Any, agent: AgentOutput, operation: Operation) -> Any:
    """Read the current value of one operation, before any write of the run."""
    if operation.kind == "metadata":
        return dict(agent.metadata)
    if operation.kind == "llm":
        return dict(
            client.large_language_model.get_large_language_model_settings(
                operation.name, agent.agent_id
            ).value
        )
    if operation.kind == "plugin":
        collection = client.plugins.get_available_plugins(agent.agent_id)
        item = next((plugin for plugin in collection.installed if plugin.id == operation.name), None)
        if item is None:
            raise RuntimeError(
                f"plugin {operation.name!r} is not installed for agent {agent.agent_id!r}"
            )
        return bool(item.local_info.get("active", False))
    if operation.kind == "settings":
        if operation.category == "plugins":
            return dict(client.plugins.get_plugin_settings(operation.name, agent.agent_id).value)
        attribute, getter_name, _putter_name = FACTORY_CATEGORIES[operation.category]
        endpoint = getattr(client, attribute)
        return dict(getattr(endpoint, getter_name)(operation.name, agent.agent_id).value)
    raise NotImplementedError(f"operation {operation.kind!r} is not implemented yet")


def _read_factory_selected(
    client: Any, category: str, agent_id: str | None
) -> tuple[str, dict[str, Any]]:
    """Read the *selected* configuration of one factory category.

    Only the selected configuration is dumped: the core's
    ``PUT /<category>/settings/{name}`` selects that name, so dumping the
    alternatives too would turn a reload into a silent selection change.
    """
    if category in FACTORY_LIST_GETTERS:
        attribute, getter_name = FACTORY_LIST_GETTERS[category]
        response = getattr(getattr(client, attribute), getter_name)(agent_id)
    elif category in SYSTEM_WIDE_CATEGORIES:
        if agent_id is not None:
            raise ValueError(f"{category} is system-wide and cannot be read per agent")
        getter_name = (
            "get_embedders_settings" if category == "embedder" else "get_ingestions_settings"
        )
        response = getattr(getattr(client, category), getter_name)()
    else:
        raise ValueError(f"unknown factory category {category!r}")

    selected = getattr(response, "selected_configuration", "") or ""
    if selected:
        for setting in response.settings:
            if setting.name == selected:
                return selected, dict(setting.value)
    raise ValueError(f"no selected configuration for {category} on {agent_id or 'system'}")


def _strip_secrets(value: Any) -> Any:
    """Drop secret-looking keys at every level; never substitute a placeholder."""
    if isinstance(value, dict):
        return {
            key: _strip_secrets(item)
            for key, item in value.items()
            if not _SECRET_KEY.search(str(key))
        }
    if isinstance(value, list):
        return [_strip_secrets(item) for item in value]
    return value


def _dump_agent_config(
    client: Any,
    agent: AgentOutput,
    categories: list[str],
    include_secrets: bool,
) -> dict[str, Any]:
    """Build the patch-shaped payload for one agent from the live SDK state."""
    role = _agent_role(agent.agent_id)
    payload: dict[str, Any] = {}
    if "metadata" in categories:
        payload["metadata"] = dict(agent.metadata)
    for category in categories:
        if category in FACTORY_LIST_GETTERS:
            name, value = _read_factory_selected(client, category, agent.agent_id)
            payload[category] = {name: value}
    if role == "system":
        for category in ("embedder", "ingestion"):
            if category in categories:
                name, value = _read_factory_selected(client, category, None)
                payload[category] = {name: value}
    if "plugins" in categories:
        collection = client.plugins.get_available_plugins(agent.agent_id)
        payload["plugins"] = {
            plugin.id: dict(client.plugins.get_plugin_settings(plugin.id, agent.agent_id).value)
            for plugin in collection.installed
        }
    if "active_plugins" in categories:
        collection = client.plugins.get_available_plugins(agent.agent_id)
        payload["active_plugins"] = {
            plugin.id: "on" if plugin.local_info.get("active") else "off"
            for plugin in collection.installed
        }
    if not include_secrets:
        payload = _strip_secrets(payload)
    return payload


def _build_snapshot(
    agent: AgentOutput,
    operations: list[Operation],
    states: list[Any],
    client: Any,
) -> dict[str, Any]:
    """Collect the pre-write state of everything this run touches for one agent."""
    snapshot: dict[str, Any] = {
        "agent_id": agent.agent_id,
        "metadata": dict(agent.metadata),
        "llm": {},
        "plugins": {},
        "categories": {},
    }
    for operation, state in zip(operations, states):
        if state is _READ_FAILED:
            continue
        if operation.kind == "llm":
            snapshot["llm"][operation.name] = dict(state)
        elif operation.kind == "plugin":
            settings = client.plugins.get_plugin_settings(operation.name, agent.agent_id).value
            snapshot["plugins"][operation.name] = {"active": bool(state), "settings": dict(settings)}
        elif operation.kind == "settings":
            if operation.category == "plugins":
                snapshot["plugins"][operation.name] = {
                    "active": _plugin_active(client, agent.agent_id, operation.name),
                    "settings": dict(state),
                }
            else:
                snapshot["categories"].setdefault(operation.category, {})[operation.name] = dict(state)
    return snapshot


_SNAPSHOT_LOCK = threading.Lock()

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None


@contextlib.contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """Exclusive OS-level lock on ``path``; a no-op where ``fcntl`` is missing."""
    if fcntl is None:  # pragma: no cover - non-POSIX platforms
        yield
        return
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` through a same-directory temporary file."""
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2))
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _write_snapshot(directory: Path, snapshot: dict[str, Any], argv: list[str]) -> None:
    """Write one agent snapshot atomically and update the shared manifest."""
    directory.mkdir(parents=True, exist_ok=True)
    agent_id = snapshot["agent_id"]
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", agent_id)
    filename = f"{safe_id}-{hashlib.sha1(agent_id.encode()).hexdigest()[:8]}.json"
    _atomic_write_json(directory / filename, snapshot)

    manifest_path = directory / "manifest.json"
    with _SNAPSHOT_LOCK, _exclusive_lock(directory / ".manifest.lock"):
        manifest: dict[str, Any]
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        else:
            manifest = {"created_at": _utc_now(), "argv": list(argv), "agents": []}
        if agent_id not in manifest["agents"]:
            manifest["agents"].append(agent_id)
        _atomic_write_json(manifest_path, manifest)


def _dump_header(
    agent: Any,
    role: str,
    categories: list[str],
    selected: dict[str, str],
    argv: list[str],
    host: str,
    secret_policy: str,
    warnings: list[str],
) -> str:
    """Render the YAML comment block that documents one dump file."""
    agent_id = getattr(agent, "agent_id", agent)
    selected_label = ", ".join(f"{category}={name}" for category, name in selected.items())
    warnings_label = ", ".join(warnings) if warnings else "none"
    lines = [
        "# dump-format: patch-v1",
        f"# generated-at: {_utc_now()}",
        "# generator: batch_agent_update.py --dump",
        f"# agent-id: {agent_id}",
        f"# agent-role: {role}",
        f"# categories: {', '.join(categories)}",
        f"# selected: {selected_label}",
        f"# argv: {json.dumps(list(argv))}",
        f"# source-host: {host}",
        f"# secret-policy: {secret_policy}",
        f"# warnings: {warnings_label}",
    ]
    return "\n".join(lines) + "\n"


def _render_dump_yaml(payload: dict[str, Any], header: str) -> str:
    """Prepend the metadata header to the YAML body of one dump file."""
    body = yaml.safe_dump(
        payload,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    return header + body


def _atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write ``text`` to ``path`` atomically with ``mode`` (default ``0600``)."""
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _dump_filename(agent_id: str) -> str:
    """A filesystem-safe, collision-free ``.yml`` name for one agent dump."""
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", agent_id)
    return f"{safe_id}-{hashlib.sha1(agent_id.encode()).hexdigest()[:8]}.yml"


def _run_agent(
    client: Any,
    agent: AgentOutput,
    ordered: list[Operation],
    snapshot_dir: Path | None,
    argv: list[str],
) -> dict[str, Any]:
    """Read every state, snapshot, then write: one agent never aborts the run."""
    result: dict[str, Any] = {"agent_id": agent.agent_id, "ok": True, "ops": [], "error": None}
    try:
        states: list[Any] = []
        for operation in ordered:
            try:
                states.append(_read_operation_state(client, agent, operation))
            except Exception as exc:  # noqa: BLE001 - one operation must not abort the agent
                states.append(_READ_FAILED)
                result["ops"].append(
                    {"op": _operation_label(operation), "status": "failed", "detail": str(exc)}
                )
                result["ok"] = False

        if snapshot_dir is not None:
            try:
                snapshot = _build_snapshot(agent, ordered, states, client)
                _write_snapshot(snapshot_dir, snapshot, argv)
            except Exception as exc:  # noqa: BLE001 - a failed snapshot must block every write
                result["ok"] = False
                result["error"] = f"snapshot failed: {exc}"
                return result

        for operation, current in zip(ordered, states):
            if current is _READ_FAILED:
                continue
            try:
                detail = _apply_operation(client, agent, operation, current)
            except Exception as exc:  # noqa: BLE001 - one operation must not abort the agent
                result["ops"].append(
                    {"op": _operation_label(operation), "status": "failed", "detail": str(exc)}
                )
                result["ok"] = False
            else:
                result["ops"].append({"op": _operation_label(operation), "status": "ok", "detail": detail})
    except Exception as exc:  # noqa: BLE001 - one agent must not abort the run
        result["ok"] = False
        result["error"] = str(exc)
    return result


def execute_batch(
    client: Any,
    selected: list[AgentOutput],
    operations: list[Operation] | dict[str, list[Operation]],
    system_wide: list[Operation] | None = None,
    snapshot_dir: Path | None = None,
    argv: list[str] | None = None,
    max_workers: int = 1,
    missing: set[str] | None = None,
) -> dict[str, Any]:
    """Apply every operation, isolating failures per agent and per operation.

    ``operations`` is either one list applied to every agent or a per-agent
    mapping (used by ``--restore``). System-wide operations run exactly once,
    after the per-agent loop.
    """
    argv = list(argv or [])
    if isinstance(operations, dict):
        per_agent = operations

        def ordered_for(agent: AgentOutput) -> list[Operation]:
            return sorted(per_agent.get(agent.agent_id, []), key=lambda op: _OPERATION_ORDER[op.kind])

    else:
        ordered = sorted(operations, key=lambda operation: _OPERATION_ORDER[operation.kind])

        def ordered_for(agent: AgentOutput) -> list[Operation]:
            return ordered

    def run(agent: AgentOutput) -> dict[str, Any]:
        if missing and agent.agent_id in missing:
            return {
                "agent_id": agent.agent_id,
                "ok": False,
                "ops": [],
                "error": "agent not found on the server",
            }
        return _run_agent(client, agent, ordered_for(agent), snapshot_dir, argv)

    if max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(run, selected))
    else:
        results = [run(agent) for agent in selected]

    system_results = []
    for operation in system_wide or []:
        try:
            detail = _apply_system_wide(client, operation)
        except Exception as exc:  # noqa: BLE001 - one system-wide op must not abort the run
            system_results.append(
                {"op": _operation_label(operation), "status": "failed", "detail": str(exc)}
            )
        else:
            system_results.append({"op": _operation_label(operation), "status": "ok", "detail": detail})

    ok = sum(1 for result in results if result["ok"])
    skipped = sum(
        1
        for result in results
        if result["ok"]
        and result["ops"]
        and all(operation["detail"].startswith("already-") for operation in result["ops"])
    )
    return {
        "agents": results,
        "system_wide": system_results,
        "summary": {
            "selected": len(selected),
            "ok": ok,
            "failed": len(results) - ok,
            "skipped": skipped,
        },
    }


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_backup_dir() -> Path:
    return Path(f"agent-backup-{_utc_stamp()}")


def _load_snapshots(path: str) -> list[dict[str, Any]]:
    """Load every snapshot in a file or directory, skipping the manifest."""
    target = Path(path)
    if target.is_dir():
        files = sorted(
            item
            for item in target.glob("*.json")
            if item.name != "manifest.json" and not item.name.startswith(".")
        )
    else:
        files = [target]

    snapshots = []
    seen: set[str] = set()
    for file in files:
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read snapshot {str(file)!r}: {exc}") from None
        if not isinstance(data, dict) or "agent_id" not in data:
            raise ValueError(f"invalid snapshot {str(file)!r}: missing agent_id")
        agent_id = data["agent_id"]
        if agent_id in seen:
            print(
                f"duplicate snapshot for agent {agent_id!r} in {str(file)!r}: keeping the first",
                file=sys.stderr,
            )
            continue
        seen.add(agent_id)
        snapshots.append(data)
    if not snapshots:
        raise ValueError(f"no snapshots found in {path!r}")
    return snapshots


def _restore_operations(snapshot: dict[str, Any]) -> list[Operation]:
    """Turn one snapshot back into the operations that recreate it."""
    operations = [Operation(kind="metadata", name="", payload=dict(snapshot.get("metadata", {})))]
    for model, value in snapshot.get("llm", {}).items():
        operations.append(Operation(kind="llm", name=model, payload=dict(value)))
    for name, entry in snapshot.get("plugins", {}).items():
        operations.append(
            Operation(
                kind="settings",
                category="plugins",
                name=name,
                payload=dict(entry.get("settings", {})),
            )
        )
        operations.append(Operation(kind="plugin", name=name, payload=bool(entry.get("active", False))))
    for category, entries in snapshot.get("categories", {}).items():
        for name, value in entries.items():
            operations.append(Operation(kind="settings", category=category, name=name, payload=dict(value)))
    return operations


def _report(envelope: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(envelope, indent=2))
        return

    for result in envelope["agents"]:
        for operation in result["ops"]:
            line = f"[batch] agent={result['agent_id']} op={operation['op']} result={operation['status']}"
            if operation["status"] == "failed":
                line += f" reason={operation['detail']}"
            print(line)
        if result["error"]:
            print(f"[batch] agent={result['agent_id']} error={result['error']}", file=sys.stderr)
    for operation in envelope["system_wide"]:
        line = f"[batch] system-wide op={operation['op']} result={operation['status']}"
        if operation["status"] == "failed":
            line += f" reason={operation['detail']}"
        print(line)


def _exit_code(envelope: dict[str, Any]) -> int:
    if any(not result["ok"] for result in envelope["agents"]):
        return 1
    if any(operation["status"] != "ok" for operation in envelope["system_wide"]):
        return 1
    return 0


_EXAMPLES = """\
LLM configuration names: --llm and --set llm:<name> take the configuration name
reported by GET /llm/settings (the JSON-schema title, i.e. the class name such as
LLMOpenRouterConfig). The friendly label shown in ADMIN is humanReadableName and
is NOT accepted. An unknown name fails with "X not supported. Must be one of
[...]", which lists every valid name.

examples:
  # 1. Select an LLM configuration and set its model. The OpenRouter configuration used from
  #    ADMIN takes its model parameters from openrouter.ai, so model must be a valid
  #    OpenRouter model id; run the dry-run first and read the plan before adding --yes.
  uv run python scripts/batch_agent_update.py --agent corso \\
    --set llm:LLMOpenRouterConfig:model=anthropic/claude-3.5-sonnet
  #    The same change through --llm, which merges an object into the configuration:
  uv run python scripts/batch_agent_update.py --agent corso \\
    --llm LLMOpenRouterConfig='{"model": "anthropic/claude-3.5-sonnet"}'

  # 2. Set api_key of an LLM configuration; any other key (temperature, base_url, ...) works the same way.
  #    Prefer --set (or an env var, or a patch file you do not commit); never commit a real key.
  uv run python scripts/batch_agent_update.py --agent corso --set llm:LLMOpenRouterConfig:api_key=sk-...
  uv run python scripts/batch_agent_update.py --agent corso --patch examples/patch-llm-openrouter.yml

  # 3. Add or overwrite an agent metadata key; --name is the shortcut for --metadata name=VALUE.
  #    Metadata keys are literal: dotted nesting is only available through --set.
  uv run python scripts/batch_agent_update.py --agent corso --metadata label=production
  uv run python scripts/batch_agent_update.py --agent corso --name "New name"

  # 4. Enable or disable a plugin. The current state is read first, so a repeated run is
  #    idempotent and never toggles twice.
  uv run python scripts/batch_agent_update.py --agent corso --plugin my_plugin=on
  uv run python scripts/batch_agent_update.py --agent corso --plugin my_plugin=off

  # 5. Modify a plugin setting. Values are deep-merged and dotted keys create nested branches.
  uv run python scripts/batch_agent_update.py --agent corso --set plugins:my_plugin:options.enabled=true

  # 6. Everything else.
  #    A patch file is YAML: one document, '#' comments, and a value containing '#' must be
  #    quoted, e.g. api_key: "sk-abc#def". Copy and adapt:
  #      examples/patch-llm-openrouter.yml  one LLM change on OpenRouter
  #      examples/patch-full-agent.yml      metadata + all settings + active_plugins
  #    active_plugins takes on/off (true/false/1/0 also work) and reconciles like
  #    --plugin; an explicit --plugin NAME=on|off wins over the same name in the file.
  uv run python scripts/batch_agent_update.py --agent corso --patch examples/patch-full-agent.yml
  uv run python scripts/batch_agent_update.py --agent corso --patch examples/patch-llm-openrouter.yml
  #    Several agents at once, minus the test ones.
  uv run python scripts/batch_agent_update.py --agent corso --agent lab --exclude test --metadata label=production
  #    Every non-system agent, writing with a before-snapshot.
  uv run python scripts/batch_agent_update.py --all --exclude system --yes --backup-out ./backup --plugin my_plugin=on
  #    Restore a snapshot file or directory written by --backup-out.
  uv run python scripts/batch_agent_update.py --restore ./backup --yes
  #    System-wide categories (embedder, ingestion) re-embed every agent: opt in explicitly.
  uv run python scripts/batch_agent_update.py --all --set embedder:openai:key=sk-... --allow-system-wide
  #    Parallel workers, one task per agent.
  uv run python scripts/batch_agent_update.py --all --plugin my_plugin=on --max-workers 4

  # Safety: dry-run is the default; --yes writes; exit codes 0 = ok, 1 = failure, 2 = dry-run/abort.
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="batch_agent_update.py",
        description="Batch-update Grinning Cat agents through the official REST API (dry-run by default).",
        epilog=_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--agent",
        action="append",
        metavar="SUBSTRING",
        help="agent id or a case-insensitive substring of it (repeatable)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="every agent except system and __*",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        metavar="SUBSTRING",
        help="drop agents whose id contains this substring, case-insensitive (repeatable)",
    )
    parser.add_argument(
        "--llm",
        action="append",
        metavar="NAME=JSON",
        help="merge a JSON object into the LLM configuration NAME from GET /llm/settings (repeatable)",
    )
    parser.add_argument(
        "--plugin",
        action="append",
        metavar="NAME=on|off",
        help="enable or disable a plugin (repeatable)",
    )
    parser.add_argument(
        "--metadata",
        action="append",
        metavar="KEY=VALUE",
        help="merge one metadata key (repeatable)",
    )
    parser.add_argument(
        "--name",
        metavar="VALUE",
        help="shortcut for --metadata name=VALUE",
    )
    parser.add_argument(
        "--set",
        action="append",
        metavar="CATEGORY:NAME:KEY=VALUE",
        help="patch one factory setting (repeatable)",
    )
    parser.add_argument(
        "--patch",
        metavar="FILE",
        help="YAML file with per-category patches and active_plugins",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="apply the changes (default: dry-run)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the plan as JSON",
    )
    parser.add_argument(
        "--backup-out",
        metavar="DIR",
        help="directory for the before-snapshot",
    )
    parser.add_argument(
        "--restore",
        metavar="PATH",
        help="restore a snapshot file or directory",
    )
    parser.add_argument(
        "--allow-system-wide",
        action="store_true",
        help="allow system-wide categories such as embedder",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=1,
        metavar="INT",
        help="parallel workers, one task per agent (default: 1)",
    )
    parser.add_argument(
        "--dump",
        metavar="DIR",
        help="write the selected agents' configuration as YAML files into DIR",
    )
    parser.add_argument(
        "--dump-only",
        action="append",
        metavar="CATEGORY",
        help="dump only this category (repeatable; requires --dump)",
    )
    parser.add_argument(
        "--dump-secrets",
        action="store_true",
        help="include secret-looking fields in the dump (default: omitted)",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="compare the live configuration with the existing dump in DIR (requires --dump)",
    )
    parser.add_argument(
        "--dump-manifest-only",
        action="store_true",
        help="write only manifest.yml, without per-agent files (requires --dump)",
    )
    return parser


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject impossible command lines before any client is built."""
    if args.dump:
        conflicting = [flag for flag in OPERATION_FLAGS if getattr(args, flag)]
        if args.restore:
            conflicting.append("restore")
        if args.yes:
            conflicting.append("yes")
        if conflicting:
            parser.error(f"--dump cannot be combined with --{conflicting[0]}")
        if not args.agent and not args.all:
            parser.error("--dump requires --agent or --all")
        if args.diff and not Path(args.dump).is_dir():
            parser.error(f"--diff requires an existing directory: {args.dump!r}")
        return

    dump_modifiers = [
        name
        for name in ("dump_only", "dump_secrets", "diff", "dump_manifest_only")
        if getattr(args, name)
    ]
    if dump_modifiers:
        parser.error(f"--{dump_modifiers[0].replace('_', '-')} requires --dump")

    if args.restore:
        conflicting = [flag for flag in OPERATION_FLAGS if getattr(args, flag)]
        if conflicting:
            parser.error(f"--restore cannot be combined with --{conflicting[0]}")
        return

    if not any(getattr(args, flag) for flag in OPERATION_FLAGS):
        parser.error(
            "at least one operation is required: --llm, --plugin, --metadata, --name, --set or --patch"
        )
    if not args.agent and not args.all:
        parser.error("--agent or --all is required")


def main(argv: list[str] | None = None, client_factory=default_client_factory) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Validate the command line and every operation before credentials: a
    # malformed patch must name the format error, not a missing key. --help and
    # argparse usage errors therefore work without GRINNING_CAT_API_KEY.
    _validate(parser, args)

    if args.max_workers < 1:
        parser.error("--max-workers must be at least 1")

    snapshots = None
    system_wide: list[Operation] = []
    built_operations: list[Operation] | None = None
    plan_ops: list[str] = ["restore"]
    plan_metadata_keys: list[str] = []
    if args.restore:
        try:
            snapshots = _load_snapshots(args.restore)
        except ValueError as exc:
            parser.error(str(exc))
        operations: list[Operation] | dict[str, list[Operation]] = {}
    else:
        try:
            built_operations, system_wide = _build_operations(args)
        except ValueError as exc:
            parser.error(str(exc))
        operations = built_operations

    # Only now build the client: the real one needs credentials, and an
    # injected factory (tests) skips the environment entirely.
    client = client_factory()
    if client is None:
        return 2

    try:
        agents = client.utils.get_agents()
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report the discovery failure instead of a traceback
        print(f"cannot list agents: {exc}", file=sys.stderr)
        return 1

    missing: set[str] | None = None
    if snapshots is not None:
        by_id = {agent.agent_id: agent for agent in agents}
        selected = []
        per_agent: dict[str, list[Operation]] = {}
        missing = set()
        for snapshot in snapshots:
            agent_id = snapshot["agent_id"]
            per_agent[agent_id] = _restore_operations(snapshot)
            if agent_id not in by_id:
                missing.add(agent_id)
            selected.append(
                AgentOutput(agent_id=agent_id, metadata=dict(snapshot.get("metadata", {})))
            )
        operations = per_agent
    else:
        try:
            selected = select_agents(
                agents,
                args.agent or [],
                args.all,
                args.exclude or [],
                include_internal=bool(args.dump),
            )
        except SelectionError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        plan_ops = [_operation_label(operation) for operation in built_operations or []]
        plan_metadata_keys = sorted(
            key
            for operation in built_operations or []
            if operation.kind == "metadata"
            for key in operation.payload
        )

    if not args.yes:
        if args.json:
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "agents": [
                            {
                                "agent": agent.agent_id,
                                "ops": plan_ops,
                                "metadata_keys": plan_metadata_keys,
                            }
                            for agent in selected
                        ],
                        "system_wide": [{"op": _operation_label(operation)} for operation in system_wide],
                    },
                    indent=2,
                )
            )
        else:
            for line in render_plan(selected, plan_ops, plan_metadata_keys):
                print(line)
            print(f"system-wide: {len(system_wide)}")
        print("dry-run: no changes written (pass --yes to apply)", file=sys.stderr)
        return 2

    backup_dir = Path(args.backup_out) if args.backup_out else _default_backup_dir()

    envelope = execute_batch(
        client,
        selected,
        operations,
        system_wide=system_wide,
        snapshot_dir=backup_dir,
        argv=list(argv) if argv is not None else sys.argv[1:],
        max_workers=args.max_workers,
        missing=missing,
    )
    _report(envelope, args.json)
    return _exit_code(envelope)


if __name__ == "__main__":
    raise SystemExit(main())
