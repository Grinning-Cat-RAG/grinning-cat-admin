"""Tests for ``scripts/batch_agent_update.py``.

The CLI is always exercised through ``main(argv, client_factory=...)`` with a
fake client: no test opens a socket, touches Redis or needs a running Cat.
"""
import hashlib
import importlib.util
import sys
import threading
from argparse import Namespace
from pathlib import Path

import pytest
import yaml

from grinning_cat_python_sdk.models.api.admins import (
    AgentCreatedOutput,
    AgentOutput,
    AgentUpdatedOutput,
)
from grinning_cat_python_sdk.models.api.factories import (
    FactoryObjectSettingOutput,
    FactoryObjectSettingsOutput,
)
from grinning_cat_python_sdk.models.api.nested.plugins import PluginSettingsOutput
from grinning_cat_python_sdk.models.api.plugins import PluginToggleOutput

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "batch_agent_update.py"
_spec = importlib.util.spec_from_file_location("batch_agent_update", _SCRIPT)
if _spec is None or _spec.loader is None:  # pragma: no cover - import failure
    raise RuntimeError(f"cannot import {_SCRIPT}")
cli = importlib.util.module_from_spec(_spec)
sys.modules["batch_agent_update"] = cli
_spec.loader.exec_module(cli)


@pytest.fixture(autouse=True)
def _isolated_backup_dir(monkeypatch, tmp_path):
    """Never let a test write an ``agent-backup-*`` directory into the repo."""
    monkeypatch.setattr(cli, "_default_backup_dir", lambda: tmp_path / "agent-backup")


def _forbidden_factory():
    raise AssertionError("the client must not be built for --help or usage errors")


def test_help_lists_every_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"], client_factory=_forbidden_factory)

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for flag in (
        "--agent",
        "--all",
        "--create-missing",
        "--exclude",
        "--llm",
        "--plugin",
        "--metadata",
        "--name",
        "--set",
        "--patch",
        "--yes",
        "--yaml",
        "--backup-out",
        "--restore",
        "--allow-system-wide",
        "--max-workers",
    ):
        assert flag in out


