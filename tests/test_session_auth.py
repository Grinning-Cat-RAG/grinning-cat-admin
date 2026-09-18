"""Regression tests for how the admin UI decides who it is talking to.

Every defect covered here ends the same way: a credentials session silently
rendered as the API-key one ("You are logged in with the default API key."),
which also hands the full admin UI to a user the backend would refuse.
"""
import asyncio

import pytest
import streamlit as st
from requests.exceptions import HTTPError

from app import main
from app import utils


ME = {
    "username": "admin",
    "id": "user-1",
    "exp": 4102444800,
    "agents": [
        {"agent_name": "agent-a", "user": {"id": "user-1", "permissions": {"CHAT": ["READ"]}}},
        {"agent_name": "agent-b", "user": {"id": "user-2", "permissions": {"CHAT": ["READ"]}}},
    ],
}


def _http_error(status: int) -> HTTPError:
    class _Response:
        status_code = status

    return HTTPError("rejected", response=_Response())


# --- 'me' rebuilt from the API ------------------------------------------------

def test_get_cookie_me_rebuilds_from_the_api_after_a_page_refresh(session_state, monkeypatch):
    """Only the token is persisted, so an empty session_state must be refilled
    from /auth/me instead of falling through to API-key mode."""
    session_state["token"] = "jwt-token"

    monkeypatch.setattr(main, "build_me_data", lambda: dict(st.session_state.setdefault("me", ME)))

    assert main._get_cookie_me() == ME


def test_get_cookie_me_returns_none_without_token(session_state, monkeypatch):
    """No token at all is genuine API-key mode: no API call, no 'me'."""
    def unexpected():
        raise AssertionError("must not call the API without a token")

    monkeypatch.setattr(main, "build_me_data", unexpected)

    assert main._get_cookie_me() is None


@pytest.mark.parametrize("status", [401, 403])
def test_get_cookie_me_logs_out_when_the_backend_rejects_the_token(
    session_state, no_rerun, monkeypatch, status
):
    """An expired or revoked token must send the user back to the login page,
    not grant the API-key branch."""
    session_state.update({"token": "stale-token", "selected_page": "users"})

    def rejected():
        raise _http_error(status)

    cleared = []
    monkeypatch.setattr(main, "build_me_data", rejected)
    monkeypatch.setattr(main, "clear_auth_cookies", lambda: cleared.append(True))
    monkeypatch.setattr(main, "time", type("_T", (), {"sleep": staticmethod(lambda _s: None)}))
    monkeypatch.setattr(st, "toast", lambda *args, **kwargs: None)

    with pytest.raises(no_rerun):
        main._get_cookie_me()

    assert cleared == [True]
    assert "token" not in session_state
    assert "selected_page" not in session_state
    assert session_state["auth_error"] == "Your session has expired. Please log in again."
    assert session_state["rejected_token"] == "stale-token"


def test_get_cookie_me_keeps_the_session_on_a_transient_backend_error(
    session_state, monkeypatch
):
    """A 500 or a connection drop is not an authentication verdict: the token
    stays so the next rerun can retry."""
    session_state["token"] = "jwt-token"

    def unavailable():
        raise _http_error(503)

    monkeypatch.setattr(main, "build_me_data", unavailable)

    assert main._get_cookie_me() is None
    assert session_state["token"] == "jwt-token"


def test_rehydration_does_not_persist_the_user_to_localstorage(session_state, monkeypatch):
    """The permissions the UI enforces always come from the backend: nothing
    about the user is mirrored into localStorage, where it would go stale."""
    session_state["token"] = "jwt-token"

    class _FakeMe:
        @staticmethod
        def model_dump():
            return {"sub": "admin", "exp": 4102444800, "agents": ME["agents"]}

    class _FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        @property
        def auth(self):
            return type("_Auth", (), {"me": staticmethod(lambda _token: _FakeMe())})

    monkeypatch.setattr(utils, "GrinningCatClient", _FakeClient)
    monkeypatch.setattr(
        utils, "set_local_storage", lambda *args, **kwargs: pytest.fail("must not persist 'me'")
    )

    me_data = main._get_cookie_me()

    assert me_data["username"] == "admin"
    assert me_data["agents"] == ME["agents"]
    assert session_state["me"] == me_data


# --- agent switching ----------------------------------------------------------

def test_agent_toggle_keeps_the_credentials_session(session_state, no_rerun, monkeypatch):
    """Switching agent resets the agent-scoped state but must not log the user
    out by wiping token/me/_session_key."""
    session_state.update({
        "token": "jwt-token",
        "me": ME,
        "_session_key": "sess",
        "status_connection": "Online",
        "initial_auth_check_done": True,
        "agent_id": "agent-a",
        "selected_page": "chat",
        "user_id": "user-1",
        "conversation_id": "conv-1",
        "messages": ["hello"],
    })

    monkeypatch.setattr(
        main, "build_agents_options_select", lambda cookie_me, excluded_agents=None: {"agent-b": "agent-b"}
    )
    monkeypatch.setattr(st, "selectbox", lambda *args, **kwargs: "agent-b")
    monkeypatch.setattr(st, "divider", lambda *args, **kwargs: None)

    with pytest.raises(no_rerun):
        main._build_agents_toggle_select("sidebar_nav", ME)

    assert session_state["token"] == "jwt-token"
    assert session_state["me"] == ME
    assert session_state["_session_key"] == "sess"
    assert session_state["agent_id"] == "agent-b"

    for dropped in ("user_id", "conversation_id", "messages", "selected_page"):
        assert dropped not in session_state


