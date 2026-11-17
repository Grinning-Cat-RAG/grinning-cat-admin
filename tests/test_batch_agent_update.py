"""Tests for ``scripts/batch_agent_update.py``.

The CLI is always exercised through ``main(argv, client_factory=...)`` with a
fake client: no test opens a socket, touches Redis or needs a running Cat.
"""
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

from grinning_cat_python_sdk.models.api.admins import AgentOutput, AgentUpdatedOutput
from grinning_cat_python_sdk.models.api.factories import FactoryObjectSettingOutput
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
        "--exclude",
        "--llm",
        "--plugin",
        "--metadata",
        "--name",
        "--set",
        "--patch",
        "--yes",
        "--json",
        "--backup-out",
        "--restore",
        "--allow-system-wide",
        "--max-workers",
    ):
        assert flag in out


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
        cli.main(["--restore", "snap.json", "--llm", "gpt-4={}"], client_factory=_forbidden_factory)

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

    assert cli.main(["--agent", "x", "--yes"]) == 2

    assert "GRINNING_CAT_API_KEY" in capsys.readouterr().err


def test_empty_api_key_is_rejected(monkeypatch, capsys):
    monkeypatch.setenv("GRINNING_CAT_API_KEY", "")

    assert cli._configuration() is None

    assert "GRINNING_CAT_API_KEY" in capsys.readouterr().err


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
    ):
        self.calls = []
        self.agents = list(agents or [])
        self.settings = dict(settings or {})
        self.fail_on = set(fail_on or ())
        self.updated_false = set(updated_false or ())
        self.plugin_states = {agent_id: dict(items) for agent_id, items in (plugins or {}).items()}
        self.plugin_settings = {
            (agent_id, plugin_id): dict(values)
            for (agent_id, plugin_id), values in (plugin_settings or {}).items()
        }
        self.toggle_noop = toggle_noop
        self.concurrency_guard = concurrency_guard
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
        if method_name == "get_agents":
            return list(self.agents)
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
            return PluginToggleOutput(info="toggled")
        if method_name == "get_plugin_settings":
            plugin_id, agent_id = args
            return FactoryObjectSettingOutput(
                name=plugin_id, value=dict(self.plugin_settings.get((agent_id, plugin_id), {}))
            )
        if method_name == "put_plugin_settings":
            plugin_id, agent_id, values = args
            key = (agent_id, plugin_id)
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
    assert "agent=Corso1 ops=llm metadata_keys=" in out
    assert client.calls_to("put_large_language_model_settings") == []
    assert client.calls_to("put_agent") == []


def test_dry_run_unknown_substring_exits_2(capsys):
    client = FakeClient(agents=[_agent("corso1")])

    rc = cli.main(["--agent", "zzz-not-present", "--llm", "gpt-4={}"], client_factory=lambda: client)

    assert rc == 2
    assert "zzz-not-present" in capsys.readouterr().err


def test_dry_run_json_plan(capsys):
    client = FakeClient(agents=[_agent("Corso1"), _agent("other")])

    rc = cli.main(["--agent", "corso1", "--llm", "gpt-4={}", "--json"], client_factory=lambda: client)

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == [{"agent": "Corso1", "ops": ["llm"], "metadata_keys": []}]


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
    patch_file = tmp_path / "patch.json"
    patch_file.write_text(
        json.dumps(
            {
                "llm": {"gpt-4": {"temperature": 0.2}},
                "plugins": {"foo": {"k": "v"}},
                "metadata": {"name": "X"},
            }
        ),
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


def test_patch_and_set_are_coalesced(tmp_path):
    patch_file = tmp_path / "patch.json"
    patch_file.write_text(
        json.dumps({"llm": {"gpt-4": {"temperature": 0.2, "max_tokens": 10}}}),
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
    patch_file = tmp_path / "patch.json"
    patch_file.write_text(json.dumps({"llm": {"gpt-4": 0.2}}), encoding="utf-8")
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


def test_yes_happy_path_json_report(capsys):
    client = FakeClient(agents=[_agent("corso1", {"public": True})])

    rc = cli.main(
        ["--agent", "corso", "--metadata", "label=b", "--yes", "--json"],
        client_factory=lambda: client,
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
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
            "--json",
        ],
        client_factory=lambda: client,
    )

    assert rc == 0
    assert client.calls_to("put_embedder_settings") == [
        ("openai", {"model": "text-embedding-3-small", "key": "x"})
    ]
    payload = json.loads(capsys.readouterr().out)
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
            "--json",
        ],
        client_factory=lambda: client,
    )

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
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


def test_dry_run_json_stays_an_array_with_system_wide(capsys):
    client = FakeClient(agents=[_agent("corso1")])

    rc = cli.main(
        ["--agent", "corso", "--set", "embedder:openai:key=x", "--allow-system-wide", "--json"],
        client_factory=lambda: client,
    )

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload == [{"agent": "corso1", "ops": ["set"], "metadata_keys": []}]


def test_backup_out_writes_one_snapshot_per_agent_and_a_manifest(tmp_path):
    client = FakeClient(agents=[_agent("corso1", {"public": True}), _agent("corso2")])

    rc = cli.main(
        ["--agent", "corso", "--metadata", "label=b", "--yes", "--backup-out", str(tmp_path)],
        client_factory=lambda: client,
    )

    assert rc == 0
    assert sorted(path.name for path in tmp_path.glob("*.json")) == [
        "corso1.json",
        "corso2.json",
        "manifest.json",
    ]
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["agents"] == ["corso1", "corso2"]
    assert manifest["argv"]
    assert manifest["created_at"]
    snapshot = json.loads((tmp_path / "corso1.json").read_text(encoding="utf-8"))
    assert snapshot["metadata"] == {"public": True}


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
        ["--agent", "corso", "--metadata", "label=b", "--yes", "--json", "--max-workers", "1"],
        client_factory=lambda: sequential,
    )
    first = json.loads(capsys.readouterr().out)
    rc4 = cli.main(
        ["--agent", "corso", "--metadata", "label=b", "--yes", "--json", "--max-workers", "4"],
        client_factory=lambda: parallel,
    )
    second = json.loads(capsys.readouterr().out)

    assert (rc1, rc4) == (0, 0)
    assert first == second


def test_mixed_run_summary_counts_skipped_and_failed(capsys):
    client = FakeClient(
        agents=[_agent("corso1"), _agent("corso2"), _agent("corso3")],
        plugins={"corso1": {"foo": True}, "corso2": {"foo": False}, "corso3": {}},
    )

    rc = cli.main(["--agent", "corso", "--plugin", "foo=on", "--yes", "--json"], client_factory=lambda: client)

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
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
            "--json",
        ],
        client_factory=lambda: client,
    )

    assert rc == 0
    assert client.calls_to("put_ingestion_settings") == [("default", {"engine": "core", "batch_size": 8})]
    payload = json.loads(capsys.readouterr().out)
    assert payload["system_wide"] == [{"op": "system:ingestion:default", "status": "ok", "detail": "merged"}]
