"""Tests for ``scripts/batch_agent_update.py``.

The CLI is always exercised through ``main(argv, client_factory=...)`` with a
fake client: no test opens a socket, touches Redis or needs a running Cat.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from grinning_cat_python_sdk.models.api.admins import AgentOutput, AgentUpdatedOutput
from grinning_cat_python_sdk.models.api.factories import FactoryObjectSettingOutput

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "batch_agent_update.py"
_spec = importlib.util.spec_from_file_location("batch_agent_update", _SCRIPT)
assert _spec is not None and _spec.loader is not None
cli = importlib.util.module_from_spec(_spec)
sys.modules["batch_agent_update"] = cli
_spec.loader.exec_module(cli)


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


class FakeClient:
    """In-memory stand-in for ``GrinningCatClient``: no network, no Redis."""

    def __init__(self, agents=None, settings=None, fail_on=None, updated_false=None):
        self.calls = []
        self.agents = list(agents or [])
        self.settings = dict(settings or {})
        self.fail_on = set(fail_on or ())
        self.updated_false = set(updated_false or ())
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
        ):
            setattr(self, attribute, FakeEndpoint(self, attribute))

    def _handle(self, attribute, method_name, args, kwargs):
        self.calls.append((method_name, args))
        if method_name in self.fail_on:
            raise RuntimeError(f"{method_name} failed")
        if method_name == "get_agents":
            return list(self.agents)
        if method_name == "put_agent":
            agent_id, metadata = args
            self.settings[("metadata", agent_id)] = dict(metadata)
            return AgentUpdatedOutput(updated=agent_id not in self.updated_false)
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