def test_agent_toggle_leaves_state_untouched_when_nothing_is_picked(session_state, monkeypatch):
    session_state.update({"token": "jwt-token", "me": ME, "agent_id": "agent-a", "user_id": "user-1"})

    monkeypatch.setattr(
        main, "build_agents_options_select", lambda cookie_me, excluded_agents=None: {"agent-b": "agent-b"}
    )
    monkeypatch.setattr(st, "selectbox", lambda *args, **kwargs: "(Select an Agent)")
    monkeypatch.setattr(st, "divider", lambda *args, **kwargs: None)

    main._build_agents_toggle_select("sidebar_nav", ME)

    assert session_state["agent_id"] == "agent-a"
    assert session_state["user_id"] == "user-1"


# --- API-key mode -------------------------------------------------------------

def test_client_configuration_prefers_the_token_over_the_api_key(session_state, monkeypatch):
    monkeypatch.setenv("GRINNING_CAT_API_KEY", "an-api-key")
    session_state["token"] = "jwt-token"

    assert utils.build_client_configuration().auth_key == "jwt-token"


def test_client_configuration_falls_back_to_the_api_key(session_state, monkeypatch):
    monkeypatch.setenv("GRINNING_CAT_API_KEY", "an-api-key")

    assert utils.build_client_configuration().auth_key == "an-api-key"
    assert utils.is_api_key_mode() is True


def test_client_configuration_without_credentials(session_state, monkeypatch):
    monkeypatch.delenv("GRINNING_CAT_API_KEY", raising=False)

    assert utils.build_client_configuration().auth_key is None
    assert utils.is_api_key_mode() is False


def test_api_key_mode_renders_the_app_instead_of_the_login_page(session_state, monkeypatch):
    """With an API key configured and no stored token the SDK can authenticate
    on its own, so the UI must not dead-end on the login form."""
    monkeypatch.setenv("GRINNING_CAT_API_KEY", "an-api-key")
    session_state.update({"_session_key": "sess", "initial_auth_check_done": True})

    rendered = {}
    monkeypatch.setattr(main, "_apply_custom_css", lambda: None)
    monkeypatch.setattr(
        main, "_check_status", lambda: session_state.__setitem__("status_connection", "Online")
    )
    monkeypatch.setattr(main, "get_with_expiry", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "login_page", lambda: pytest.fail("must not ask for credentials"))
    monkeypatch.setattr(main, "_render_sidebar_navigation", lambda me: rendered.__setitem__("sidebar", me))

    async def fake_render_page(me):
        rendered["page"] = me

    monkeypatch.setattr(main, "_render_page", fake_render_page)

    asyncio.run(main._main())

    assert rendered == {"sidebar": None, "page": None}


def test_login_page_is_shown_without_any_credentials(session_state, monkeypatch):
    monkeypatch.delenv("GRINNING_CAT_API_KEY", raising=False)
    session_state.update({"_session_key": "sess", "initial_auth_check_done": True})

    shown = []
    monkeypatch.setattr(main, "_apply_custom_css", lambda: None)
    monkeypatch.setattr(
        main, "_check_status", lambda: session_state.__setitem__("status_connection", "Online")
    )
    monkeypatch.setattr(main, "get_with_expiry", lambda *args, **kwargs: None)
    monkeypatch.setattr(st, "title", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "login_page", lambda: shown.append(True))
    monkeypatch.setattr(
        main, "_render_page", lambda me: pytest.fail("must not render the app unauthenticated")
    )

    asyncio.run(main._main())

    assert shown == [True]


def test_a_rejected_token_is_not_read_back_from_localstorage(session_state, monkeypatch):
    """Dropping the localStorage entry is asynchronous, so the refused token can
    still be there on the next render: adopting it again would spin the
    refuse/logout cycle forever."""
    monkeypatch.delenv("GRINNING_CAT_API_KEY", raising=False)
    session_state.update({
        "_session_key": "sess",
        "initial_auth_check_done": True,
        "rejected_token": "stale-token",
    })

    shown = []
    monkeypatch.setattr(main, "_apply_custom_css", lambda: None)
    monkeypatch.setattr(
        main, "_check_status", lambda: session_state.__setitem__("status_connection", "Online")
    )
    monkeypatch.setattr(main, "get_with_expiry", lambda *args, **kwargs: "stale-token")
    monkeypatch.setattr(st, "title", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "login_page", lambda: shown.append(True))

    asyncio.run(main._main())

    assert shown == [True]
    assert "token" not in session_state


def test_sidebar_is_rendered_in_api_key_mode(session_state, monkeypatch):
    """_render_sidebar_navigation() bails out when nobody is logged in; the API
    key counts as logged in."""
    monkeypatch.setenv("GRINNING_CAT_API_KEY", "an-api-key")
    session_state["selected_page"] = "users"
    monkeypatch.setattr(main, "has_access", lambda *args, **kwargs: False)
    monkeypatch.setattr(st, "markdown", lambda *args, **kwargs: None)
    monkeypatch.setattr(st.sidebar, "markdown", lambda *args, **kwargs: None)
    monkeypatch.setattr(st, "divider", lambda *args, **kwargs: None)
    monkeypatch.setattr(st, "info", lambda *args, **kwargs: None)

    main._render_sidebar_navigation(None)

    assert session_state["selected_page"] == "users"
