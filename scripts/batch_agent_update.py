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

    if args.restore:
        print("--restore is not implemented yet", file=sys.stderr)
        return 2

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

    if not args.yes:
        print("dry-run: no changes written (pass --yes to apply)", file=sys.stderr)
        return 2

    print("execution is not implemented yet", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