def test_help_includes_examples(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"], client_factory=_forbidden_factory)

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "examples:" in out
    for fragment in (
        "--set llm:",
        "api_key",
        "--metadata",
        "--plugin",
        "--set plugins:",
        "--patch",
        "--restore",
        "--create-missing",
        "--allow-system-wide",
        "--max-workers",
    ):
        assert fragment in out
    invocations = [
        line.strip()
        for line in out.splitlines()
        if line.strip().startswith("uv run python scripts/batch_agent_update.py")
    ]
    assert len(invocations) >= 10


def test_help_examples_are_yaml_only(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"], client_factory=_forbidden_factory)

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    examples = out.split("examples:", 1)[1]
    assert "examples/patch-llm-openrouter.yml" in examples
    assert "examples/patch-full-agent.yml" in examples
    assert "json" not in examples.lower()
    assert "jsonc" not in examples.lower()
    assert all(len(line) <= 120 for line in examples.splitlines())


def test_readme_documents_yaml_examples_only():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")

    assert "examples/patch-llm-openrouter.yml" in readme
    assert "examples/patch-full-agent.yml" in readme
    assert "plain json" not in readme.lower()
    legacy_lines = [line for line in readme.splitlines() if "jsonc" in line.lower()]
    assert len(legacy_lines) <= 1
    for line in legacy_lines:
        assert "no longer accepted" in line.lower()


def test_usage_error_does_not_build_the_client(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--agent", "x"], client_factory=_forbidden_factory)

    assert excinfo.value.code == 2
    assert "operation" in capsys.readouterr().err


def test_agent_or_all_is_required(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--llm", "gpt-4={}"], client_factory=_forbidden_factory)

    assert excinfo.value.code == 2
    assert "--agent" in capsys.readouterr().err


def test_restore_is_exclusive_with_operations(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--restore", "snap.yml", "--llm", "gpt-4={}"], client_factory=_forbidden_factory)

    assert excinfo.value.code == 2
    assert "--restore" in capsys.readouterr().err


def test_configuration_reads_the_environment(monkeypatch):
    monkeypatch.setenv("GRINNING_CAT_API_HOST", "https://cat.example.com")
    monkeypatch.setenv("GRINNING_CAT_API_PORT", "443")
    monkeypatch.setenv("GRINNING_CAT_API_KEY", "secret")
    monkeypatch.setenv("GRINNING_CAT_API_SECURE_CONNECTION", "true")

    configuration = cli._configuration()

    assert configuration.host == "cat.example.com"
    assert configuration.port == 443
    assert configuration.auth_key == "secret"
    assert configuration.secure_connection is True


def test_missing_api_key_exits_2_without_a_traceback(monkeypatch, capsys):
    monkeypatch.delenv("GRINNING_CAT_API_KEY", raising=False)

    assert cli.main(["--agent", "x", "--metadata", "label=test", "--yes"]) == 2

    assert "GRINNING_CAT_API_KEY" in capsys.readouterr().err


def test_empty_api_key_is_rejected(monkeypatch, capsys):
    monkeypatch.setenv("GRINNING_CAT_API_KEY", "")

    assert cli._configuration() is None

    assert "GRINNING_CAT_API_KEY" in capsys.readouterr().err


def test_patch_suffix_is_validated_before_the_api_key(monkeypatch, capsys):
    """A non-YAML patch must name the format, not the missing key."""
    monkeypatch.delenv("GRINNING_CAT_API_KEY", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--agent", "x", "--patch", "/tmp/x.json"])

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "YAML" in err
    assert "GRINNING_CAT_API_KEY" not in err


def test_valid_patch_without_api_key_still_reports_the_key(monkeypatch, tmp_path, capsys):
    """A valid dry-run must reach the key guard and never build a client."""
    monkeypatch.delenv("GRINNING_CAT_API_KEY", raising=False)
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text("llm:\n  openrouter:\n    temperature: 0.2\n", encoding="utf-8")

    assert cli.main(["--agent", "x", "--patch", str(patch_file)]) == 2

    assert "GRINNING_CAT_API_KEY" in capsys.readouterr().err


def test_malformed_yaml_is_reported_before_the_api_key(monkeypatch, tmp_path, capsys):
    """A YAML parse error must win over the missing key."""
    monkeypatch.delenv("GRINNING_CAT_API_KEY", raising=False)
    patch_file = tmp_path / "broken.yml"
    patch_file.write_text("llm: [unclosed\n", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--agent", "x", "--patch", str(patch_file)])

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "invalid --patch" in err
    assert "GRINNING_CAT_API_KEY" not in err


class FakeEndpoint:
    """Records every call and returns SDK-shaped canned responses."""

    def __init__(self, client, attribute):
        self._client = client
        self._attribute = attribute

    def __getattr__(self, method_name):
        def call(*args, **kwargs):
            return self._client._handle(self._attribute, method_name, args, kwargs)

        return call


class FakePlugin:
    def __init__(self, plugin_id, active):
        self.id = plugin_id
        self.local_info = {"active": active}


class FakePluginCollection:
    def __init__(self, installed):
        self.installed = installed


_LIST_GETTER_ATTRIBUTES = {
    "get_large_language_models_settings": "large_language_model",
    "get_chunkers_settings": "chunker",
    "get_vector_databases_settings": "vector_database",
    "get_auth_handlers_settings": "auth_handler",
    "get_agentic_workflows_settings": "agentic_workflow",
    "get_context_retrievers_settings": "context_retriever",
    "get_file_managers_settings": "file_manager",
    "get_embedders_settings": "embedder",
    "get_ingestions_settings": "ingestion",
}


class FakeClient:
    """In-memory stand-in for ``GrinningCatClient``: no network, no Redis."""

    def __init__(
        self,
        agents=None,
        settings=None,
        fail_on=None,
        updated_false=None,
        plugins=None,
        toggle_noop=False,
        plugin_settings=None,
        concurrency_guard=False,
        disappearing_plugins=None,
        factory_lists=None,
        plugin_settings_500_inactive=False,
        plugin_settings_fail=None,
        create_false=None,
    ):
        self.calls = []
        self.agents = list(agents or [])
        self.created = []
        self.create_false = set(create_false or ())
        self.settings = dict(settings or {})
        self.fail_on = set(fail_on or ())
        self.updated_false = set(updated_false or ())
        self.plugin_states = {agent_id: dict(items) for agent_id, items in (plugins or {}).items()}
        self.plugin_settings = {
            (agent_id, plugin_id): dict(values)
            for (agent_id, plugin_id), values in (plugin_settings or {}).items()
        }
        self.plugin_settings_500_inactive = plugin_settings_500_inactive
        self.plugin_settings_fail = set(plugin_settings_fail or ())
        self.plugin_settings_payloads = []
        self.toggle_noop = toggle_noop
        self.concurrency_guard = concurrency_guard
        self.disappearing_plugins = set(disappearing_plugins or ())
        self.factory_lists = dict(factory_lists or {})
        self._agent_locks = {}
        self._agent_ids = {agent.agent_id for agent in self.agents}
        for attribute in (
            "utils",
            "large_language_model",
            "plugins",
            "chunker",
            "vector_database",
            "auth_handler",
            "agentic_workflow",
            "context_retriever",
            "file_manager",
            "embedder",
            "ingestion",
        ):
            setattr(self, attribute, FakeEndpoint(self, attribute))

    def _handle(self, attribute, method_name, args, kwargs):
        if not self.concurrency_guard:
            return self._dispatch(attribute, method_name, args, kwargs)
        agent_id = next((value for value in args if isinstance(value, str) and value in self._agent_ids), None)
        if agent_id is None:
            return self._dispatch(attribute, method_name, args, kwargs)
        lock = self._agent_locks.setdefault(agent_id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise RuntimeError(f"concurrent access to agent {agent_id!r}")
        try:
            return self._dispatch(attribute, method_name, args, kwargs)
        finally:
            lock.release()

    def _dispatch(self, attribute, method_name, args, kwargs):
        self.calls.append((method_name, args))
        if method_name in self.fail_on:
            raise RuntimeError(f"{method_name} failed")
        agent_id = next((value for value in args if isinstance(value, str) and value in self._agent_ids), None)
        if agent_id is not None and (method_name, agent_id) in self.fail_on:
            raise RuntimeError(f"{method_name} failed for {agent_id}")
        if method_name == "get_agents":
            return list(self.agents)
        if method_name == "post_agent_create":
            agent_id, metadata = args
            self.created.append((agent_id, dict(metadata or {})))
            if agent_id in self.create_false:
                return AgentCreatedOutput(created=False)
            self.agents.append(_agent(agent_id, metadata))
            self._agent_ids.add(agent_id)
            return AgentCreatedOutput(created=True)
        if method_name == "put_agent":
            agent_id, metadata = args
            self.settings[("metadata", agent_id)] = dict(metadata)
            return AgentUpdatedOutput(updated=agent_id not in self.updated_false)
        if method_name == "get_available_plugins":
            agent_id = args[0]
            installed = [
                FakePlugin(plugin_id, active)
                for plugin_id, active in self.plugin_states.get(agent_id, {}).items()
            ]
            return FakePluginCollection(installed)
        if method_name == "put_toggle_plugin":
            plugin_id, agent_id = args
            state = self.plugin_states.setdefault(agent_id, {})
            if plugin_id not in state:
                raise RuntimeError(f"plugin {plugin_id!r} is not installed for agent {agent_id!r}")
            if not self.toggle_noop:
                state[plugin_id] = not state[plugin_id]
            if plugin_id in self.disappearing_plugins:
                state.pop(plugin_id, None)
            return PluginToggleOutput(info="toggled")
        if method_name in _LIST_GETTER_ATTRIBUTES:
            attribute = _LIST_GETTER_ATTRIBUTES[method_name]
            agent_id = args[0] if args else None
            return self.factory_lists[(attribute, agent_id)]
        if method_name == "get_plugin_settings":
            plugin_id, agent_id = args
            if plugin_id in self.plugin_settings_fail:
                raise RuntimeError(f"500 Server Error for plugin {plugin_id!r}")
            if self.plugin_settings_500_inactive and not self.plugin_states.get(agent_id, {}).get(
                plugin_id
            ):
                raise RuntimeError(f"500 Server Error: KeyError {plugin_id!r}")
            return FactoryObjectSettingOutput(
                name=plugin_id, value=dict(self.plugin_settings.get((agent_id, plugin_id), {}))
            )
        if method_name == "put_plugin_settings":
            plugin_id, agent_id, values = args
            key = (agent_id, plugin_id)
            self.plugin_settings_payloads.append((plugin_id, agent_id, dict(values)))
            merged = {**self.plugin_settings.get(key, {}), **values}
            self.plugin_settings[key] = merged
            return PluginSettingsOutput(name=plugin_id, value=merged)
        if method_name in ("put_embedder_settings", "put_ingestion_settings"):
            name, values = args
            self.settings[(attribute, name)] = dict(values)
            return FactoryObjectSettingOutput(name=name, value=dict(values))
        if method_name.startswith("get_"):
            name = args[0]
            return FactoryObjectSettingOutput(name=name, value=dict(self.settings.get((attribute, name), {})))
        if method_name.startswith("put_"):
            name, _agent_id, values = args
            self.settings[(attribute, name)] = dict(values)
            return FactoryObjectSettingOutput(name=name, value=dict(values))
        raise AssertionError(f"unexpected call {method_name}")

    def calls_to(self, method_name):
        return [args for name, args in self.calls if name == method_name]


def _agent(agent_id, metadata=None):
    return AgentOutput(agent_id=agent_id, metadata=metadata or {})


def _snapshot_files(directory):
    """Every snapshot file in ``directory``, excluding the manifest and dotfiles."""
    return sorted(
        path
        for path in Path(directory).glob("*.yml")
        if path.name != "manifest.yml" and not path.name.startswith(".")
    )


def test_select_agents_matches_case_insensitive_substrings():
    agents = [_agent("Prod_Corso1"), _agent("prod_corso2"), _agent("other")]

    selected = cli.select_agents(agents, ["corso1"], include_all=False, exclude=[])

    assert [a.agent_id for a in selected] == ["Prod_Corso1"]


def test_select_agents_all_skips_system_and_dunder_agents():
    agents = [_agent("system"), _agent("__internal"), _agent("corso1")]

    selected = cli.select_agents(agents, [], include_all=True, exclude=[])

    assert [a.agent_id for a in selected] == ["corso1"]


def test_select_agents_unknown_substring_is_fatal():
    agents = [_agent("corso1")]

    with pytest.raises(cli.SelectionError) as excinfo:
        cli.select_agents(agents, ["zzz-not-present"], include_all=False, exclude=[])

    assert "zzz-not-present" in str(excinfo.value)


def test_select_agents_excludes_substrings_case_insensitively():
    agents = [_agent("Corso1_Test"), _agent("Corso1_Prod")]

    selected = cli.select_agents(agents, ["corso1"], include_all=False, exclude=["TEST"])

    assert [a.agent_id for a in selected] == ["Corso1_Prod"]


def test_select_agents_dedupes_preserving_order():
    agents = [_agent("corso1"), _agent("corso2")]

    selected = cli.select_agents(agents, ["corso", "corso1"], include_all=False, exclude=[])

    assert [a.agent_id for a in selected] == ["corso1", "corso2"]


def test_select_agents_rejects_an_empty_selection():
    with pytest.raises(cli.SelectionError):
        cli.select_agents([_agent("system")], [], include_all=True, exclude=[])


def test_render_plan_has_one_line_per_agent():
    agents = [_agent("corso1"), _agent("corso2")]

    lines = cli.render_plan(agents, ["llm", "metadata"], ["label"])

    assert lines == [
        "agent=corso1 ops=llm,metadata metadata_keys=label",
        "agent=corso2 ops=llm,metadata metadata_keys=label",
    ]


def test_render_plan_without_metadata_keys():
    lines = cli.render_plan([_agent("corso1")], ["llm"])

    assert lines == ["agent=corso1 ops=llm metadata_keys="]


def test_dry_run_prints_the_plan_and_writes_nothing(capsys):
    client = FakeClient(agents=[_agent("Corso1"), _agent("other")])

    rc = cli.main(["--agent", "corso1", "--llm", "gpt-4={}"], client_factory=lambda: client)

    assert rc == 2
    out = capsys.readouterr().out
    assert "agent=Corso1 ops=llm:gpt-4 metadata_keys=" in out
    assert client.calls_to("put_large_language_model_settings") == []
    assert client.calls_to("put_agent") == []


def test_dry_run_unknown_substring_exits_2(capsys):
    client = FakeClient(agents=[_agent("corso1")])

    rc = cli.main(["--agent", "zzz-not-present", "--llm", "gpt-4={}"], client_factory=lambda: client)

    assert rc == 2
    assert "zzz-not-present" in capsys.readouterr().err


def test_dry_run_yaml_plan(capsys):
    client = FakeClient(agents=[_agent("Corso1"), _agent("other")])

    rc = cli.main(["--agent", "corso1", "--llm", "gpt-4={}", "--yaml"], client_factory=lambda: client)

    assert rc == 2
    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload == {
        "dry_run": True,
        "agents": [{"agent": "Corso1", "ops": ["llm:gpt-4"], "metadata_keys": []}],
        "system_wide": [],
    }


def test_dry_run_yaml_plan_has_no_anchors(capsys):
    client = FakeClient(agents=[_agent("corso1"), _agent("corso2")])

    rc = cli.main(["--agent", "corso", "--llm", "gpt-4={}", "--yaml"], client_factory=lambda: client)

    assert rc == 2
    out = capsys.readouterr().out
    assert "&id" not in out
    assert "*id" not in out
    payload = yaml.safe_load(out)
    assert payload["dry_run"] is True
    assert [entry["agent"] for entry in payload["agents"]] == ["corso1", "corso2"]
    assert payload["agents"][0]["ops"] == ["llm:gpt-4"]
    assert payload["agents"][1]["ops"] == ["llm:gpt-4"]
    assert payload["system_wide"] == []


def test_discovery_failure_exits_1(capsys):
    client = FakeClient(agents=[], fail_on={"get_agents"})

    rc = cli.main(["--agent", "x", "--llm", "gpt-4={}"], client_factory=lambda: client)

    assert rc == 1
    assert "get_agents" in capsys.readouterr().err


def test_merge_factory_setting_preserves_existing_keys():
    client = FakeClient(settings={("large_language_model", "gpt-4"): {"model": "x", "temperature": 0.0}})

    merged = cli.merge_factory_setting(client, "llm", "gpt-4", {"temperature": 0.2}, "corso1")

    assert merged == {"model": "x", "temperature": 0.2}
    assert client.calls_to("get_large_language_model_settings") == [("gpt-4", "corso1")]
    assert client.calls_to("put_large_language_model_settings") == [
        ("gpt-4", "corso1", {"model": "x", "temperature": 0.2})
    ]
    assert client.settings[("large_language_model", "gpt-4")] == {"model": "x", "temperature": 0.2}


def test_merge_factory_setting_unknown_category_raises_and_never_writes():
    client = FakeClient()

    with pytest.raises(ValueError) as excinfo:
        cli.merge_factory_setting(client, "unknown", "x", {"a": 1}, "corso1")

    message = str(excinfo.value)
    assert "unknown" in message
    for category in cli.FACTORY_CATEGORIES:
        assert category in message
    assert client.calls == []


@pytest.mark.parametrize("category", sorted(cli.FACTORY_CATEGORIES))
def test_merge_factory_setting_dispatches_every_category(category):
    attribute, getter, putter = cli.FACTORY_CATEGORIES[category]
    client = FakeClient(settings={(attribute, "default"): {"keep": 1}})

    merged = cli.merge_factory_setting(client, category, "default", {"new": 2}, "corso1")

    assert merged == {"keep": 1, "new": 2}
    assert client.calls_to(getter) == [("default", "corso1")]
    assert client.calls_to(putter) == [("default", "corso1", {"keep": 1, "new": 2})]


def test_factory_categories_match_the_sdk():
    from grinning_cat_python_sdk import GrinningCatClient
    from grinning_cat_python_sdk.endpoints import (
        AgenticWorkflowEndpoint,
        AuthHandlerEndpoint,
        ChunkerEndpoint,
        ContextRetrieverEndpoint,
        FileManagerEndpoint,
        LargeLanguageModelEndpoint,
        VectorDatabaseEndpoint,
    )

    endpoint_classes = {
        "large_language_model": LargeLanguageModelEndpoint,
        "chunker": ChunkerEndpoint,
        "vector_database": VectorDatabaseEndpoint,
        "auth_handler": AuthHandlerEndpoint,
        "agentic_workflow": AgenticWorkflowEndpoint,
        "context_retriever": ContextRetrieverEndpoint,
        "file_manager": FileManagerEndpoint,
    }
    assert {attribute for attribute, _getter, _putter in cli.FACTORY_CATEGORIES.values()} == set(endpoint_classes)
    for category, (attribute, getter, putter) in cli.FACTORY_CATEGORIES.items():
        assert hasattr(GrinningCatClient, attribute), category
        endpoint_class = endpoint_classes[attribute]
        assert hasattr(endpoint_class, getter), category
        assert hasattr(endpoint_class, putter), category


def test_set_nested_creates_missing_branches():
    target = {}

    cli.set_nested(target, "a.b.c", 1)

    assert target == {"a": {"b": {"c": 1}}}


def test_set_nested_keeps_sibling_keys():
    target = {"a": {"keep": True}}

    cli.set_nested(target, "a.b", 2)

    assert target == {"a": {"keep": True, "b": 2}}


def test_set_nested_replaces_a_scalar_branch():
    target = {"a": 1}

    cli.set_nested(target, "a.b", 2)

    assert target == {"a": {"b": 2}}


def test_set_nested_rejects_an_empty_key():
    with pytest.raises(ValueError):
        cli.set_nested({}, "", 1)


def test_llm_merges_and_preserves_untouched_keys_on_two_agents():
    client = FakeClient(
        agents=[_agent("corso1"), _agent("corso2")],
        settings={
            ("large_language_model", "gpt-4"): {"model": "gpt-4", "temperature": 0.0, "max_tokens": 100},
        },
    )

    rc = cli.main(
        ["--agent", "corso", "--llm", 'gpt-4={"temperature": 0.2}', "--yes"],
        client_factory=lambda: client,
    )

    assert rc == 0
    assert len(client.calls_to("get_large_language_model_settings")) == 2
    puts = client.calls_to("put_large_language_model_settings")
    assert len(puts) == 2
    for name, agent_id, values in puts:
        assert name == "gpt-4"
        assert agent_id in {"corso1", "corso2"}
        assert values == {"model": "gpt-4", "temperature": 0.2, "max_tokens": 100}


def test_llm_non_object_patch_exits_2_without_calls(capsys):
    client = FakeClient(agents=[_agent("corso1")])

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--agent", "corso", "--llm", "gpt-4=0.2", "--yes"], client_factory=lambda: client)

    assert excinfo.value.code == 2
    assert "gpt-4=0.2" in capsys.readouterr().err
    assert client.calls == []


def test_llm_invalid_json_exits_2_without_calls(capsys):
    client = FakeClient(agents=[_agent("corso1")])

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--agent", "corso", "--llm", "gpt-4={bad}", "--yes"], client_factory=lambda: client)

    assert excinfo.value.code == 2
    assert "gpt-4={bad}" in capsys.readouterr().err
    assert client.calls == []


def test_llm_failure_surfaces_as_agent_failure():
    client = FakeClient(agents=[_agent("corso1")], fail_on={"put_large_language_model_settings"})

    rc = cli.main(["--agent", "corso", "--llm", "gpt-4={}", "--yes"], client_factory=lambda: client)

    assert rc == 1


def test_plugin_already_active_is_not_toggled():
    client = FakeClient(agents=[_agent("corso1")], plugins={"corso1": {"foo": True}})

    rc = cli.main(["--agent", "corso", "--plugin", "foo=on", "--yes"], client_factory=lambda: client)

    assert rc == 0
    assert client.calls_to("put_toggle_plugin") == []
    assert client.plugin_states["corso1"]["foo"] is True


def test_plugin_inactive_is_toggled_once_and_verified():
    client = FakeClient(agents=[_agent("corso1")], plugins={"corso1": {"foo": False}})

    rc = cli.main(["--agent", "corso", "--plugin", "foo=on", "--yes"], client_factory=lambda: client)

    assert rc == 0
    assert client.calls_to("put_toggle_plugin") == [("foo", "corso1")]
    assert client.plugin_states["corso1"]["foo"] is True
    assert len(client.calls_to("get_available_plugins")) == 2


def test_plugin_off_toggles_an_active_plugin():
    client = FakeClient(agents=[_agent("corso1")], plugins={"corso1": {"foo": True}})

    rc = cli.main(["--agent", "corso", "--plugin", "foo=off", "--yes"], client_factory=lambda: client)

    assert rc == 0
    assert client.calls_to("put_toggle_plugin") == [("foo", "corso1")]
    assert client.plugin_states["corso1"]["foo"] is False


def test_plugin_command_twice_toggles_once():
    client = FakeClient(agents=[_agent("corso1")], plugins={"corso1": {"foo": False}})

    first = cli.main(["--agent", "corso", "--plugin", "foo=on", "--yes"], client_factory=lambda: client)
    second = cli.main(["--agent", "corso", "--plugin", "foo=on", "--yes"], client_factory=lambda: client)

    assert (first, second) == (0, 0)
    assert client.calls_to("put_toggle_plugin") == [("foo", "corso1")]


def test_plugin_not_installed_fails_that_agent_only(capsys):
    client = FakeClient(
        agents=[_agent("corso1"), _agent("corso2")],
        plugins={"corso1": {}, "corso2": {"foo": False}},
    )

    rc = cli.main(["--agent", "corso", "--plugin", "foo=on", "--yes"], client_factory=lambda: client)

    assert rc == 1
    assert client.plugin_states["corso2"]["foo"] is True
    assert client.calls_to("put_toggle_plugin") == [("foo", "corso2")]
    out = capsys.readouterr().out
    assert "agent=corso1 op=plugin:foo result=failed" in out
    assert "is not installed" in out


def test_plugin_toggle_that_does_not_change_state_fails():
    client = FakeClient(
        agents=[_agent("corso1")],
        plugins={"corso1": {"foo": False}},
        toggle_noop=True,
    )

    rc = cli.main(["--agent", "corso", "--plugin", "foo=on", "--yes"], client_factory=lambda: client)

    assert rc == 1
    assert client.calls_to("put_toggle_plugin") == [("foo", "corso1")]


def test_plugin_invalid_state_exits_2_without_calls(capsys):
    client = FakeClient(agents=[_agent("corso1")])

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--agent", "corso", "--plugin", "foo=maybe", "--yes"], client_factory=lambda: client)

    assert excinfo.value.code == 2
    assert "foo=maybe" in capsys.readouterr().err
    assert client.calls == []


def test_metadata_merge_preserves_existing_keys():
    client = FakeClient(agents=[_agent("corso1", {"public": True, "label": "a"})])

    rc = cli.main(["--agent", "corso", "--metadata", "label=b", "--yes"], client_factory=lambda: client)

    assert rc == 0
    assert client.calls_to("put_agent") == [("corso1", {"public": True, "label": "b"})]


def test_name_sets_name_and_keeps_other_keys():
    client = FakeClient(agents=[_agent("corso1", {"public": True})])

    rc = cli.main(["--agent", "corso", "--name", "Nuovo", "--yes"], client_factory=lambda: client)

    assert rc == 0
    assert client.calls_to("put_agent") == [("corso1", {"public": True, "name": "Nuovo"})]


def test_name_wins_over_metadata_name():
    client = FakeClient(agents=[_agent("corso1")])

    rc = cli.main(
        ["--agent", "corso", "--metadata", "name=old", "--name", "new", "--yes"],
        client_factory=lambda: client,
    )

    assert rc == 0
    assert client.calls_to("put_agent") == [("corso1", {"name": "new"})]


def test_metadata_values_are_json_parsed():
    client = FakeClient(agents=[_agent("corso1")])

    rc = cli.main(
        [
            "--agent",
            "corso",
            "--metadata",
            "public=true",
            "--metadata",
            "n=3",
            "--metadata",
            "label=x",
            "--yes",
        ],
        client_factory=lambda: client,
    )

    assert rc == 0
    assert client.calls_to("put_agent") == [("corso1", {"public": True, "n": 3, "label": "x"})]


def test_updated_false_fails_the_agent(capsys):
    client = FakeClient(agents=[_agent("corso1")], updated_false={"corso1"})

    rc = cli.main(["--agent", "corso", "--metadata", "label=b", "--yes"], client_factory=lambda: client)

    assert rc == 1
    assert "updated=false for corso1" in capsys.readouterr().out


def test_metadata_without_equals_exits_2_without_calls(capsys):
    client = FakeClient(agents=[_agent("corso1")])

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--agent", "corso", "--metadata", "label", "--yes"], client_factory=lambda: client)

    assert excinfo.value.code == 2
    assert "label" in capsys.readouterr().err
    assert client.calls == []


def test_set_chunking_writes_an_int():
    client = FakeClient(
        agents=[_agent("corso1")],
        settings={("chunker", "default"): {"chunk_size": 256, "chunk_overlap": 32}},
    )

    rc = cli.main(
        ["--agent", "corso", "--set", "chunking:default:chunk_size=512", "--yes"],
        client_factory=lambda: client,
    )

    assert rc == 0
    assert client.calls_to("put_chunker_settings") == [
        ("default", "corso1", {"chunk_size": 512, "chunk_overlap": 32})
    ]


def test_set_dotted_key_creates_nested_branch():
    client = FakeClient(
        agents=[_agent("corso1")],
        settings={("large_language_model", "gpt-4"): {"model": "gpt-4"}},
    )

    rc = cli.main(["--agent", "corso", "--set", "llm:gpt-4:a.b=1", "--yes"], client_factory=lambda: client)

    assert rc == 0
    assert client.calls_to("put_large_language_model_settings") == [
        ("gpt-4", "corso1", {"model": "gpt-4", "a": {"b": 1}})
    ]


def test_patch_file_writes_each_pair_once(tmp_path):
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text(
        "llm:\n"
        "  gpt-4:\n"
        "    temperature: 0.2\n"
        "plugins:\n"
        "  foo:\n"
        "    k: v\n"
        "metadata:\n"
        "  name: X\n",
        encoding="utf-8",
    )
    client = FakeClient(
        agents=[_agent("corso1", {"public": True})],
        settings={("large_language_model", "gpt-4"): {"model": "gpt-4"}},
        plugins={"corso1": {"foo": True}},
    )

    rc = cli.main(["--agent", "corso", "--patch", str(patch_file), "--yes"], client_factory=lambda: client)

    assert rc == 0
    assert client.calls_to("put_large_language_model_settings") == [
        ("gpt-4", "corso1", {"model": "gpt-4", "temperature": 0.2})
    ]
    assert client.calls_to("put_plugin_settings") == [("foo", "corso1", {"k": "v"})]
    assert client.calls_to("put_agent") == [("corso1", {"public": True, "name": "X"})]


def test_set_plugin_dotted_key_deep_merges_nested_siblings():
    client = FakeClient(
        agents=[_agent("corso1")],
        plugins={"corso1": {"foo": True}},
        plugin_settings={("corso1", "foo"): {"options": {"a": 1, "b": 2}, "other": 1}},
    )

    rc = cli.main(
        ["--agent", "corso", "--set", "plugins:foo:options.a=9", "--yes"],
        client_factory=lambda: client,
    )

    assert rc == 0
    assert client.plugin_settings_payloads == [
        ("foo", "corso1", {"options": {"a": 9, "b": 2}, "other": 1})
    ]
    assert client.plugin_settings[("corso1", "foo")] == {"options": {"a": 9, "b": 2}, "other": 1}


def test_patch_plugin_nested_dict_keeps_siblings(tmp_path):
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text("plugins:\n  foo:\n    options:\n      a: 5\n", encoding="utf-8")
    client = FakeClient(
        agents=[_agent("corso1")],
        plugins={"corso1": {"foo": True}},
        plugin_settings={("corso1", "foo"): {"options": {"a": 1, "b": 2}, "other": 1}},
    )

    rc = cli.main(["--agent", "corso", "--patch", str(patch_file), "--yes"], client_factory=lambda: client)

    assert rc == 0
    assert client.plugin_settings_payloads == [
        ("foo", "corso1", {"options": {"a": 5, "b": 2}, "other": 1})
    ]
    assert client.plugin_settings[("corso1", "foo")] == {"options": {"a": 5, "b": 2}, "other": 1}


def test_restore_writes_back_nested_plugin_settings_with_siblings(tmp_path):
    client = FakeClient(
        agents=[_agent("corso1")],
        plugins={"corso1": {"foo": True}},
        plugin_settings={("corso1", "foo"): {"options": {"a": 1, "b": 2}, "other": 1}},
    )

    first = cli.main(
        [
            "--agent",
            "corso",
            "--set",
            "plugins:foo:options.a=9",
            "--yes",
            "--backup-out",
            str(tmp_path),
        ],
        client_factory=lambda: client,
    )
    assert first == 0
    assert client.plugin_settings[("corso1", "foo")] == {"options": {"a": 9, "b": 2}, "other": 1}

    client.plugin_settings[("corso1", "foo")] = {"options": {"a": 9, "b": 7}, "other": 1}
    client.calls.clear()
    client.plugin_settings_payloads.clear()

    second = cli.main(["--restore", str(tmp_path), "--yes"], client_factory=lambda: client)

    assert second == 0
    assert client.plugin_settings_payloads == [
        ("foo", "corso1", {"options": {"a": 1, "b": 2}, "other": 1})
    ]
    assert client.plugin_settings[("corso1", "foo")] == {"options": {"a": 1, "b": 2}, "other": 1}


def test_patch_and_set_are_coalesced(tmp_path):
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text(
        "llm:\n  gpt-4:\n    temperature: 0.2\n    max_tokens: 10\n",
        encoding="utf-8",
    )
    client = FakeClient(
        agents=[_agent("corso1")],
        settings={("large_language_model", "gpt-4"): {"model": "gpt-4"}},
    )

    rc = cli.main(
        [
            "--agent",
            "corso",
            "--patch",
            str(patch_file),
            "--set",
            "llm:gpt-4:temperature=0.5",
            "--yes",
        ],
        client_factory=lambda: client,
    )

    assert rc == 0
    puts = client.calls_to("put_large_language_model_settings")
    assert len(puts) == 1
    assert puts[0][2] == {"model": "gpt-4", "temperature": 0.5, "max_tokens": 10}


def test_unknown_category_exits_2_without_calls(capsys):
    client = FakeClient(agents=[_agent("corso1")])

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--agent", "corso", "--set", "nope:x:y=1", "--yes"], client_factory=lambda: client)

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "nope" in err
    assert "llm" in err
    assert client.calls == []


def test_invalid_patch_shape_exits_2_without_calls(tmp_path, capsys):
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text("llm:\n  gpt-4: 0.2\n", encoding="utf-8")
    client = FakeClient(agents=[_agent("corso1")])

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--agent", "corso", "--patch", str(patch_file), "--yes"], client_factory=lambda: client)

    assert excinfo.value.code == 2
    assert "gpt-4" in capsys.readouterr().err
    assert client.calls == []


def test_malformed_set_exits_2_with_grammar(capsys):
    client = FakeClient(agents=[_agent("corso1")])

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--agent", "corso", "--set", "llm:gpt-4", "--yes"], client_factory=lambda: client)

    assert excinfo.value.code == 2
    assert "CATEGORY:NAME:KEY=VALUE" in capsys.readouterr().err
    assert client.calls == []


def test_embedder_is_rejected_as_system_wide(capsys):
    client = FakeClient(agents=[_agent("corso1")])

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--agent", "corso", "--set", "embedder:openai:key=x", "--yes"], client_factory=lambda: client)

    assert excinfo.value.code == 2
    assert "system-wide" in capsys.readouterr().err
    assert client.calls == []


def test_max_workers_zero_exits_2(capsys):
    client = FakeClient(agents=[_agent("corso1")])

    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            ["--agent", "corso", "--metadata", "a=b", "--max-workers", "0", "--yes"],
            client_factory=lambda: client,
        )

    assert excinfo.value.code == 2
    assert "--max-workers" in capsys.readouterr().err
    assert client.calls == []


def test_yes_happy_path_yaml_report(capsys):
    client = FakeClient(agents=[_agent("corso1", {"public": True})])

    rc = cli.main(
        ["--agent", "corso", "--metadata", "label=b", "--yes", "--yaml"],
        client_factory=lambda: client,
    )

    assert rc == 0
    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload["summary"]["ok"] == 1
    assert payload["agents"][0]["agent_id"] == "corso1"
    assert payload["agents"][0]["ok"] is True
    assert payload["agents"][0]["ops"] == [{"op": "metadata", "status": "ok", "detail": "updated"}]
    assert payload["agents"][0]["error"] is None


def test_human_mode_prints_one_line_per_op(capsys):
    client = FakeClient(agents=[_agent("corso1")])

    rc = cli.main(["--agent", "corso", "--metadata", "label=b", "--yes"], client_factory=lambda: client)

    assert rc == 0
    assert "[batch] agent=corso1 op=metadata result=ok" in capsys.readouterr().out


def test_failed_op_prints_reason(capsys):
    client = FakeClient(agents=[_agent("corso1")], updated_false={"corso1"})

    rc = cli.main(["--agent", "corso", "--metadata", "label=b", "--yes"], client_factory=lambda: client)

    assert rc == 1
    out = capsys.readouterr().out
    assert "result=failed" in out
    assert "updated=false for corso1" in out


def test_operation_order_is_metadata_llm_plugins_settings():
    client = FakeClient(
        agents=[_agent("corso1")],
        settings={("large_language_model", "gpt-4"): {"model": "gpt-4"}},
        plugins={"corso1": {"foo": True}},
    )

    rc = cli.main(
        [
            "--agent",
            "corso",
            "--set",
            "chunking:default:chunk_size=512",
            "--plugin",
            "foo=on",
            "--llm",
            "gpt-4={}",
            "--metadata",
            "label=b",
            "--yes",
        ],
        client_factory=lambda: client,
    )

    assert rc == 0
    write_calls = [name for name, _args in client.calls if name.startswith("put_")]
    assert write_calls == ["put_agent", "put_large_language_model_settings", "put_chunker_settings"]


def test_system_wide_without_flag_exits_2_and_writes_nothing(capsys):
    client = FakeClient(agents=[_agent("corso1"), _agent("corso2"), _agent("corso3")])

    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            ["--agent", "corso", "--set", "embedder:openai:key=x", "--yes"],
            client_factory=lambda: client,
        )

    assert excinfo.value.code == 2
    assert "system-wide" in capsys.readouterr().err
    assert client.calls == []


def test_system_wide_with_flag_runs_once_for_all_agents(capsys):
    client = FakeClient(
        agents=[_agent("corso1"), _agent("corso2"), _agent("corso3")],
        settings={("embedder", "openai"): {"model": "text-embedding-3-small"}},
    )

    rc = cli.main(
        [
            "--agent",
            "corso",
            "--set",
            "embedder:openai:key=x",
            "--allow-system-wide",
            "--yes",
            "--yaml",
        ],
        client_factory=lambda: client,
    )

    assert rc == 0
    assert client.calls_to("put_embedder_settings") == [
        ("openai", {"model": "text-embedding-3-small", "key": "x"})
    ]
    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload["summary"]["ok"] == 3
    assert payload["system_wide"] == [{"op": "system:embedder:openai", "status": "ok", "detail": "merged"}]


def test_system_wide_failure_exits_1_without_touching_agents(capsys):
    client = FakeClient(
        agents=[_agent("corso1"), _agent("corso2")],
        fail_on={"put_embedder_settings"},
    )

    rc = cli.main(
        [
            "--agent",
            "corso",
            "--set",
            "embedder:openai:key=x",
            "--allow-system-wide",
            "--yes",
            "--yaml",
        ],
        client_factory=lambda: client,
    )

    assert rc == 1
    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload["summary"] == {"selected": 2, "ok": 2, "failed": 0, "skipped": 0}
    assert payload["system_wide"][0]["status"] == "failed"


def test_dry_run_prints_the_system_wide_count(capsys):
    client = FakeClient(agents=[_agent("corso1")])

    rc = cli.main(
        ["--agent", "corso", "--set", "embedder:openai:key=x", "--allow-system-wide"],
        client_factory=lambda: client,
    )

    assert rc == 2
    assert "system-wide: 1" in capsys.readouterr().out


def test_dry_run_yaml_stays_an_array_with_system_wide(capsys):
    client = FakeClient(agents=[_agent("corso1")])

    rc = cli.main(
        ["--agent", "corso", "--set", "embedder:openai:key=x", "--allow-system-wide", "--yaml"],
        client_factory=lambda: client,
    )

    assert rc == 2
    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload == {
        "dry_run": True,
        "agents": [{"agent": "corso1", "ops": [], "metadata_keys": []}],
        "system_wide": [{"op": "system:embedder:openai"}],
    }


def test_dry_run_plan_derives_metadata_keys_from_the_operations(capsys):
    client = FakeClient(agents=[_agent("corso1")])

    rc = cli.main(
        ["--agent", "corso", "--set", "metadata:ignored:name=x", "--yaml"],
        client_factory=lambda: client,
    )

    assert rc == 2
    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload["agents"] == [{"agent": "corso1", "ops": ["metadata"], "metadata_keys": ["name"]}]
    assert payload["system_wide"] == []


def test_backup_out_writes_one_snapshot_per_agent_and_a_manifest(tmp_path):
    client = FakeClient(agents=[_agent("corso1", {"public": True}), _agent("corso2")])

    rc = cli.main(
        ["--agent", "corso", "--metadata", "label=b", "--yes", "--backup-out", str(tmp_path)],
        client_factory=lambda: client,
    )

    assert rc == 0
    snapshot_files = _snapshot_files(tmp_path)
    assert len(snapshot_files) == 2
    manifest = yaml.safe_load((tmp_path / "manifest.yml").read_text(encoding="utf-8"))
    assert manifest["agents"] == ["corso1", "corso2"]
    assert manifest["argv"]
    assert manifest["created_at"]
    snapshots = [yaml.safe_load(path.read_text(encoding="utf-8")) for path in snapshot_files]
    by_id = {snapshot["agent_id"]: snapshot for snapshot in snapshots}
    assert by_id["corso1"]["metadata"] == {"public": True}
    assert by_id["corso2"]["metadata"] == {}


def test_backup_snapshots_and_manifest_are_yaml_0600_and_restore_reads_them(tmp_path):
    client = FakeClient(agents=[_agent("corso1", {"public": True})])

    rc = cli.main(
        ["--agent", "corso", "--metadata", "label=b", "--yes", "--backup-out", str(tmp_path)],
        client_factory=lambda: client,
    )

    assert rc == 0
    snapshot_files = _snapshot_files(tmp_path)
    assert [path.suffix for path in snapshot_files] == [".yml"]
    manifest_path = tmp_path / "manifest.yml"
    assert manifest_path.is_file()
    assert not (tmp_path / "manifest.json").exists()
    for path in [*snapshot_files, manifest_path]:
        assert path.stat().st_mode & 0o777 == 0o600
        assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict)

    client.calls.clear()
    restored = cli.main(["--restore", str(tmp_path), "--yes"], client_factory=lambda: client)

    assert restored == 0
    assert client.calls_to("put_agent") == [("corso1", {"public": True})]


def test_restore_accepts_a_single_yaml_file_with_any_extension(tmp_path):
    client = FakeClient(agents=[_agent("corso1", {"public": True})])
    snapshot = {
        "agent_id": "corso1",
        "metadata": {"public": True},
        "llm": {},
        "plugins": {},
        "categories": {},
    }
    snapshot_file = tmp_path / "snapshot.txt"
    snapshot_file.write_text(yaml.safe_dump(snapshot), encoding="utf-8")

    rc = cli.main(["--restore", str(snapshot_file), "--yes"], client_factory=lambda: client)

    assert rc == 0
    assert client.calls_to("put_agent") == [("corso1", {"public": True})]


def test_snapshot_round_trip_restores_the_original_metadata(tmp_path):
    client = FakeClient(agents=[_agent("corso1", {"public": True, "label": "a"})])

    first = cli.main(
        ["--agent", "corso", "--name", "X", "--yes", "--backup-out", str(tmp_path)],
        client_factory=lambda: client,
    )
    assert first == 0
    assert client.calls_to("put_agent") == [("corso1", {"public": True, "label": "a", "name": "X"})]

    client.calls.clear()
    second = cli.main(["--restore", str(tmp_path), "--yes"], client_factory=lambda: client)
    assert second == 0
    assert client.calls_to("put_agent") == [("corso1", {"public": True, "label": "a"})]

    client.calls.clear()
    third = cli.main(["--restore", str(tmp_path), "--yes"], client_factory=lambda: client)
    assert third == 0
    assert client.calls_to("put_agent") == [("corso1", {"public": True, "label": "a"})]


def test_unwritable_backup_dir_blocks_every_write():
    client = FakeClient(agents=[_agent("corso1"), _agent("corso2")])

    rc = cli.main(
        ["--agent", "corso", "--metadata", "label=b", "--yes", "--backup-out", "/proc/nope"],
        client_factory=lambda: client,
    )

    assert rc == 1
    assert [name for name, _args in client.calls if name.startswith("put_")] == []


def test_restore_dry_run_exits_2_without_writes(tmp_path):
    client = FakeClient(agents=[_agent("corso1", {"public": True})])
    cli.main(
        ["--agent", "corso", "--name", "X", "--yes", "--backup-out", str(tmp_path)],
        client_factory=lambda: client,
    )
    client.calls.clear()

    rc = cli.main(["--restore", str(tmp_path)], client_factory=lambda: client)

    assert rc == 2
    assert [name for name, _args in client.calls if name.startswith("put_")] == []


def test_max_workers_matches_sequential_results(capsys):
    agents = [_agent("corso1"), _agent("corso2"), _agent("corso3")]
    sequential = FakeClient(agents=agents)
    parallel = FakeClient(agents=agents, concurrency_guard=True)

    rc1 = cli.main(
        ["--agent", "corso", "--metadata", "label=b", "--yes", "--yaml", "--max-workers", "1"],
        client_factory=lambda: sequential,
    )
    first = yaml.safe_load(capsys.readouterr().out)
    rc4 = cli.main(
        ["--agent", "corso", "--metadata", "label=b", "--yes", "--yaml", "--max-workers", "4"],
        client_factory=lambda: parallel,
    )
    second = yaml.safe_load(capsys.readouterr().out)

    assert (rc1, rc4) == (0, 0)
    assert first == second


def test_mixed_run_summary_counts_skipped_and_failed(capsys):
    client = FakeClient(
        agents=[_agent("corso1"), _agent("corso2"), _agent("corso3")],
        plugins={"corso1": {"foo": True}, "corso2": {"foo": False}, "corso3": {}},
    )

    rc = cli.main(["--agent", "corso", "--plugin", "foo=on", "--yes", "--yaml"], client_factory=lambda: client)

    assert rc == 1
    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload["summary"] == {"selected": 3, "ok": 2, "failed": 1, "skipped": 1}


def test_ingestion_system_wide_merges_once(capsys):
    client = FakeClient(
        agents=[_agent("corso1"), _agent("corso2")],
        settings={("ingestion", "default"): {"engine": "core"}},
    )

    rc = cli.main(
        [
            "--agent",
            "corso",
            "--set",
            "ingestion:default:batch_size=8",
            "--allow-system-wide",
            "--yes",
            "--yaml",
        ],
        client_factory=lambda: client,
    )

    assert rc == 0
    assert client.calls_to("put_ingestion_settings") == [("default", {"engine": "core", "batch_size": 8})]
    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload["system_wide"] == [{"op": "system:ingestion:default", "status": "ok", "detail": "merged"}]


def test_llm_get_failure_fails_only_that_agent(capsys):
    client = FakeClient(
        agents=[_agent("corso1"), _agent("corso2")],
        settings={("large_language_model", "gpt-4"): {"model": "gpt-4", "temperature": 0.0}},
        fail_on={("get_large_language_model_settings", "corso1")},
    )

    rc = cli.main(
        ["--agent", "corso", "--llm", 'gpt-4={"temperature": 0.2}', "--yes", "--yaml"],
        client_factory=lambda: client,
    )

    assert rc == 1
    payload = yaml.safe_load(capsys.readouterr().out)
    by_id = {result["agent_id"]: result for result in payload["agents"]}
    assert by_id["corso1"]["ok"] is False
    assert by_id["corso2"]["ok"] is True
    assert client.calls_to("put_large_language_model_settings") == [
        ("gpt-4", "corso2", {"model": "gpt-4", "temperature": 0.2})
    ]


def test_plugin_disappearing_after_toggle_fails_the_agent(capsys):
    client = FakeClient(
        agents=[_agent("corso1")],
        plugins={"corso1": {"foo": False}},
        disappearing_plugins={"foo"},
    )

    rc = cli.main(["--agent", "corso", "--plugin", "foo=on", "--yes"], client_factory=lambda: client)

    assert rc == 1
    assert client.calls_to("put_toggle_plugin") == [("foo", "corso1")]
    assert "disappeared" in capsys.readouterr().out


def test_restore_writes_back_llm_and_chunking_values(tmp_path):
    client = FakeClient(
        agents=[_agent("corso1")],
        settings={
            ("large_language_model", "gpt-4"): {"model": "gpt-4", "temperature": 0.0},
            ("chunker", "default"): {"chunk_size": 256, "chunk_overlap": 32},
        },
    )

    first = cli.main(
        [
            "--agent",
            "corso",
            "--llm",
            'gpt-4={"temperature": 0.2}',
            "--set",
            "chunking:default:chunk_size=512",
            "--yes",
            "--backup-out",
            str(tmp_path),
        ],
        client_factory=lambda: client,
    )
    assert first == 0
    assert client.settings[("large_language_model", "gpt-4")] == {"model": "gpt-4", "temperature": 0.2}
    assert client.settings[("chunker", "default")] == {"chunk_size": 512, "chunk_overlap": 32}

    client.settings[("large_language_model", "gpt-4")] = {"model": "gpt-4", "temperature": 0.9}
    client.settings[("chunker", "default")] = {"chunk_size": 1024, "chunk_overlap": 32}
    client.calls.clear()

    second = cli.main(["--restore", str(tmp_path), "--yes"], client_factory=lambda: client)

    assert second == 0
    assert client.calls_to("put_large_language_model_settings") == [
        ("gpt-4", "corso1", {"model": "gpt-4", "temperature": 0.0})
    ]
    assert client.calls_to("put_chunker_settings") == [
        ("default", "corso1", {"chunk_size": 256, "chunk_overlap": 32})
    ]


def test_restore_reconciles_plugin_active_state_and_settings(tmp_path):
    client = FakeClient(
        agents=[_agent("corso1")],
        plugins={"corso1": {"foo": True}},
        plugin_settings={("corso1", "foo"): {"k": "v"}},
    )

    first = cli.main(
        ["--agent", "corso", "--plugin", "foo=off", "--yes", "--backup-out", str(tmp_path)],
        client_factory=lambda: client,
    )
    assert first == 0
    assert client.plugin_states["corso1"]["foo"] is False

    client.calls.clear()
    second = cli.main(["--restore", str(tmp_path), "--yes"], client_factory=lambda: client)

    assert second == 0
    assert client.plugin_states["corso1"]["foo"] is True
    assert client.calls_to("put_plugin_settings") == [("foo", "corso1", {"k": "v"})]


def test_snapshot_contains_metadata_llm_plugins_and_categories(tmp_path):
    client = FakeClient(
        agents=[_agent("corso1", {"public": True})],
        settings={
            ("large_language_model", "gpt-4"): {"model": "gpt-4", "temperature": 0.0},
            ("chunker", "default"): {"chunk_size": 256},
        },
        plugins={"corso1": {"foo": True}},
        plugin_settings={("corso1", "foo"): {"k": "v"}},
    )

    rc = cli.main(
        [
            "--agent",
            "corso",
            "--metadata",
            "label=b",
            "--llm",
            'gpt-4={"temperature": 0.2}',
            "--plugin",
            "foo=off",
            "--set",
            "chunking:default:chunk_size=512",
            "--yes",
            "--backup-out",
            str(tmp_path),
        ],
        client_factory=lambda: client,
    )

    assert rc == 0
    files = _snapshot_files(tmp_path)
    assert len(files) == 1
    snapshot = yaml.safe_load(files[0].read_text(encoding="utf-8"))
    assert snapshot["metadata"] == {"public": True}
    assert snapshot["llm"] == {"gpt-4": {"model": "gpt-4", "temperature": 0.0}}
    assert snapshot["plugins"] == {"foo": {"active": True, "settings": {"k": "v"}}}
    assert snapshot["categories"] == {"chunking": {"default": {"chunk_size": 256}}}


def test_yaml_report_for_a_failed_agent_carries_the_detail(capsys):
    client = FakeClient(agents=[_agent("corso1")], updated_false={"corso1"})

    rc = cli.main(
        ["--agent", "corso", "--metadata", "label=b", "--yes", "--yaml"],
        client_factory=lambda: client,
    )

    assert rc == 1
    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload["agents"][0]["ok"] is False
    failed = [operation for operation in payload["agents"][0]["ops"] if operation["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["detail"] == "updated=false for corso1"
    assert payload["summary"]["failed"] == 1


def test_all_with_exclude_writes_only_the_remaining_agents():
    client = FakeClient(
        agents=[
            _agent("system"),
            _agent("__internal"),
            _agent("corso1"),
            _agent("corso2_test"),
            _agent("other"),
        ]
    )

    rc = cli.main(
        ["--all", "--exclude", "test", "--metadata", "label=b", "--yes"],
        client_factory=lambda: client,
    )

    assert rc == 0
    assert sorted(args[0] for args in client.calls_to("put_agent")) == ["corso1", "other"]


def test_restore_of_a_missing_agent_fails_without_writes(tmp_path, capsys):
    client = FakeClient(agents=[_agent("corso1", {"public": True})])
    first = cli.main(
        ["--agent", "corso", "--name", "X", "--yes", "--backup-out", str(tmp_path)],
        client_factory=lambda: client,
    )
    assert first == 0
    capsys.readouterr()

    gone = FakeClient(agents=[])
    rc = cli.main(["--restore", str(tmp_path), "--yes", "--yaml"], client_factory=lambda: gone)

    assert rc == 1
    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload["agents"][0]["ok"] is False
    assert payload["agents"][0]["error"] == "agent not found on the server"
    assert payload["summary"]["failed"] == 1
    assert [name for name, _args in gone.calls if name.startswith("put_")] == []


def test_set_nested_patch_keeps_nested_siblings():
    client = FakeClient(
        agents=[_agent("corso1")],
        settings={("large_language_model", "gpt-4"): {"model": "gpt-4", "options": {"a": 1, "b": 2}}},
    )

    rc = cli.main(
        ["--agent", "corso", "--set", "llm:gpt-4:options.a=9", "--yes"],
        client_factory=lambda: client,
    )

    assert rc == 0
    assert client.calls_to("put_large_language_model_settings") == [
        ("gpt-4", "corso1", {"model": "gpt-4", "options": {"a": 9, "b": 2}})
    ]


def test_llm_patch_merges_nested_objects():
    client = FakeClient(
        agents=[_agent("corso1")],
        settings={("large_language_model", "gpt-4"): {"model": "gpt-4", "options": {"a": 1, "b": 2}}},
    )

    rc = cli.main(
        ["--agent", "corso", "--llm", 'gpt-4={"options": {"a": 7}}', "--yes"],
        client_factory=lambda: client,
    )

    assert rc == 0
    assert client.calls_to("put_large_language_model_settings") == [
        ("gpt-4", "corso1", {"model": "gpt-4", "options": {"a": 7, "b": 2}})
    ]


def test_update_metadata_deep_merges_and_keeps_siblings():
    client = FakeClient()
    agent = _agent("corso1", {"opts": {"x": 1, "y": 2}})

    merged = cli.update_metadata(client, agent, {"opts": {"x": 5}})

    assert merged == {"opts": {"x": 5, "y": 2}}
    assert client.calls_to("put_agent") == [("corso1", {"opts": {"x": 5, "y": 2}})]


def test_system_wide_merge_keeps_nested_siblings():
    client = FakeClient(
        agents=[_agent("corso1")],
        settings={("embedder", "default"): {"model": "m", "options": {"a": 1, "b": 2}}},
    )

    rc = cli.main(
        ["--agent", "corso", "--set", "embedder:default:options.a=9", "--allow-system-wide", "--yes"],
        client_factory=lambda: client,
    )

    assert rc == 0
    assert client.calls_to("put_embedder_settings") == [
        ("default", {"model": "m", "options": {"a": 9, "b": 2}})
    ]


def test_snapshot_filenames_do_not_collide(tmp_path):
    client = FakeClient(
        agents=[_agent("a/b", {"which": "slash"}), _agent("a_b", {"which": "underscore"})]
    )

    rc = cli.main(
        ["--agent", "a", "--metadata", "label=x", "--yes", "--backup-out", str(tmp_path)],
        client_factory=lambda: client,
    )

    assert rc == 0
    files = _snapshot_files(tmp_path)
    expected = sorted(
        [
            f"a_b-{hashlib.sha1(b'a/b').hexdigest()[:8]}.yml",
            f"a_b-{hashlib.sha1(b'a_b').hexdigest()[:8]}.yml",
        ]
    )
    assert [path.name for path in files] == expected
    by_id = {yaml.safe_load(path.read_text(encoding="utf-8"))["agent_id"]: path for path in files}
    assert set(by_id) == {"a/b", "a_b"}
    assert yaml.safe_load(by_id["a/b"].read_text(encoding="utf-8"))["metadata"] == {"which": "slash"}
    assert yaml.safe_load(by_id["a_b"].read_text(encoding="utf-8"))["metadata"] == {"which": "underscore"}


def test_manifest_stays_valid_with_parallel_writers(tmp_path):
    agents = [_agent(f"corso{index}") for index in range(8)]
    client = FakeClient(agents=agents)

    rc = cli.main(
        [
            "--all",
            "--metadata",
            "label=b",
            "--yes",
            "--backup-out",
            str(tmp_path),
            "--max-workers",
            "4",
        ],
        client_factory=lambda: client,
    )

    assert rc == 0
    manifest = yaml.safe_load((tmp_path / "manifest.yml").read_text(encoding="utf-8"))
    assert sorted(manifest["agents"]) == [f"corso{index}" for index in range(8)]
    assert len(cli._load_snapshots(str(tmp_path))) == 8


def test_duplicate_snapshots_are_applied_once(tmp_path, capsys):
    client = FakeClient(agents=[_agent("corso1")], plugins={"corso1": {"foo": False}})
    snapshot = {
        "agent_id": "corso1",
        "metadata": {},
        "llm": {},
        "plugins": {"foo": {"active": True, "settings": {}}},
        "categories": {},
    }
    (tmp_path / "a.yml").write_text(yaml.safe_dump(snapshot), encoding="utf-8")
    (tmp_path / "b.yml").write_text(yaml.safe_dump(snapshot), encoding="utf-8")

    rc = cli.main(["--restore", str(tmp_path), "--yes", "--yaml"], client_factory=lambda: client)

    assert rc == 0
    assert client.calls_to("put_toggle_plugin") == [("foo", "corso1")]
    assert client.plugin_states["corso1"]["foo"] is True
    assert len(client.calls_to("put_agent")) == 1
    err = capsys.readouterr().err
    assert "duplicate" in err
    assert "corso1" in err


def test_yaml_patch_with_hash_comments_parses_and_applies(tmp_path):
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text(
        "# the model to use\n"
        "llm:\n"
        "  gpt-4:\n"
        "    temperature: 0.2  # keep the model name untouched\n",
        encoding="utf-8",
    )
    client = FakeClient(
        agents=[_agent("corso1")],
        settings={("large_language_model", "gpt-4"): {"model": "gpt-4"}},
    )

    dry = cli.main(["--agent", "corso", "--patch", str(patch_file)], client_factory=lambda: client)

    assert dry == 2
    assert client.calls == [("get_agents", ())]

    rc = cli.main(["--agent", "corso", "--patch", str(patch_file), "--yes"], client_factory=lambda: client)

    assert rc == 0
    assert client.calls_to("put_large_language_model_settings") == [
        ("gpt-4", "corso1", {"model": "gpt-4", "temperature": 0.2})
    ]


def test_yaml_quoted_hash_inside_a_value_is_preserved(tmp_path):
    # YAML starts a comment at a '#' preceded by whitespace, so a value that
    # contains '#' must be quoted; the shipped examples always quote it.
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text('llm:\n  LLMOpenRouterConfig:\n    api_key: "sk-abc#def"\n', encoding="utf-8")

    patches, active_plugins = cli.load_patch(str(patch_file))

    assert patches[("llm", "LLMOpenRouterConfig")] == {"api_key": "sk-abc#def"}
    assert active_plugins == {}


def test_yaml_leaves_have_no_implicit_typing(tmp_path):
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text(
        "chunking:\n"
        "  default:\n"
        "    chunk_size: 0123\n"
        "    temperature: .5\n"
        "    version: 1.10\n"
        "    label: on\n"
        "    date: 2024-01-01\n"
        '    enabled: "true"\n'
        '    retries: "1"\n'
        '    ratio: "0.2"\n',
        encoding="utf-8",
    )

    patches, _active_plugins = cli.load_patch(str(patch_file))

    assert patches[("chunking", "default")] == {
        # BaseLoader hands every scalar over as a string, so there is no YAML
        # octal/bool/date coercion. _parse_value only accepts JSON numbers:
        # 0123 (leading zero) and .5 (no leading digit) stay literal strings.
        "chunk_size": "0123",
        "temperature": ".5",
        "version": 1.1,
        "label": "on",
        "date": "2024-01-01",
        # Quoted scalars still reach _parse_value, which decodes the JSON
        # literals true/1/0.2 into a bool, an int and a float.
        "enabled": True,
        "retries": 1,
        "ratio": 0.2,
    }
    assert not isinstance(patches[("chunking", "default")]["label"], bool)


def test_active_plugins_accepts_yaml_string_states(tmp_path):
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text("active_plugins: {foo: on, bar: off}\n", encoding="utf-8")
    client = FakeClient(
        agents=[_agent("corso1")],
        plugins={"corso1": {"foo": False, "bar": True}},
    )

    first = cli.main(["--agent", "corso", "--patch", str(patch_file), "--yes"], client_factory=lambda: client)
    second = cli.main(["--agent", "corso", "--patch", str(patch_file), "--yes"], client_factory=lambda: client)

    assert (first, second) == (0, 0)
    assert sorted(client.calls_to("put_toggle_plugin")) == [("bar", "corso1"), ("foo", "corso1")]
    assert client.plugin_states["corso1"] == {"foo": True, "bar": False}


def test_active_plugins_unknown_state_exits_2_without_calls(tmp_path, capsys):
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text("active_plugins: {foo: maybe}\n", encoding="utf-8")
    client = FakeClient(agents=[_agent("corso1")])

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--agent", "corso", "--patch", str(patch_file), "--yes"], client_factory=lambda: client)

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "active_plugins" in err
    assert "foo" in err
    assert "maybe" in err
    assert client.calls == []


def test_yaml_multiple_documents_exit_2(tmp_path, capsys):
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text("llm:\n  gpt-4: {temperature: 0.2}\n---\nllm:\n  gpt-4: {temperature: 0.3}\n", encoding="utf-8")
    client = FakeClient(agents=[_agent("corso1")])

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--agent", "corso", "--patch", str(patch_file), "--yes"], client_factory=lambda: client)

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "multiple documents separated by ---" in err
    assert str(patch_file) in err
    assert client.calls == []


def test_yaml_empty_file_exits_2(tmp_path, capsys):
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text("# only a comment\n", encoding="utf-8")
    client = FakeClient(agents=[_agent("corso1")])

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--agent", "corso", "--patch", str(patch_file), "--yes"], client_factory=lambda: client)

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "empty" in err
    assert str(patch_file) in err
    assert client.calls == []


def test_non_yaml_patch_extension_is_rejected(tmp_path, capsys):
    # Built at runtime so the tree does not advertise a format it rejects.
    legacy_suffix = "".join(("json",))
    patch_file = tmp_path / f"patch.{legacy_suffix}"
    patch_file.write_text("llm:\n  gpt-4:\n    temperature: 0.2\n", encoding="utf-8")
    client = FakeClient(agents=[_agent("corso1")])

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--agent", "corso", "--patch", str(patch_file), "--yes"], client_factory=lambda: client)

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert f"invalid --patch '{patch_file}'" in err
    assert "patch files must be YAML (.yml or .yaml); JSON and JSONC are not supported" in err
    assert client.calls == []


def test_legacy_comment_extension_is_rejected(tmp_path, capsys):
    legacy_suffix = "".join(("json", "c"))
    patch_file = tmp_path / f"patch.{legacy_suffix}"
    patch_file.write_text("llm:\n  gpt-4:\n    temperature: 0.2\n", encoding="utf-8")
    client = FakeClient(agents=[_agent("corso1")])

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--agent", "corso", "--patch", str(patch_file), "--yes"], client_factory=lambda: client)

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "patch files must be YAML (.yml or .yaml); JSON and JSONC are not supported" in err
    assert client.calls == []


@pytest.mark.parametrize("suffix", [".yml", ".yaml", ".YML", ".YAML"])
def test_yaml_suffixes_are_accepted(tmp_path, suffix):
    patch_file = tmp_path / f"patch{suffix}"
    patch_file.write_text("llm:\n  gpt-4:\n    temperature: 0.2\n", encoding="utf-8")

    patches, active_plugins = cli.load_patch(str(patch_file))

    assert patches == {("llm", "gpt-4"): {"temperature": 0.2}}
    assert active_plugins == {}


def test_slash_comments_exit_2_with_a_neutral_hint(tmp_path, capsys):
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text(
        '{\n  // comment\n  "llm": {"gpt-4": {"temperature": 0.2}}\n}\n',
        encoding="utf-8",
    )
    client = FakeClient(agents=[_agent("corso1")])

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--agent", "corso", "--patch", str(patch_file), "--yes"], client_factory=lambda: client)

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "comment syntax in patch files is '#', not '//'" in err
    assert "JSONC" not in err
    assert str(patch_file) in err
    assert client.calls == []


EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"


def test_shipped_llm_example_loads_through_load_patch():
    patches, active_plugins = cli.load_patch(str(EXAMPLES_DIR / "patch-llm-openrouter.yml"))

    assert set(patches) == {("llm", "LLMOpenRouterConfig")}
    assert patches[("llm", "LLMOpenRouterConfig")] == {
        "model": "anthropic/claude-3.5-sonnet",
        "api_key": "sk-or-REPLACE-ME",
        "temperature": 0.2,
    }
    assert active_plugins == {}


def test_shipped_full_agent_example_loads_through_load_patch():
    patches, active_plugins = cli.load_patch(str(EXAMPLES_DIR / "patch-full-agent.yml"))

    assert set(patches) == {
        ("metadata", ""),
        ("llm", "LLMOpenRouterConfig"),
        ("chunking", "default"),
        ("plugins", "my_plugin"),
        ("plugins", "other_plugin"),
    }
    assert patches[("metadata", "")] == {"name": "Corso 1", "label": "production", "owner": "didattica"}
    assert patches[("llm", "LLMOpenRouterConfig")] == {
        "model": "anthropic/claude-3.5-sonnet",
        "temperature": 0.2,
        "max_tokens": 1024,
    }
    assert patches[("chunking", "default")] == {"chunk_size": 512, "chunk_overlap": 32}
    assert patches[("plugins", "my_plugin")] == {"options": {"enabled": True, "threshold": 0.7}}
    assert patches[("plugins", "other_plugin")] == {
        "api_key": "REPLACE-ME",
        "http": {"timeout": {"seconds": 30}},
    }
    assert active_plugins == {"my_plugin": True, "other_plugin": False}


def test_shipped_full_agent_example_produces_the_expected_operations(capsys):
    client = FakeClient(agents=[_agent("corso1")])

    rc = cli.main(
        ["--agent", "corso", "--patch", str(EXAMPLES_DIR / "patch-full-agent.yml"), "--yaml"],
        client_factory=lambda: client,
    )

    assert rc == 2
    plan = yaml.safe_load(capsys.readouterr().out)
    assert plan["agents"][0]["ops"] == [
        "metadata",
        "plugin:my_plugin",
        "plugin:other_plugin",
        "settings:llm:LLMOpenRouterConfig",
        "settings:chunking:default",
        "settings:plugins:my_plugin",
        "settings:plugins:other_plugin",
    ]
    assert plan["agents"][0]["metadata_keys"] == ["label", "name", "owner"]
    assert client.calls == [("get_agents", ())]


def test_examples_use_real_schema_titles():
    for name in ("patch-llm-openrouter.yml", "patch-full-agent.yml"):
        text = (EXAMPLES_DIR / name).read_text(encoding="utf-8")
        assert "LLMOpenRouterConfig" in text
        assert not any(line.strip() == "openrouter:" for line in text.splitlines())


def test_help_uses_real_llm_config_names(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"], client_factory=_forbidden_factory)

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "llm:LLMOpenRouterConfig:model=" in out
    legacy_name = "llm:" + "openrouter:"
    assert legacy_name not in out


def test_help_and_readme_explain_where_llm_names_come_from(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"], client_factory=_forbidden_factory)

    assert excinfo.value.code == 0
    help_text = capsys.readouterr().out
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    for text in (help_text, readme):
        assert "GET /llm/settings" in text
        assert "humanReadableName" in text
        assert "not supported. Must be one of" in text


def test_yaml_anchors_and_aliases_are_allowed(tmp_path):
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text(
        "llm:\n"
        "  LLMOpenRouterConfig: &base\n"
        "    temperature: 0.2\n"
        "  mirror: *base\n",
        encoding="utf-8",
    )

    patches, _active_plugins = cli.load_patch(str(patch_file))

    assert patches[("llm", "LLMOpenRouterConfig")] == {"temperature": 0.2}
    assert patches[("llm", "mirror")] == {"temperature": 0.2}


def test_cli_plugin_beats_active_plugins_in_the_operation_payload(tmp_path):
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text("active_plugins: {foo: false, bar: true}\n", encoding="utf-8")

    args = Namespace(
        patch=str(patch_file),
        set=[],
        metadata=[],
        name=None,
        llm=[],
        plugin=["foo=on"],
        allow_system_wide=False,
    )

    operations, system_wide = cli._build_operations(args)

    plugin_ops = [(operation.name, operation.payload) for operation in operations if operation.kind == "plugin"]
    assert plugin_ops == [("foo", True), ("bar", True)]
    assert system_wide == []
    assert not [
        operation for operation in operations if operation.kind == "settings" and operation.category == "active_plugins"
    ]


def test_cli_plugin_beats_active_plugins_when_applied(tmp_path):
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text("active_plugins: {foo: false}\n", encoding="utf-8")
    client = FakeClient(agents=[_agent("corso1")], plugins={"corso1": {"foo": False}})

    rc = cli.main(
        ["--agent", "corso", "--patch", str(patch_file), "--plugin", "foo=on", "--yes"],
        client_factory=lambda: client,
    )

    assert rc == 0
    assert client.calls_to("put_toggle_plugin") == [("foo", "corso1")]
    assert client.plugin_states["corso1"]["foo"] is True


# --- Todo 1: dump flags and validation -------------------------------------


def test_help_lists_the_dump_flags(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"], client_factory=_forbidden_factory)

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--dump", "--dump-only", "--dump-secrets", "--diff", "--dump-manifest-only"):
        assert flag in out


def test_dump_is_exclusive_with_yes(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--dump", "/tmp/x", "--all", "--yes"], client_factory=_forbidden_factory)

    assert excinfo.value.code == 2
    assert "--yes" in capsys.readouterr().err


def test_dump_is_exclusive_with_operations(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--dump", "/tmp/x", "--all", "--llm", "gpt-4={}"], client_factory=_forbidden_factory)

    assert excinfo.value.code == 2
    assert "--llm" in capsys.readouterr().err


def test_dump_is_exclusive_with_restore(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--dump", "/tmp/x", "--all", "--restore", "snap.yml"], client_factory=_forbidden_factory)

    assert excinfo.value.code == 2
    assert "--restore" in capsys.readouterr().err


@pytest.mark.parametrize(
    "flag",
    [["--dump-only", "llm"], ["--dump-secrets"], ["--diff"], ["--dump-manifest-only"]],
)
def test_dump_modifiers_require_dump(flag, capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--all", *flag], client_factory=_forbidden_factory)

    assert excinfo.value.code == 2
    assert "--dump" in capsys.readouterr().err


def test_diff_requires_an_existing_directory(tmp_path, capsys):
    missing = tmp_path / "missing"

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--dump", str(missing), "--all", "--diff"], client_factory=_forbidden_factory)

    assert excinfo.value.code == 2
    assert "--diff" in capsys.readouterr().err


def test_dump_requires_a_selection(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--dump", "/tmp/x"], client_factory=_forbidden_factory)

    assert excinfo.value.code == 2
    assert "--agent" in capsys.readouterr().err


# --- Todo 2: dump selection includes system and widgets --------------------


def test_select_agents_include_internal_all_keeps_system_and_widgets():
    agents = [_agent("system"), _agent("__widget1"), _agent("corso")]

    selected = cli.select_agents(agents, [], True, [], include_internal=True)

    assert [agent.agent_id for agent in selected] == ["system", "__widget1", "corso"]


def test_select_agents_without_include_internal_still_skips_internal():
    agents = [_agent("system"), _agent("__widget1"), _agent("corso")]

    selected = cli.select_agents(agents, [], True, [])

    assert [agent.agent_id for agent in selected] == ["corso"]


def test_select_agents_include_internal_substring_matches_a_widget():
    agents = [_agent("system"), _agent("__widget1"), _agent("corso")]

    selected = cli.select_agents(agents, ["widget"], False, [], include_internal=True)

    assert [agent.agent_id for agent in selected] == ["__widget1"]


def test_select_agents_include_internal_still_applies_exclude():
    agents = [_agent("system"), _agent("__widget1"), _agent("corso")]

    selected = cli.select_agents(agents, [], True, ["__"], include_internal=True)

    assert [agent.agent_id for agent in selected] == ["system", "corso"]


@pytest.mark.parametrize(
    ("agent_id", "role"),
    [("system", "system"), ("__widget1", "widget"), ("corso", "agent")],
)
def test_agent_role(agent_id, role):
    assert cli._agent_role(agent_id) == role


# --- Todo 3: read the full configuration through the SDK -------------------


def _factory_list(selected, values):
    return FactoryObjectSettingsOutput(
        settings=[
            FactoryObjectSettingOutput(name=name, value=value) for name, value in values.items()
        ],
        selected_configuration=selected,
    )


def test_read_factory_selected_returns_the_selected_value():
    client = FakeClient(
        factory_lists={
            ("large_language_model", "corso"): _factory_list(
                "LLMOpenRouterConfig",
                {"LLMOpenRouterConfig": {"model": "x"}, "Other": {"model": "y"}},
            )
        }
    )

    name, value = cli._read_factory_selected(client, "llm", "corso")

    assert name == "LLMOpenRouterConfig"
    assert value == {"model": "x"}


def test_read_factory_selected_without_a_selection_raises():
    client = FakeClient(
        factory_lists={("large_language_model", "corso"): _factory_list("", {"A": {}})}
    )

    with pytest.raises(ValueError, match="no selected configuration for llm on corso"):
        cli._read_factory_selected(client, "llm", "corso")


def test_read_factory_selected_with_a_missing_name_raises():
    client = FakeClient(
        factory_lists={("large_language_model", "corso"): _factory_list("Missing", {"A": {}})}
    )

    with pytest.raises(ValueError, match="no selected configuration for llm on corso"):
        cli._read_factory_selected(client, "llm", "corso")


def test_read_factory_selected_reads_system_wide_categories():
    client = FakeClient(
        factory_lists={
            ("embedder", None): _factory_list("OpenAIEmbedder", {"OpenAIEmbedder": {"api_key": "sk-1"}}),
            ("ingestion", None): _factory_list("Ingestion", {"Ingestion": {"batch": 10}}),
        }
    )

    assert cli._read_factory_selected(client, "embedder", None) == (
        "OpenAIEmbedder",
        {"api_key": "sk-1"},
    )
    assert cli._read_factory_selected(client, "ingestion", None) == ("Ingestion", {"batch": 10})


def test_read_factory_selected_rejects_a_system_wide_category_with_an_agent():
    client = FakeClient()

    with pytest.raises(ValueError, match="system-wide"):
        cli._read_factory_selected(client, "embedder", "corso")


def test_strip_secrets_omits_secret_keys_recursively():
    value = {
        "api_key": "sk-1",
        "API-KEY": "sk-2",
        "nested": {"password": "p", "model": "m"},
        "items": [{"token": "t", "keep": 1}],
        "secret": "s",
    }

    assert cli._strip_secrets(value) == {"nested": {"model": "m"}, "items": [{"keep": 1}]}


def test_dump_agent_config_reads_every_requested_category():
    client = FakeClient(
        factory_lists={
            ("large_language_model", "corso"): _factory_list(
                "LLMOpenRouterConfig", {"LLMOpenRouterConfig": {"model": "m", "api_key": "sk-1"}}
            ),
            ("chunker", "corso"): _factory_list("Chunker", {"Chunker": {"chunk_size": 100}}),
        },
        plugins={"corso": {"foo": True, "bar": False}},
        plugin_settings={("corso", "foo"): {"opt": 1}},
    )

    payload = cli._dump_agent_config(
        client,
        _agent("corso", {"name": "Corso"}),
        ["metadata", "llm", "chunking", "plugins", "active_plugins"],
        include_secrets=False,
    )

    assert payload["metadata"] == {"name": "Corso"}
    assert payload["llm"] == {"LLMOpenRouterConfig": {"model": "m"}}
    assert payload["chunking"] == {"Chunker": {"chunk_size": 100}}
    # Inactive plugins are listed in active_plugins but their settings are not
    # read: the core answers 500 for them.
    assert payload["plugins"] == {"foo": {"opt": 1}}
    assert payload["active_plugins"] == {"foo": "on", "bar": "off"}


def test_dump_agent_config_system_includes_embedder_and_ingestion():
    client = FakeClient(
        factory_lists={
            ("embedder", None): _factory_list("OpenAIEmbedder", {"OpenAIEmbedder": {"api_key": "sk-1"}}),
            ("ingestion", None): _factory_list("Ingestion", {"Ingestion": {"batch": 10}}),
        }
    )

    payload = cli._dump_agent_config(
        client, _agent("system"), ["embedder", "ingestion"], include_secrets=False
    )

    assert payload["embedder"] == {"OpenAIEmbedder": {}}
    assert payload["ingestion"] == {"Ingestion": {"batch": 10}}


def test_dump_agent_config_keeps_secrets_when_requested():
    client = FakeClient(
        factory_lists={
            ("large_language_model", "corso"): _factory_list(
                "LLMOpenRouterConfig", {"LLMOpenRouterConfig": {"api_key": "sk-1"}}
            )
        }
    )

    payload = cli._dump_agent_config(
        client, _agent("corso"), ["llm"], include_secrets=True
    )

    assert payload["llm"]["LLMOpenRouterConfig"]["api_key"] == "sk-1"


# --- Todo 4: YAML rendering with a metadata header and atomic 0600 write ---


def test_dump_header_has_the_expected_lines_in_order():
    header = cli._dump_header(
        "corso",
        "agent",
        ["metadata", "llm"],
        {"llm": "LLMOpenRouterConfig"},
        ["--dump", "x"],
        "host1",
        "omitted",
        [],
    )

    lines = header.splitlines()
    assert lines[0] == "# dump-format: patch-v1"
    assert lines[1].startswith("# generated-at: ")
    assert lines[2] == "# generator: batch_agent_update.py --dump"
    assert lines[3] == "# agent-id: corso"
    assert lines[4] == "# agent-role: agent"
    assert lines[5] == "# categories: metadata, llm"
    assert lines[6] == "# selected: llm=LLMOpenRouterConfig"
    assert lines[7] == '# argv: ["--dump", "x"]'
    assert lines[8] == "# source-host: host1"
    assert lines[9] == "# secret-policy: omitted"
    assert lines[10] == "# warnings: none"


def test_dump_header_lists_warnings():
    header = cli._dump_header(
        "system", "system", ["embedder"], {}, [], "h", "included", ["secrets omitted (2 fields)"]
    )

    assert "# warnings: secrets omitted (2 fields)" in header


def test_render_dump_yaml_round_trips_the_payload():
    payload = {"metadata": {"name": "Corso"}, "llm": {"LLMOpenRouterConfig": {"model": "m"}}}
    header = cli._dump_header("corso", "agent", ["metadata", "llm"], {}, [], "h", "omitted", [])

    text = cli._render_dump_yaml(payload, header)

    assert text.startswith("# dump-format: patch-v1\n")
    body = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    assert yaml.safe_load(body) == payload


def test_dump_file_is_accepted_by_load_patch(tmp_path):
    payload = {"metadata": {"name": "Corso"}, "llm": {"LLMOpenRouterConfig": {"model": "m"}}}
    header = cli._dump_header("corso", "agent", ["metadata", "llm"], {}, [], "h", "omitted", [])
    path = tmp_path / cli._dump_filename("corso")

    cli._atomic_write_text(path, cli._render_dump_yaml(payload, header))
    patches, active_plugins = cli.load_patch(str(path))

    assert patches[("metadata", "")] == {"name": "Corso"}
    assert patches[("llm", "LLMOpenRouterConfig")] == {"model": "m"}
    assert active_plugins == {}


def test_atomic_write_text_sets_0600(tmp_path):
    path = tmp_path / "x.yml"

    cli._atomic_write_text(path, "hello\n")

    assert path.read_text(encoding="utf-8") == "hello\n"
    assert path.stat().st_mode & 0o777 == 0o600


def test_atomic_write_text_leaves_no_partial_file_on_failure(tmp_path):
    missing = tmp_path / "missing"

    with pytest.raises(OSError):
        cli._atomic_write_text(missing / "x.yml", "x")

    assert not missing.exists()


def test_dump_filename_is_unique_for_colliding_safe_ids():
    assert cli._dump_filename("a/b") != cli._dump_filename("a_b")
    assert cli._dump_filename("corso").endswith(".yml")


# --- Todo 5: manifest and dump orchestration -------------------------------


def _full_factory_lists(agent_id):
    values = {
        "llm": ("LLMOpenRouterConfig", {"model": "m"}),
        "chunking": ("Chunker", {"chunk_size": 100}),
        "vector_database": ("VectorDB", {"host": "h"}),
        "auth_handler": ("AuthHandler", {"mode": "x"}),
        "agentic_workflow": ("Workflow", {"steps": 1}),
        "context_retriever": ("Retriever", {"k": 3}),
        "file_manager": ("FileManager", {"root": "/tmp"}),
    }
    lists = {}
    for category, (attribute, _getter) in cli.FACTORY_LIST_GETTERS.items():
        name, value = values[category]
        lists[(attribute, agent_id)] = _factory_list(name, {name: value})
    # The system agent always exists in a deployment and --dump --all now
    # includes it, so every complete fixture carries its factory lists too.
    if agent_id != "system":
        lists.update(_system_factory_lists())
    return lists


def _system_factory_lists():
    lists = _full_factory_lists("system")
    lists[("embedder", None)] = _factory_list(
        "OpenAIEmbedder", {"OpenAIEmbedder": {"api_key": "sk-1"}}
    )
    lists[("ingestion", None)] = _factory_list("Ingestion", {"Ingestion": {"batch": 10}})
    return lists


def _dump_body(path):
    text = Path(path).read_text(encoding="utf-8")
    return yaml.safe_load("\n".join(line for line in text.splitlines() if not line.startswith("#")))


def test_dump_configs_writes_one_file_per_agent_and_a_manifest(tmp_path):
    agents = [_agent("corso"), _agent("lab")]
    lists = {**_full_factory_lists("corso"), **_full_factory_lists("lab")}
    client = FakeClient(
        agents=agents, factory_lists=lists, plugins={"corso": {"foo": True}, "lab": {}}
    )

    envelope = cli.dump_configs(
        client, agents, tmp_path, cli.DUMP_CATEGORIES, False, ["--dump", str(tmp_path)], 1
    )

    files = sorted(path.name for path in tmp_path.glob("*.yml"))
    assert files == sorted(
        [cli._dump_filename("corso"), cli._dump_filename("lab"), "manifest.yml"]
    )
    manifest = yaml.safe_load((tmp_path / "manifest.yml").read_text(encoding="utf-8"))
    assert manifest["format"] == "patch-v1"
    assert manifest["secret_policy"] == "omitted"
    assert {entry["agent_id"] for entry in manifest["agents"]} == {"corso", "lab"}
    assert manifest["counts"] == {"selected": 2, "ok": 2, "failed": 0}
    assert envelope["summary"] == {"selected": 2, "ok": 2, "failed": 0}
    for agent_id in ("corso", "lab"):
        patches, _active = cli.load_patch(str(tmp_path / cli._dump_filename(agent_id)))
        assert ("llm", "LLMOpenRouterConfig") in patches


def test_dump_configs_isolates_a_failing_agent(tmp_path):
    agents = [_agent("corso"), _agent("lab")]
    client = FakeClient(agents=agents, factory_lists=_full_factory_lists("corso"))

    envelope = cli.dump_configs(
        client, agents, tmp_path, cli.DUMP_CATEGORIES, False, [], 1
    )

    assert envelope["summary"] == {"selected": 2, "ok": 1, "failed": 1}
    assert (tmp_path / cli._dump_filename("corso")).exists()
    assert not (tmp_path / cli._dump_filename("lab")).exists()
    failed = next(entry for entry in envelope["agents"] if entry["agent_id"] == "lab")
    assert failed["ok"] is False
    assert failed["file"] is None
    assert failed["reason"]


def test_dump_manifest_is_0600(tmp_path):
    agents = [_agent("corso")]
    client = FakeClient(agents=agents, factory_lists=_full_factory_lists("corso"))

    cli.dump_configs(client, agents, tmp_path, cli.DUMP_CATEGORIES, False, [], 1)

    assert (tmp_path / "manifest.yml").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / cli._dump_filename("corso")).stat().st_mode & 0o777 == 0o600


def test_dump_configs_max_workers_matches_sequential(tmp_path):
    agents = [_agent("corso"), _agent("lab")]
    lists = {**_full_factory_lists("corso"), **_full_factory_lists("lab")}

    sequential = tmp_path / "sequential"
    parallel = tmp_path / "parallel"
    cli.dump_configs(
        FakeClient(agents=agents, factory_lists=lists), agents, sequential, cli.DUMP_CATEGORIES, False, [], 1
    )
    cli.dump_configs(
        FakeClient(agents=agents, factory_lists=lists), agents, parallel, cli.DUMP_CATEGORIES, False, [], 4
    )

    for agent_id in ("corso", "lab"):
        assert _dump_body(sequential / cli._dump_filename(agent_id)) == _dump_body(
            parallel / cli._dump_filename(agent_id)
        )


def test_main_dump_writes_files_and_reports(tmp_path, capsys):
    agents = [_agent("corso")]
    client = FakeClient(agents=agents, factory_lists=_full_factory_lists("corso"))

    rc = cli.main(["--dump", str(tmp_path), "--all"], client_factory=lambda: client)

    assert rc == 0
    out = capsys.readouterr().out
    assert "[dump] agent=corso role=agent file=" in out
    assert "result=ok" in out


def test_main_dump_failure_exits_1(tmp_path, capsys):
    agents = [_agent("corso")]
    client = FakeClient(agents=agents)

    rc = cli.main(["--dump", str(tmp_path), "--all"], client_factory=lambda: client)

    assert rc == 1
    out = capsys.readouterr().out
    assert "result=failed" in out
    assert "reason=" in out


def test_main_dump_yaml_envelope(tmp_path, capsys):
    agents = [_agent("corso")]
    client = FakeClient(agents=agents, factory_lists=_full_factory_lists("corso"))

    rc = cli.main(["--dump", str(tmp_path), "--all", "--yaml"], client_factory=lambda: client)

    assert rc == 0
    data = yaml.safe_load(capsys.readouterr().out)
    assert data["summary"] == {"selected": 2, "ok": 2, "failed": 0}
    assert data["agents"][0]["agent_id"] == "corso"
    assert data["agents"][1]["agent_id"] == "system"


def test_main_dump_all_includes_system_and_widgets(tmp_path):
    agents = [_agent("system"), _agent("__w"), _agent("corso")]
    lists = {
        **_system_factory_lists(),
        **_full_factory_lists("__w"),
        **_full_factory_lists("corso"),
    }
    client = FakeClient(agents=agents, factory_lists=lists)

    rc = cli.main(["--dump", str(tmp_path), "--all"], client_factory=lambda: client)

    assert rc == 0
    manifest = yaml.safe_load((tmp_path / "manifest.yml").read_text(encoding="utf-8"))
    roles = {entry["agent_id"]: entry["role"] for entry in manifest["agents"]}
    assert roles == {"system": "system", "__w": "widget", "corso": "agent"}


def test_main_dump_system_includes_embedder_and_ingestion(tmp_path):
    agents = [_agent("system")]
    client = FakeClient(agents=agents, factory_lists=_system_factory_lists())

    rc = cli.main(["--dump", str(tmp_path), "--agent", "system"], client_factory=lambda: client)

    assert rc == 0
    payload = _dump_body(tmp_path / cli._dump_filename("system"))
    assert "embedder" in payload
    assert "ingestion" in payload


# --- Todo 6: --dump-only category filtering --------------------------------


def test_dump_only_llm_writes_only_llm(tmp_path):
    agents = [_agent("corso")]
    client = FakeClient(
        agents=agents,
        factory_lists={
            ("large_language_model", "corso"): _factory_list(
                "LLMOpenRouterConfig", {"LLMOpenRouterConfig": {"model": "m"}}
            ),
            ("large_language_model", "system"): _factory_list(
                "LLMOpenRouterConfig", {"LLMOpenRouterConfig": {"model": "m"}}
            ),
        },
    )

    rc = cli.main(
        ["--dump", str(tmp_path), "--all", "--dump-only", "llm"],
        client_factory=lambda: client,
    )

    assert rc == 0
    path = tmp_path / cli._dump_filename("corso")
    assert list(_dump_body(path)) == ["llm"]
    assert "# categories: llm" in path.read_text(encoding="utf-8").splitlines()


def test_dump_only_unknown_category_exits_2(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            ["--dump", str(tmp_path), "--all", "--dump-only", "nope"],
            client_factory=_forbidden_factory,
        )

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "nope" in err
    assert "llm" in err


def test_dump_only_system_wide_with_a_non_system_agent_exits_2(tmp_path, capsys):
    agents = [_agent("corso")]
    client = FakeClient(agents=agents)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            ["--dump", str(tmp_path), "--all", "--dump-only", "embedder"],
            client_factory=lambda: client,
        )

    assert excinfo.value.code == 2
    assert "embedder" in capsys.readouterr().err


def test_dump_only_embedder_with_system_works(tmp_path):
    agents = [_agent("system")]
    client = FakeClient(
        agents=agents,
        factory_lists={
            ("embedder", None): _factory_list(
                "OpenAIEmbedder", {"OpenAIEmbedder": {"api_key": "sk-1"}}
            )
        },
    )

    rc = cli.main(
        ["--dump", str(tmp_path), "--agent", "system", "--dump-only", "embedder"],
        client_factory=lambda: client,
    )

    assert rc == 0
    assert list(_dump_body(tmp_path / cli._dump_filename("system"))) == ["embedder"]


def test_dump_header_categories_exclude_system_wide_for_agents(tmp_path):
    agents = [_agent("corso")]
    client = FakeClient(agents=agents, factory_lists=_full_factory_lists("corso"))

    rc = cli.main(["--dump", str(tmp_path), "--all"], client_factory=lambda: client)

    assert rc == 0
    header = (tmp_path / cli._dump_filename("corso")).read_text(encoding="utf-8").splitlines()
    categories_line = next(line for line in header if line.startswith("# categories: "))
    assert "embedder" not in categories_line
    assert "ingestion" not in categories_line


# --- Todo 7: secret policy and warnings ------------------------------------


def test_count_secret_keys_counts_recursively():
    value = {
        "api_key": "x",
        "nested": {"password": "p", "model": "m"},
        "items": [{"token": "t", "keep": 1}],
    }

    assert cli._count_secret_keys(value) == 3


def test_dump_agent_config_counts_omitted_secrets():
    client = FakeClient(
        factory_lists={
            ("large_language_model", "corso"): _factory_list(
                "LLMOpenRouterConfig", {"LLMOpenRouterConfig": {"api_key": "sk-1", "model": "m"}}
            )
        }
    )
    stats = {"omitted": 0}

    payload = cli._dump_agent_config(client, _agent("corso"), ["llm"], False, stats=stats)

    assert stats["omitted"] == 1
    assert "api_key" not in payload["llm"]["LLMOpenRouterConfig"]


def test_dump_configs_warns_about_omitted_secrets(tmp_path):
    agents = [_agent("corso")]
    client = FakeClient(
        agents=agents,
        factory_lists={
            ("large_language_model", "corso"): _factory_list(
                "LLMOpenRouterConfig", {"LLMOpenRouterConfig": {"api_key": "sk-1"}}
            )
        },
    )

    envelope = cli.dump_configs(client, agents, tmp_path, ["llm"], False, [], 1)

    entry = envelope["agents"][0]
    assert entry["omitted_secrets"] == 1
    assert entry["warnings"] == ["secrets omitted (1 fields); reload preserves them"]
    header = (tmp_path / cli._dump_filename("corso")).read_text(encoding="utf-8")
    assert "# warnings: secrets omitted (1 fields); reload preserves them" in header
    manifest = yaml.safe_load((tmp_path / "manifest.yml").read_text(encoding="utf-8"))
    assert manifest["agents"][0]["omitted_secrets"] == 1


def test_dump_configs_with_secrets_has_no_warning(tmp_path):
    agents = [_agent("corso")]
    client = FakeClient(
        agents=agents,
        factory_lists={
            ("large_language_model", "corso"): _factory_list(
                "LLMOpenRouterConfig", {"LLMOpenRouterConfig": {"api_key": "sk-1"}}
            )
        },
    )

    envelope = cli.dump_configs(client, agents, tmp_path, ["llm"], True, [], 1)

    entry = envelope["agents"][0]
    assert entry["omitted_secrets"] == 0
    assert entry["warnings"] == []
    header = (tmp_path / cli._dump_filename("corso")).read_text(encoding="utf-8")
    assert "# secret-policy: included" in header
    assert "# warnings: none" in header


# --- Todo 8: --diff against an existing dump -------------------------------


def _dump_once(tmp_path, client, *extra):
    return cli.main(["--dump", str(tmp_path), "--all", *extra], client_factory=lambda: client)


def test_diff_identical_exits_0_and_writes_nothing(tmp_path, capsys):
    agents = [_agent("corso")]
    client = FakeClient(agents=agents, factory_lists=_full_factory_lists("corso"))
    assert _dump_once(tmp_path, client) == 0
    before = {path.name: path.read_bytes() for path in tmp_path.glob("*.yml")}
    capsys.readouterr()

    rc = cli.main(
        ["--dump", str(tmp_path), "--all", "--diff"], client_factory=lambda: client
    )

    assert rc == 0
    after = {path.name: path.read_bytes() for path in tmp_path.glob("*.yml")}
    assert before == after
    assert [name for name, _args in client.calls if name.startswith("put_")] == []


def test_diff_changed_exits_1(tmp_path, capsys):
    agents = [_agent("corso")]
    client = FakeClient(agents=agents, factory_lists=_full_factory_lists("corso"))
    assert _dump_once(tmp_path, client) == 0
    path = tmp_path / cli._dump_filename("corso")
    path.write_text(
        path.read_text(encoding="utf-8").replace("model: m", "model: changed"),
        encoding="utf-8",
    )
    capsys.readouterr()

    rc = cli.main(
        ["--dump", str(tmp_path), "--all", "--diff"], client_factory=lambda: client
    )

    assert rc == 1
    out = capsys.readouterr().out
    assert "changed" in out


def test_diff_empty_directory_exits_2(tmp_path, capsys):
    agents = [_agent("corso")]
    client = FakeClient(agents=agents, factory_lists=_full_factory_lists("corso"))

    rc = cli.main(
        ["--dump", str(tmp_path), "--all", "--diff"], client_factory=lambda: client
    )

    assert rc == 2
    assert "no dump files" in capsys.readouterr().err


def test_diff_corrupt_file_exits_2(tmp_path, capsys):
    agents = [_agent("corso")]
    client = FakeClient(agents=agents, factory_lists=_full_factory_lists("corso"))
    (tmp_path / "bad.yml").write_text("not: [valid", encoding="utf-8")

    rc = cli.main(
        ["--dump", str(tmp_path), "--all", "--diff"], client_factory=lambda: client
    )

    assert rc == 2
    assert "bad.yml" in capsys.readouterr().err


def test_diff_yaml_envelope(tmp_path, capsys):
    agents = [_agent("corso")]
    client = FakeClient(agents=agents, factory_lists=_full_factory_lists("corso"))
    assert _dump_once(tmp_path, client) == 0
    capsys.readouterr()

    rc = cli.main(
        ["--dump", str(tmp_path), "--all", "--diff", "--yaml"],
        client_factory=lambda: client,
    )

    assert rc == 0
    data = yaml.safe_load(capsys.readouterr().out)
    assert data["identical"] is True
    assert data["agents"][0]["agent_id"] == "corso"
    assert data["agents"][0]["identical"] is True


def test_diff_reports_a_selected_agent_without_a_file(tmp_path, capsys):
    agents = [_agent("corso"), _agent("lab")]
    lists = {**_full_factory_lists("corso"), **_full_factory_lists("lab")}
    client = FakeClient(agents=agents, factory_lists=lists)
    assert cli.main(
        ["--dump", str(tmp_path), "--agent", "lab"], client_factory=lambda: client
    ) == 0
    capsys.readouterr()

    rc = cli.main(
        ["--dump", str(tmp_path), "--agent", "corso", "--diff"],
        client_factory=lambda: client,
    )

    assert rc == 1
    out = capsys.readouterr().out
    assert "corso" in out


# --- Todo 9: --dump-manifest-only ------------------------------------------


def test_dump_manifest_only_writes_only_the_manifest(tmp_path):
    agents = [_agent("corso")]
    client = FakeClient(
        agents=agents,
        factory_lists=_full_factory_lists("corso"),
        plugins={"corso": {"foo": True, "bar": False}},
        plugin_settings={("corso", "foo"): {"opt": 1}},
    )

    rc = cli.main(
        ["--dump", str(tmp_path), "--all", "--dump-manifest-only"],
        client_factory=lambda: client,
    )

    assert rc == 0
    assert [path.name for path in tmp_path.glob("*.yml")] == ["manifest.yml"]
    manifest = yaml.safe_load((tmp_path / "manifest.yml").read_text(encoding="utf-8"))
    entry = manifest["agents"][0]
    assert entry["file"] is None
    assert entry["plugins"] == 2
    assert entry["selected"]["llm"] == "LLMOpenRouterConfig"
    assert entry["categories"]
    assert client.calls_to("get_plugin_settings") == []


def test_dump_manifest_only_is_exclusive_with_diff(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            ["--dump", str(tmp_path), "--all", "--dump-manifest-only", "--diff"],
            client_factory=_forbidden_factory,
        )

    assert excinfo.value.code == 2
    assert "--diff" in capsys.readouterr().err


# --- Todo 10: dump/diff/manifest coverage ----------------------------------


def test_dump_round_trip_through_patch_dry_run(tmp_path, capsys):
    agents = [_agent("corso")]
    client = FakeClient(
        agents=agents,
        factory_lists=_full_factory_lists("corso"),
        plugins={"corso": {"foo": True}},
    )
    assert _dump_once(tmp_path, client) == 0
    path = tmp_path / cli._dump_filename("corso")
    capsys.readouterr()

    rc = cli.main(["--agent", "corso", "--patch", str(path)], client_factory=lambda: client)

    assert rc == 2
    assert [name for name, _args in client.calls if name.startswith("put_")] == []


def test_dump_only_plugins_and_active_plugins(tmp_path):
    agents = [_agent("corso")]
    client = FakeClient(
        agents=agents,
        plugins={"corso": {"foo": True}},
        plugin_settings={("corso", "foo"): {"opt": 1}},
    )

    rc = cli.main(
        [
            "--dump",
            str(tmp_path),
            "--all",
            "--dump-only",
            "plugins",
            "--dump-only",
            "active_plugins",
        ],
        client_factory=lambda: client,
    )

    assert rc == 0
    payload = _dump_body(tmp_path / cli._dump_filename("corso"))
    assert set(payload) == {"plugins", "active_plugins"}
    assert payload["plugins"] == {"foo": {"opt": 1}}
    assert payload["active_plugins"] == {"foo": "on"}


def test_dump_secrets_flag_keeps_secrets_in_the_file(tmp_path):
    agents = [_agent("corso")]
    client = FakeClient(
        agents=agents,
        factory_lists={
            ("large_language_model", "corso"): _factory_list(
                "LLMOpenRouterConfig", {"LLMOpenRouterConfig": {"api_key": "sk-1"}}
            ),
            ("large_language_model", "system"): _factory_list(
                "LLMOpenRouterConfig", {"LLMOpenRouterConfig": {"api_key": "sk-1"}}
            ),
        },
    )

    rc = cli.main(
        ["--dump", str(tmp_path), "--all", "--dump-only", "llm", "--dump-secrets"],
        client_factory=lambda: client,
    )

    assert rc == 0
    path = tmp_path / cli._dump_filename("corso")
    assert _dump_body(path)["llm"]["LLMOpenRouterConfig"]["api_key"] == "sk-1"
    assert "# secret-policy: included" in path.read_text(encoding="utf-8")


def test_diff_does_not_write_a_manifest(tmp_path):
    agents = [_agent("corso")]
    client = FakeClient(agents=agents, factory_lists=_full_factory_lists("corso"))
    assert _dump_once(tmp_path, client) == 0
    manifest_before = (tmp_path / "manifest.yml").read_bytes()

    cli.main(["--dump", str(tmp_path), "--all", "--diff"], client_factory=lambda: client)

    assert (tmp_path / "manifest.yml").read_bytes() == manifest_before


# --- Todo 12: end-to-end round-trip and system-wide guard ------------------

_ROUND_TRIP_AGENT_IDS = ("corso", "lab", "system", "__widget")


def _round_trip_factory_lists(agent_id, api_key=None):
    """Every category a dump reads, with a known ``temperature`` in the LLM."""
    lists = _full_factory_lists(agent_id)
    llm_value = {"model": "m", "temperature": 0.2}
    if api_key is not None:
        llm_value["api_key"] = api_key
    lists[("large_language_model", agent_id)] = _factory_list(
        "LLMOpenRouterConfig", {"LLMOpenRouterConfig": llm_value}
    )
    if agent_id == "system":
        lists[("embedder", None)] = _factory_list(
            "OpenAIEmbedder", {"OpenAIEmbedder": {"model": "e", "api_key": "sk-embed"}}
        )
        lists[("ingestion", None)] = _factory_list(
            "Ingestion", {"Ingestion": {"batch": 10}}
        )
    return lists


def _round_trip_client(agent_ids, api_key=None):
    """A fake client holding a complete, dumpable configuration per agent."""
    agent_ids = list(agent_ids)
    if "system" not in agent_ids:
        agent_ids.append("system")
    agents = [_agent(agent_id, metadata={"name": agent_id}) for agent_id in agent_ids]
    lists = {}
    for agent_id in agent_ids:
        lists.update(_round_trip_factory_lists(agent_id, api_key=api_key))
    return FakeClient(
        agents=agents,
        factory_lists=lists,
        plugins={agent_id: {"foo": True} for agent_id in agent_ids},
        plugin_settings={(agent_id, "foo"): {"enabled": True, "level": 2} for agent_id in agent_ids},
    )


def _manifest_entries(directory):
    manifest = yaml.safe_load((Path(directory) / "manifest.yml").read_text(encoding="utf-8"))
    return manifest["agents"]


def _dump_body_text(path):
    """The YAML body of a dump file, with the ``#`` metadata header removed."""
    return "\n".join(
        line for line in Path(path).read_text(encoding="utf-8").splitlines() if not line.startswith("#")
    )


def _tree_state(directory):
    """Every entry under ``directory`` with its size, mtime and content hash."""
    state = {}
    for path in sorted(Path(directory).rglob("*")):
        relative = path.relative_to(directory).as_posix()
        stat = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        state[relative] = (stat.st_size, stat.st_mtime_ns, digest)
    return state


def _put_calls(client):
    return [name for name, _args in client.calls if name.startswith("put_")]


@pytest.mark.parametrize("agent_id", _ROUND_TRIP_AGENT_IDS)
def test_dump_reload_is_a_dry_run_that_never_writes(agent_id, tmp_path, capsys, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    dump_dir = tmp_path / "dump"
    client = _round_trip_client(_ROUND_TRIP_AGENT_IDS, api_key="sk-included")

    assert (
        cli.main(
            ["--dump", str(dump_dir), "--all", "--dump-secrets"],
            client_factory=lambda: client,
        )
        == 0
    )
    capsys.readouterr()
    entry = next(item for item in _manifest_entries(dump_dir) if item["agent_id"] == agent_id)
    patch_path = dump_dir / entry["file"]
    assert patch_path.resolve().is_relative_to(dump_dir.resolve())

    rc = cli.main(
        ["--agent", agent_id, "--patch", str(patch_path), "--allow-system-wide"],
        client_factory=lambda: client,
    )

    assert rc == 2
    captured = capsys.readouterr()
    assert f"agent={agent_id} ops=" in captured.out
    assert "metadata" in captured.out
    assert "dry-run: no changes written" in captured.err
    assert _put_calls(client) == []
    assert list(work.iterdir()) == []


def test_diff_after_dump_is_clean_and_leaves_the_directory_untouched(tmp_path, capsys, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    dump_dir = tmp_path / "dump"
    client = _round_trip_client(_ROUND_TRIP_AGENT_IDS)
    assert cli.main(["--dump", str(dump_dir), "--all"], client_factory=lambda: client) == 0
    capsys.readouterr()
    before = _tree_state(dump_dir)

    rc = cli.main(["--dump", str(dump_dir), "--all", "--diff"], client_factory=lambda: client)

    assert rc == 0
    out = capsys.readouterr().out
    for agent_id in _ROUND_TRIP_AGENT_IDS:
        assert f"[diff] agent={agent_id} result=identical" in out
    assert _tree_state(dump_dir) == before
    assert _put_calls(client) == []
    assert list(work.iterdir()) == []


def test_diff_after_an_edit_reports_the_changed_key(tmp_path, capsys):
    dump_dir = tmp_path / "dump"
    client = _round_trip_client(["corso"])
    assert cli.main(["--dump", str(dump_dir), "--all"], client_factory=lambda: client) == 0
    path = dump_dir / cli._dump_filename("corso")
    original = path.read_text(encoding="utf-8")
    assert "temperature: 0.2" in original
    path.write_text(original.replace("temperature: 0.2", "temperature: 0.9"), encoding="utf-8")
    capsys.readouterr()

    rc = cli.main(["--dump", str(dump_dir), "--all", "--diff"], client_factory=lambda: client)

    assert rc == 1
    out = capsys.readouterr().out
    assert "[diff] agent=corso result=different" in out
    assert "temperature" in out
    # The dump side carries the edited value, the live side the original one.
    assert "temperature: 0.9" in out
    assert "temperature: 0.2" in out
    assert _put_calls(client) == []


def test_system_dump_contains_both_system_wide_categories(tmp_path):
    dump_dir = tmp_path / "dump"
    client = _round_trip_client(["system"])

    assert (
        cli.main(
            ["--dump", str(dump_dir), "--all", "--dump-secrets"],
            client_factory=lambda: client,
        )
        == 0
    )

    payload = _dump_body(dump_dir / cli._dump_filename("system"))
    assert "embedder" in payload
    assert "ingestion" in payload
    assert _put_calls(client) == []


def test_system_dump_reload_requires_allow_system_wide(tmp_path, capsys):
    dump_dir = tmp_path / "dump"
    client = _round_trip_client(["system"])
    assert (
        cli.main(
            ["--dump", str(dump_dir), "--all", "--dump-secrets"],
            client_factory=lambda: client,
        )
        == 0
    )
    path = dump_dir / cli._dump_filename("system")
    capsys.readouterr()

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--agent", "system", "--patch", str(path)], client_factory=lambda: client)

    assert excinfo.value.code == 2
    assert "--allow-system-wide" in capsys.readouterr().err
    assert _put_calls(client) == []


def test_system_dump_reload_with_allow_system_wide_is_a_dry_run(tmp_path, capsys):
    dump_dir = tmp_path / "dump"
    client = _round_trip_client(["system"])
    assert (
        cli.main(
            ["--dump", str(dump_dir), "--all", "--dump-secrets"],
            client_factory=lambda: client,
        )
        == 0
    )
    path = dump_dir / cli._dump_filename("system")
    capsys.readouterr()

    rc = cli.main(
        ["--agent", "system", "--patch", str(path), "--allow-system-wide"],
        client_factory=lambda: client,
    )

    assert rc == 2
    captured = capsys.readouterr()
    assert "agent=system ops=" in captured.out
    # embedder and ingestion are applied once for the whole deployment, never
    # per agent, so the plan reports them as system-wide operations.
    assert "system-wide: 2" in captured.out
    assert "dry-run: no changes written" in captured.err
    assert _put_calls(client) == []


def test_two_dumps_produce_identical_bodies(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    client = _round_trip_client(_ROUND_TRIP_AGENT_IDS, api_key="sk-included")

    assert (
        cli.main(
            ["--dump", str(first), "--all", "--dump-secrets"],
            client_factory=lambda: client,
        )
        == 0
    )
    assert (
        cli.main(
            ["--dump", str(second), "--all", "--dump-secrets"],
            client_factory=lambda: client,
        )
        == 0
    )

    names = sorted(path.name for path in first.glob("*.yml") if path.name != "manifest.yml")
    assert names == sorted(path.name for path in second.glob("*.yml") if path.name != "manifest.yml")
    assert names
    for name in names:
        assert _dump_body_text(first / name) == _dump_body_text(second / name)


def test_default_dump_omits_secrets_and_counts_them(tmp_path):
    dump_dir = tmp_path / "dump"
    client = _round_trip_client(["corso"], api_key="sk-do-not-leak")

    rc = cli.main(["--dump", str(dump_dir), "--all"], client_factory=lambda: client)

    assert rc == 0
    text = (dump_dir / cli._dump_filename("corso")).read_text(encoding="utf-8")
    assert "api_key" not in text
    assert "sk-do-not-leak" not in text
    manifest = yaml.safe_load((dump_dir / "manifest.yml").read_text(encoding="utf-8"))
    entry = manifest["agents"][0]
    assert manifest["secret_policy"] == "omitted"
    assert entry["omitted_secrets"] >= 1
    assert "secrets omitted" in entry["warnings"][0]
    assert _put_calls(client) == []


# --- F3 fix: system synthesis and inactive plugin settings -----------------


def test_ensure_system_agent_appends_a_synthetic_system_agent():
    agents = [_agent("corso")]

    ensured = cli._ensure_system_agent(agents)

    assert [agent.agent_id for agent in ensured] == ["corso", "system"]
    assert ensured[-1].metadata == {}
    assert [agent.agent_id for agent in agents] == ["corso"]


def test_ensure_system_agent_keeps_an_existing_system_agent():
    agents = [_agent("system", {"name": "System"}), _agent("corso")]

    ensured = cli._ensure_system_agent(agents)

    assert [agent.agent_id for agent in ensured] == ["system", "corso"]
    assert ensured[0].metadata == {"name": "System"}


def test_main_dump_agent_system_is_synthesized_when_the_sdk_omits_it(tmp_path, capsys):
    agents = [_agent("corso")]
    client = FakeClient(agents=agents, factory_lists=_system_factory_lists())

    rc = cli.main(["--dump", str(tmp_path), "--agent", "system"], client_factory=lambda: client)

    assert rc == 0
    out = capsys.readouterr().out
    assert "agent=system role=system" in out
    assert "result=ok" in out
    path = tmp_path / cli._dump_filename("system")
    assert path.exists()
    payload = _dump_body(path)
    assert payload["metadata"] == {}
    assert "embedder" in payload
    assert "ingestion" in payload


def test_main_dump_all_includes_the_synthesized_system_agent(tmp_path):
    agents = [_agent("corso")]
    client = FakeClient(agents=agents, factory_lists=_full_factory_lists("corso"))

    rc = cli.main(["--dump", str(tmp_path), "--all"], client_factory=lambda: client)

    assert rc == 0
    manifest = yaml.safe_load((tmp_path / "manifest.yml").read_text(encoding="utf-8"))
    roles = {entry["agent_id"]: entry["role"] for entry in manifest["agents"]}
    assert roles == {"corso": "agent", "system": "system"}


def test_update_path_never_synthesizes_the_system_agent(capsys):
    client = FakeClient(agents=[_agent("corso")])

    rc = cli.main(["--agent", "system", "--llm", "gpt-4={}"], client_factory=lambda: client)

    assert rc == 2
    assert "no agent id contains 'system'" in capsys.readouterr().err


def test_dump_agent_config_skips_inactive_plugin_settings():
    client = FakeClient(
        plugins={"corso": {"foo": True, "bar": False}},
        plugin_settings={("corso", "foo"): {"opt": 1}, ("corso", "bar"): {"opt": 2}},
        plugin_settings_500_inactive=True,
    )
    stats = {"omitted": 0, "warnings": []}

    payload = cli._dump_agent_config(
        client,
        _agent("corso"),
        ["plugins", "active_plugins"],
        include_secrets=False,
        stats=stats,
    )

    assert payload["plugins"] == {"foo": {"opt": 1}}
    assert payload["active_plugins"] == {"foo": "on", "bar": "off"}
    assert stats["warnings"] == ["plugin settings skipped (inactive: bar)"]
    assert client.calls_to("get_plugin_settings") == [("foo", "corso")]


def test_dump_agent_config_isolates_a_failing_plugin_settings_read():
    client = FakeClient(
        plugins={"corso": {"foo": True, "bar": True}},
        plugin_settings={("corso", "foo"): {"opt": 1}},
        plugin_settings_fail={"bar"},
    )
    stats = {"omitted": 0, "warnings": []}

    payload = cli._dump_agent_config(
        client, _agent("corso"), ["plugins"], include_secrets=False, stats=stats
    )

    assert payload["plugins"] == {"foo": {"opt": 1}}
    assert len(stats["warnings"]) == 1
    assert "plugin settings skipped (bar:" in stats["warnings"][0]


def test_dump_configs_records_the_system_metadata_warning(tmp_path):
    agents = [_agent("system")]
    client = FakeClient(agents=agents, factory_lists=_system_factory_lists())

    envelope = cli.dump_configs(client, agents, tmp_path, cli.DUMP_CATEGORIES, False, [], 1)

    entry = envelope["agents"][0]
    assert entry["ok"] is True
    assert "system metadata not exposed by the SDK" in entry["warnings"]
    header = (tmp_path / cli._dump_filename("system")).read_text(encoding="utf-8")
    assert "# warnings: system metadata not exposed by the SDK" in header
    manifest = yaml.safe_load((tmp_path / "manifest.yml").read_text(encoding="utf-8"))
    assert "system metadata not exposed by the SDK" in manifest["agents"][0]["warnings"]


def test_main_dump_all_succeeds_with_an_inactive_plugin(tmp_path, capsys):
    agents = [_agent("corso")]
    client = FakeClient(
        agents=agents,
        factory_lists=_full_factory_lists("corso"),
        plugins={"corso": {"foo": True, "bar": False}},
        plugin_settings={("corso", "foo"): {"opt": 1}},
        plugin_settings_500_inactive=True,
    )

    rc = cli.main(["--dump", str(tmp_path), "--all"], client_factory=lambda: client)

    assert rc == 0
    out = capsys.readouterr().out
    assert "agent=corso role=agent" in out
    assert "result=ok" in out
    path = tmp_path / cli._dump_filename("corso")
    payload = _dump_body(path)
    assert payload["plugins"] == {"foo": {"opt": 1}}
    assert payload["active_plugins"] == {"foo": "on", "bar": "off"}
    header = path.read_text(encoding="utf-8")
    assert "plugin settings skipped (inactive: bar)" in header


def test_dump_agent_config_system_tolerates_missing_per_agent_routes():
    client = FakeClient(
        factory_lists={
            ("embedder", None): _factory_list(
                "OpenAIEmbedder", {"OpenAIEmbedder": {"model": "e"}}
            ),
        },
        fail_on={
            "get_large_language_model_settings",
            "get_chunkers_settings",
            "get_vector_databases_settings",
            "get_auth_handlers_settings",
            "get_agentic_workflows_settings",
            "get_context_retrievers_settings",
            "get_file_managers_settings",
            "get_available_plugins",
        },
    )
    stats = {"omitted": 0, "warnings": []}

    payload = cli._dump_agent_config(
        client, _agent("system"), cli.DUMP_CATEGORIES, include_secrets=False, stats=stats
    )

    assert payload["metadata"] == {}
    assert payload["embedder"] == {"OpenAIEmbedder": {"model": "e"}}
    assert "ingestion" not in payload
    assert "system metadata not exposed by the SDK" in stats["warnings"]
    assert any("category skipped (llm:" in warning for warning in stats["warnings"])
    assert any("plugin settings skipped" in warning for warning in stats["warnings"])


def test_dump_manifest_only_tolerates_a_system_without_plugin_manager(tmp_path):
    agents = [_agent("system")]
    client = FakeClient(
        agents=agents,
        factory_lists=_system_factory_lists(),
        fail_on={("get_available_plugins", "system")},
    )

    rc = cli.main(
        ["--dump", str(tmp_path), "--all", "--dump-manifest-only"],
        client_factory=lambda: client,
    )

    assert rc == 0
    manifest = yaml.safe_load((tmp_path / "manifest.yml").read_text(encoding="utf-8"))
    entry = manifest["agents"][0]
    assert entry["plugins"] == 0
    assert any("plugin settings skipped" in warning for warning in entry["warnings"])


def test_select_agents_create_missing_only_on_zero_matches():
    agents = [_agent("corso1")]
    created = []

    selected = cli.select_agents(
        agents,
        ["corso", "nuovo"],
        include_all=False,
        exclude=[],
        create_missing=True,
        created_out=created,
    )

    assert [agent.agent_id for agent in selected] == ["corso1", "nuovo"]
    assert created == ["nuovo"]
    assert selected[1].metadata == {}


def test_select_agents_create_missing_never_creates_when_a_substring_matches():
    agents = [_agent("corso1")]
    created = []

    selected = cli.select_agents(
        agents,
        ["corso"],
        include_all=False,
        exclude=[],
        create_missing=True,
        created_out=created,
    )

    assert [agent.agent_id for agent in selected] == ["corso1"]
    assert created == []


def test_select_agents_without_create_missing_still_raises():
    with pytest.raises(cli.SelectionError):
        cli.select_agents([_agent("corso1")], ["nuovo"], include_all=False, exclude=[])


def test_all_with_create_missing_exits_2(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            ["--all", "--create-missing", "--metadata", "label=qa"],
            client_factory=_forbidden_factory,
        )

    assert excinfo.value.code == 2
    assert "--create-missing" in capsys.readouterr().err


def test_dump_with_create_missing_exits_2(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            ["--dump", str(tmp_path), "--all", "--create-missing"],
            client_factory=_forbidden_factory,
        )

    assert excinfo.value.code == 2
    assert "--create-missing" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra",
    [["--diff"], ["--dump-only", "llm"], ["--dump-secrets"], ["--dump-manifest-only"]],
)
def test_dump_modifiers_with_create_missing_exit_2(tmp_path, capsys, extra):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            ["--dump", str(tmp_path), "--all", "--create-missing", *extra],
            client_factory=_forbidden_factory,
        )

    assert excinfo.value.code == 2
    assert "--create-missing" in capsys.readouterr().err


def test_dry_run_plan_marks_a_missing_agent_for_creation(capsys):
    client = FakeClient(agents=[_agent("corso1")])

    rc = cli.main(
        ["--agent", "nuovo", "--create-missing", "--metadata", "label=qa"],
        client_factory=lambda: client,
    )

    assert rc == 2
    out = capsys.readouterr().out
    assert "agent=nuovo ops=create,metadata metadata_keys=label" in out
    assert client.calls_to("post_agent_create") == []


def test_dry_run_yaml_plan_marks_a_missing_agent_for_creation(capsys):
    client = FakeClient(agents=[_agent("corso1")])

    rc = cli.main(
        ["--agent", "nuovo", "--create-missing", "--metadata", "label=qa", "--yaml"],
        client_factory=lambda: client,
    )

    assert rc == 2
    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload["agents"] == [
        {"agent": "nuovo", "ops": ["create", "metadata"], "metadata_keys": ["label"]}
    ]


def test_yes_creates_then_applies_operations(tmp_path, capsys):
    client = FakeClient(agents=[_agent("corso1")])

    rc = cli.main(
        [
            "--agent",
            "nuovo",
            "--create-missing",
            "--metadata",
            "label=qa",
            "--yes",
            "--backup-out",
            str(tmp_path),
            "--yaml",
        ],
        client_factory=lambda: client,
    )

    assert rc == 0
    payload = yaml.safe_load(capsys.readouterr().out)
    entry = payload["agents"][0]
    assert entry["created"] is True
    assert entry["ok"] is True
    assert client.calls_to("post_agent_create") == [("nuovo", {})]
    assert client.calls_to("put_agent") == [("nuovo", {"label": "qa"})]
    names = [name for name, _args in client.calls]
    assert names.index("post_agent_create") < names.index("put_agent")
    assert _snapshot_files(tmp_path) == []


def test_human_report_marks_created_agents(tmp_path, capsys):
    client = FakeClient(agents=[_agent("corso1")])

    rc = cli.main(
        [
            "--agent",
            "nuovo",
            "--agent",
            "corso",
            "--create-missing",
            "--metadata",
            "label=qa",
            "--yes",
            "--backup-out",
            str(tmp_path),
        ],
        client_factory=lambda: client,
    )

    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    created = [line for line in lines if line.startswith("[batch] agent=nuovo ")]
    assert created[0] == "[batch] agent=nuovo created=yes"
    assert created[1].startswith("[batch] agent=nuovo op=metadata result=ok")
    assert not any("created=" in line for line in lines if line.startswith("[batch] agent=corso1 "))


def test_created_false_fails_only_that_agent(capsys):
    client = FakeClient(agents=[_agent("corso1")], create_false={"nuovo"})

    rc = cli.main(
        [
            "--agent",
            "nuovo",
            "--agent",
            "corso",
            "--create-missing",
            "--metadata",
            "label=qa",
            "--yes",
            "--yaml",
        ],
        client_factory=lambda: client,
    )

    assert rc == 1
    payload = yaml.safe_load(capsys.readouterr().out)
    by_id = {entry["agent_id"]: entry for entry in payload["agents"]}
    assert by_id["nuovo"]["ok"] is False
    assert by_id["nuovo"]["created"] is False
    assert "created=false" in by_id["nuovo"]["error"]
    assert by_id["corso1"]["ok"] is True
    assert client.calls_to("put_agent") == [("corso1", {"label": "qa"})]


def test_yes_does_not_create_when_the_substring_matches(tmp_path):
    client = FakeClient(agents=[_agent("corso1")])

    rc = cli.main(
        [
            "--agent",
            "corso",
            "--create-missing",
            "--metadata",
            "label=qa",
            "--yes",
            "--backup-out",
            str(tmp_path),
        ],
        client_factory=lambda: client,
    )

    assert rc == 0
    assert client.calls_to("post_agent_create") == []


def test_restore_create_missing_creates_then_restores(tmp_path, capsys):
    client = FakeClient(agents=[_agent("corso1", {"public": True})])
    first = cli.main(
        ["--agent", "corso", "--name", "X", "--yes", "--backup-out", str(tmp_path)],
        client_factory=lambda: client,
    )
    assert first == 0
    capsys.readouterr()

    gone = FakeClient(agents=[])
    rc = cli.main(
        ["--restore", str(tmp_path), "--create-missing", "--yes", "--yaml"],
        client_factory=lambda: gone,
    )

    assert rc == 0
    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload["agents"][0]["created"] is True
    assert gone.calls_to("post_agent_create") == [("corso1", {"public": True})]
    assert gone.calls_to("put_agent") == [("corso1", {"public": True})]


def test_restore_dry_run_marks_missing_agents_for_creation(tmp_path, capsys):
    client = FakeClient(agents=[_agent("corso1", {"public": True})])
    first = cli.main(
        ["--agent", "corso", "--name", "X", "--yes", "--backup-out", str(tmp_path)],
        client_factory=lambda: client,
    )
    assert first == 0
    capsys.readouterr()

    gone = FakeClient(agents=[])
    rc = cli.main(
        ["--restore", str(tmp_path), "--create-missing"],
        client_factory=lambda: gone,
    )

    assert rc == 2
    assert "agent=corso1 ops=create,restore" in capsys.readouterr().out
    assert gone.calls_to("post_agent_create") == []
