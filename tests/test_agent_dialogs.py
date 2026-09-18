"""The agent dialogs must behave the same whichever way the UI authenticated."""
import pytest
import streamlit as st

from app.routes import utilities


ME = {"username": "admin", "id": "user-1", "exp": 4102444800, "agents": []}


@pytest.fixture
def created_agent(monkeypatch):
    """Drive the 'create agent' form up to a successful backend response."""
    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        @property
        def utils(self):
            return type(
                "_Utils", (), {"post_agent_create": staticmethod(lambda **_kw: type("_R", (), {"created": True})())}
            )

    monkeypatch.setattr(utilities, "GrinningCatClient", _Client)
    monkeypatch.setattr(utilities, "has_access", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        utilities, "show_overlay_spinner", lambda *args, **kwargs: type("_S", (), {"empty": staticmethod(lambda: None)})()
    )
    monkeypatch.setattr(utilities, "time", type("_T", (), {"sleep": staticmethod(lambda _s: None)}))
    monkeypatch.setattr(st, "text_input", lambda *args, **kwargs: "new-agent")
    monkeypatch.setattr(st, "text_area", lambda *args, **kwargs: "{}")


@pytest.mark.usefixtures("stub_streamlit_widgets", "created_agent")
def test_create_agent_refreshes_the_page_in_api_key_mode(session_state, no_rerun):
    """Without a 'me' to refresh there is still a stale agent list on screen."""
    with pytest.raises(no_rerun):
        utilities._create_agent(None)


@pytest.mark.usefixtures("stub_streamlit_widgets", "created_agent")
def test_create_agent_refreshes_the_user_agents_for_a_credentials_session(
    session_state, no_rerun, monkeypatch
):
    refreshed = []
    monkeypatch.setattr(utilities, "build_me_data", lambda: refreshed.append(True))

    with pytest.raises(no_rerun):
        utilities._create_agent(ME)

    assert refreshed == [True]
