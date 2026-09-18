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


# --- what the UI looks like while management mode is on ---------------------

ME = {
    "username": "admin",
    "id": "user-1",
    "exp": 4102444800,
    "agents": [{"agent_name": "system", "user": {"id": "user-1", "permissions": {"SYSTEM": ["READ", "WRITE"]}}}],
}


@pytest.fixture
def sidebar(monkeypatch, session_state):
    """Render _render_sidebar_navigation() and collect the menu entries."""
    from app import main

    buttons = []
    warnings = []

    session_state.update({"token": "jwt-token", "agent_id": "system", "selected_page": "plugins"})

    monkeypatch.setattr(main, "has_access", lambda *args, **kwargs: True)
    monkeypatch.setattr(main, "is_system_agent_selected", lambda: True)
    monkeypatch.setattr(main, "_build_agents_toggle_select", lambda *args, **kwargs: None)
    monkeypatch.setattr(st, "markdown", lambda *a, **k: None)
    monkeypatch.setattr(st.sidebar, "markdown", lambda *a, **k: None)
    monkeypatch.setattr(st.sidebar, "warning", lambda msg, *a, **k: warnings.append(str(msg)))
    monkeypatch.setattr(st, "divider", lambda *a, **k: None)
    monkeypatch.setattr(st, "info", lambda *a, **k: None)
    monkeypatch.setattr(st, "button", lambda label, *a, **k: buttons.append(label) or False)
    return {"buttons": buttons, "warnings": warnings, "state": session_state, "main": main}


def test_sidebar_keeps_only_system_in_management_mode(sidebar):
    sidebar["state"]["management"] = {"management_active": True, "management_message": "Torniamo alle 18"}

    sidebar["main"]._render_sidebar_navigation(ME)

    nav_entries = [b for b in sidebar["buttons"] if b != "Logout"]
    assert nav_entries == ["⚙️ System"]
    # logging out must stay possible
    assert "Logout" in sidebar["buttons"]
    # a section that would only show 404s must not stay selected
    assert sidebar["state"]["selected_page"] == "system"


def test_sidebar_does_not_strand_a_session_without_an_agent(sidebar):
    """Right after login no agent is selected yet, and every System entry is
    gated on the system agent: forcing the page here would skip the welcome
    screen, the only place offering the agent selector, and leave the user with
    an empty sidebar and no way forward."""
    sidebar["state"]["management"] = {"management_active": True, "management_message": "Torniamo alle 18"}
    sidebar["state"].pop("agent_id")
    sidebar["state"]["selected_page"] = None
    # the real gate: no agent selected means no access to the System section
    sidebar["main"].has_access = lambda *args, **kwargs: not kwargs.get("only_admin")

    sidebar["main"]._render_sidebar_navigation(ME)

    assert sidebar["state"]["selected_page"] is None


def test_sidebar_keeps_every_section_out_of_management_mode(sidebar):
    sidebar["state"]["management"] = {"management_active": False}

    sidebar["main"]._render_sidebar_navigation(ME)

    nav_entries = [b for b in sidebar["buttons"] if b != "Logout"]
    assert "⚙️ System" in nav_entries
    assert "💬 Chat" in nav_entries
    assert sidebar["state"]["selected_page"] == "plugins"


def test_banner_shows_the_message_from_the_plugin(sidebar):
    sidebar["state"]["management"] = {"management_active": True, "management_message": "Torniamo alle 18"}

    sidebar["main"]._render_management_banner()

    assert len(sidebar["warnings"]) == 1
    assert "Torniamo alle 18" in sidebar["warnings"][0]


def test_banner_falls_back_to_the_global_message(sidebar):
    sidebar["state"]["management"] = {"management_active": True, "management_message": "", "global_message": "Avviso"}

    sidebar["main"]._render_management_banner()

    assert "Avviso" in sidebar["warnings"][0]


def test_no_banner_out_of_management_mode(sidebar):
    sidebar["state"]["management"] = {"management_active": False, "management_message": "Torniamo alle 18"}

    sidebar["main"]._render_management_banner()

    assert sidebar["warnings"] == []


def test_system_dropdown_offers_only_management_mode(monkeypatch, session_state, stub_streamlit_widgets):
    """With the mode on, the System page opens straight on the form that turns
    it off: no placeholder to pick, no entry that would only 404."""
    session_state["management"] = {"management_active": True}
    offered = {}

    monkeypatch.setattr(utilities, "has_access", lambda *args, **kwargs: True)
    monkeypatch.setattr(utilities, "_management_mode", lambda cookie_me: None)
    monkeypatch.setattr(st, "title", lambda *a, **k: None)

    def fake_selectbox(label, options, *args, **kwargs):
        offered["options"] = list(options)
        return list(options)[0]

    monkeypatch.setattr(st, "selectbox", fake_selectbox)

    utilities.utilities_management(ME)

    assert offered["options"] == ["Management mode"]


def test_system_dropdown_is_complete_out_of_management_mode(monkeypatch, session_state, stub_streamlit_widgets):
    session_state["management"] = {"management_active": False}
    offered = {}

    monkeypatch.setattr(utilities, "has_access", lambda *args, **kwargs: True)
    monkeypatch.setattr(utilities, "_list_agents", lambda cookie_me: None)
    monkeypatch.setattr(st, "title", lambda *a, **k: None)

    def fake_selectbox(label, options, *args, **kwargs):
        offered["options"] = list(options)
        return list(options)[0]

    monkeypatch.setattr(st, "selectbox", fake_selectbox)

    utilities.utilities_management(ME)

    assert "Management mode" in offered["options"]
    assert "Factory Reset" in offered["options"]
    assert offered["options"][0] == "(Select a menu)"


def test_sidebar_tells_how_to_reach_system_when_the_agent_cannot(sidebar, monkeypatch):
    """An agent other than the system one sees no entry at all: without a hint
    the sidebar is simply empty and there is nothing to act on."""
    infos = []
    monkeypatch.setattr(st.sidebar, "info", lambda msg, *a, **k: infos.append(str(msg)))

    sidebar["state"]["management"] = {"management_active": True}
    sidebar["state"]["agent_id"] = "another-agent"
    sidebar["main"].has_access = lambda *args, **kwargs: not kwargs.get("only_admin")

    sidebar["main"]._render_sidebar_navigation(ME)

    assert [b for b in sidebar["buttons"] if b != "Logout"] == []
    assert len(infos) == 1
    assert "system" in infos[0]


def test_welcome_offers_the_agent_selector_in_management_mode(monkeypatch, session_state):
    """The way out of the stranded session: the welcome screen still lets the
    user pick the system agent, and it needs no backend call to do it."""
    from app.routes import welcome as welcome_page

    offered = {}
    monkeypatch.setattr(st, "title", lambda *a, **k: None)
    monkeypatch.setattr(st, "markdown", lambda *a, **k: None)
    monkeypatch.setattr(st, "info", lambda *a, **k: None)

    def fake_selectbox(label, options, *args, **kwargs):
        offered["options"] = list(options)
        return "(Select an Agent)"

    monkeypatch.setattr(st, "selectbox", fake_selectbox)

    welcome_page.welcome(ME)

    assert "system" in offered["options"]
