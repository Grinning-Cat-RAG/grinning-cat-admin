"""Batch-update Grinning Cat agents through the official REST API.

The tool selects agents by a case-insensitive substring of their ``agent_id``
(or every non-system agent with ``--all``) and applies the requested changes to
each of them. It is a dry-run by default: it prints the plan and exits with
code 2 without writing anything, and only ``--yes`` turns that plan into
writes. Exit codes: 0 = every operation succeeded, 1 = at least one agent or
operation failed, 2 = dry-run, abort or usage error.
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.env import get_env, get_env_bool  # noqa: E402

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
) -> list[AgentOutput]:
    """Pick the agents to update, preserving the order of ``agents``.

    ``--all`` skips the ``system`` agent and every ``__*`` internal agent.
    A substring that matches nothing is fatal: silently updating a smaller
    set than the operator asked for is the worst failure mode here.
    """
    if include_all:
        candidates = [a for a in agents if a.agent_id != "system" and not a.agent_id.startswith("__")]
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


def render_plan(
    selected: list[AgentOutput],
    ops: list[str],
    metadata_keys: list[str] | None = None,
) -> list[str]:
    """One ``agent=... ops=... metadata_keys=...`` line per selected agent."""
    ops_label = ",".join(ops)
    keys_label = ",".join(metadata_keys or [])
    return [f"agent={agent.agent_id} ops={ops_label} metadata_keys={keys_label}" for agent in selected]


def _operations(args: argparse.Namespace) -> list[str]:
    ops = []
    if args.llm:
        ops.append("llm")
    if args.plugin:
        ops.append("plugin")
    if args.metadata or args.name:
        ops.append("metadata")
    if args.set:
        ops.append("set")
    if args.patch:
        ops.append("patch")
    return ops


def _metadata_keys(args: argparse.Namespace) -> list[str]:
    keys = []
    for item in args.metadata or []:
        key = item.split("=", 1)[0].strip()
        if key:
            keys.append(key)
    if args.name:
        keys.append("name")
    return keys


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
    client: Any, category: str, name: str, patch: dict[str, Any], agent_id: str
) -> dict[str, Any]:
    """Read-merge-write one factory setting and return the merged value.

    The core PUT replaces the whole value dict, so the current value must be
    read first and merged with the patch: a blind PUT would drop every key the
    patch does not mention.
    """
    try:
        attribute, getter_name, putter_name = FACTORY_CATEGORIES[category]
    except KeyError:
        valid = ", ".join(sorted(FACTORY_CATEGORIES))
        raise ValueError(f"unknown factory category {category!r}; valid categories: {valid}") from None

    endpoint = getattr(client, attribute)
    current = getattr(endpoint, getter_name)(name, agent_id)
    merged = {**current.value, **patch}
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


def _parse_value(raw: str) -> Any:
    """Decode a CLI value as JSON, falling back to the raw string."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def parse_llm(items: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """Parse ``MODEL=JSON`` items; the JSON side must be an object."""
    parsed = []
    for item in items:
        model, separator, raw = item.partition("=")
        if not separator or not model:
            raise ValueError(f"invalid --llm {item!r}: expected MODEL=JSON")
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


def load_patch(path: str) -> dict[tuple[str, str], dict[str, Any]]:
    """Load a ``{category: {name: patch}}`` JSON file into per-target patches."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read --patch {path!r}: {exc}") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in --patch {path!r}: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"invalid --patch {path!r}: the top level must be a JSON object")

    patches: dict[tuple[str, str], dict[str, Any]] = {}
    for category, entries in data.items():
        if not isinstance(entries, dict):
            raise ValueError(f"invalid --patch {path!r}: category {category!r} must be an object")
        if category == "metadata":
            patches[("metadata", "")] = dict(entries)
            continue
        for name, patch in entries.items():
            if not isinstance(patch, dict):
                raise ValueError(f"invalid --patch {path!r}: {category}:{name} must be an object")
            patches[(category, name)] = patch
    return patches


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _validate_categories(patches: dict[tuple[str, str], dict[str, Any]]) -> None:
    for category, _name in patches:
        if category in SYSTEM_WIDE_CATEGORIES:
            raise ValueError(f"{category} is system-wide; --allow-system-wide is not implemented yet")
        if category not in VALID_CATEGORIES:
            valid = ", ".join(sorted(VALID_CATEGORIES))
            raise ValueError(f"unknown category {category!r}; valid categories: {valid}")


def _build_operations(args: argparse.Namespace) -> list[Operation]:
    """Parse every operation before the first write, so a typo aborts the run."""
    patches = load_patch(args.patch) if args.patch else {}
    for key, patch in parse_set(args.set or []).items():
        patches[key] = _deep_merge(patches.get(key, {}), patch)

    metadata_payload: dict[str, Any] = {}
    for (category, _name), patch in patches.items():
        if category == "metadata":
            metadata_payload = _deep_merge(metadata_payload, patch)
    metadata_payload = _deep_merge(metadata_payload, parse_metadata_pairs(args.metadata or []))
    if args.name is not None:
        metadata_payload["name"] = args.name

    _validate_categories(patches)

    operations = []
    if metadata_payload:
        operations.append(Operation(kind="metadata", name="", payload=metadata_payload))
    for model, patch in parse_llm(args.llm or []):
        operations.append(Operation(kind="llm", name=model, payload=patch))
    for name, desired in parse_plugins(args.plugin or []):
        operations.append(Operation(kind="plugin", name=name, payload=desired))
    for (category, name), patch in patches.items():
        if category == "metadata":
            continue
        operations.append(Operation(kind="settings", category=category, name=name, payload=patch))
    return operations


