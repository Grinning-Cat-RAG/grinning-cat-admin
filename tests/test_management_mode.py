"""The Management mode page reads through the mgmt_message plugin's own route.

That route is the only one the core keeps answering while management mode is
on, so this page is the way back out of it: if it cannot render its form, the
instance can only be reopened by editing Redis by hand.
"""
import pytest
import streamlit as st

from app.routes import utilities


# exactly what GET /mgmt_message/settings returns
MGMT_RESPONSE = {
    "name": "mgmt_message",
    "value": {
        "management_message": "Sistema in manutenzione",
        "management_active": True,
        "global_message": "",
        "show_global_msg": False,
    },
    "scheme": {
        "properties": {
            "management_message": {"default": "", "title": "Management Message", "type": "string"},
            "management_active": {"default": False, "title": "Management Active", "type": "boolean"},
            "global_message": {"default": "", "title": "Global Message", "type": "string"},
            "show_global_msg": {"default": False, "title": "Show Global Msg", "type": "boolean"},
        },
        "title": "PluginSettings",
        "type": "object",
    },
}


@pytest.fixture
def management_page(monkeypatch, session_state, stub_streamlit_widgets):
    """Drive _management_mode() up to the rendering of its form."""
    calls = {"get_custom": [], "errors": [], "rendered": None}

    class _Custom:
        @staticmethod
        def get_custom(url, agent_id, **kwargs):
            calls["get_custom"].append((url, agent_id))
            return MGMT_RESPONSE

    class _FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        @property
        def custom(self):
            return _Custom()

    monkeypatch.setattr(utilities, "GrinningCatClient", _FakeClient)
    monkeypatch.setattr(utilities, "has_access", lambda *args, **kwargs: True)
    monkeypatch.setattr(utilities, "run_toast", lambda: None)
    monkeypatch.setattr(st, "error", lambda msg, *a, **k: calls["errors"].append(str(msg)))
    monkeypatch.setattr(st, "text", lambda *a, **k: None)
    # isolate the read: do not walk into the save branch
    monkeypatch.setattr(st, "form_submit_button", lambda *a, **k: False)

    def fake_render_json_form(values, types, prefix=""):
        calls["rendered"] = (values, types)
        return values

    monkeypatch.setattr(utilities, "render_json_form", fake_render_json_form)
    return calls


def test_management_mode_renders_the_settings_it_reads(management_page):
    utilities._management_mode({"username": "admin", "agents": []})

    assert management_page["errors"] == []
    assert management_page["get_custom"] == [("/mgmt_message/settings", "system")]

    values, types = management_page["rendered"]
    assert values == MGMT_RESPONSE["value"]
    assert types["management_active"]["type"] == "boolean"
    assert types["management_message"]["type"] == "string"