def update_metadata(client: Any, agent: AgentOutput, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge ``updates`` into the agent metadata and PUT the whole dict.

    The core replaces the metadata dict, so the merge is mandatory: a blind
    PUT would drop every key the update does not mention.
    """
    merged = {**agent.metadata, **updates}
    result = client.utils.put_agent(agent.agent_id, merged)
    if not result.updated:
        raise RuntimeError(f"updated=false for {agent.agent_id}")
    return merged


def reconcile_plugin(client: Any, agent_id: str, name: str, desired: bool) -> str:
    """Bring one plugin to ``desired`` state, tolerating the core's flip toggle.

    The core endpoint flips the active flag instead of setting it, so the
    current state is read first and the result is verified afterwards: a
    repeated command must never toggle twice.
    """
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
    if operation.kind in ("llm", "plugin"):
        return f"{operation.kind}:{operation.name}"
    return operation.kind


def _apply_operation(client: Any, agent: AgentOutput, operation: Operation) -> str:
    if operation.kind == "metadata":
        update_metadata(client, agent, operation.payload)
        return "updated"
    if operation.kind == "llm":
        merge_factory_setting(client, "llm", operation.name, operation.payload, agent.agent_id)
        return "merged"
    if operation.kind == "plugin":
        return reconcile_plugin(client, agent.agent_id, operation.name, operation.payload)
    if operation.kind == "settings":
        if operation.category == "plugins":
            client.plugins.put_plugin_settings(operation.name, agent.agent_id, operation.payload)
            return "merged"
        merge_factory_setting(client, operation.category, operation.name, operation.payload, agent.agent_id)
        return "merged"
    raise NotImplementedError(f"operation {operation.kind!r} is not implemented yet")


def execute_batch(
    client: Any, selected: list[AgentOutput], operations: list[Operation]
) -> list[dict[str, Any]]:
    """Apply every operation to every agent, isolating failures per agent and per op."""
    ordered = sorted(operations, key=lambda operation: _OPERATION_ORDER[operation.kind])
    results = []
    for agent in selected:
        result: dict[str, Any] = {"agent_id": agent.agent_id, "ok": True, "ops": [], "error": None}
        try:
            for operation in ordered:
                try:
                    detail = _apply_operation(client, agent, operation)
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
        results.append(result)
    return results


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="batch_agent_update.py",
        description="Batch-update Grinning Cat agents through the official REST API (dry-run by default).",
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
        metavar="MODEL=JSON",
        help="merge a JSON object into the LLM settings of MODEL (repeatable)",
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
        help="JSON file with per-category patches",
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
        help="parallel workers (default: 1)",
    )
    return parser


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject impossible command lines before any client is built."""
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

    # The real client needs credentials. Report a missing key before the usage
    # checks so that an incomplete command line still names the real blocker;
    # an injected factory (tests) skips the environment entirely.
    if client_factory is default_client_factory and _configuration() is None:
        return 2

    _validate(parser, args)

    if args.max_workers != 1:
        parser.error("--max-workers > 1 is not implemented yet")

    if args.restore:
        print("--restore is not implemented yet", file=sys.stderr)
        return 2

    try:
        operations = _build_operations(args)
    except ValueError as exc:
        parser.error(str(exc))

    client = client_factory()
    if client is None:
        return 2

    try:
        agents = client.utils.get_agents()
    except Exception as exc:
        print(f"cannot list agents: {exc}", file=sys.stderr)
        return 1

    try:
        selected = select_agents(agents, args.agent or [], args.all, args.exclude or [])
    except SelectionError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    ops = _operations(args)
    metadata_keys = _metadata_keys(args)

    if not args.yes:
        if args.json:
            print(
                json.dumps(
                    [{"agent": agent.agent_id, "ops": ops, "metadata_keys": metadata_keys} for agent in selected],
                    indent=2,
                )
            )
        else:
            for line in render_plan(selected, ops, metadata_keys):
                print(line)
        print("dry-run: no changes written (pass --yes to apply)", file=sys.stderr)
        return 2

    results = execute_batch(client, selected, operations)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for result in results:
            for operation in result["ops"]:
                line = f"[batch] agent={result['agent_id']} op={operation['op']} result={operation['status']}"
                if operation["status"] == "failed":
                    line += f" reason={operation['detail']}"
                print(line)
            if result["error"]:
                print(f"[batch] agent={result['agent_id']} error={result['error']}", file=sys.stderr)

    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
