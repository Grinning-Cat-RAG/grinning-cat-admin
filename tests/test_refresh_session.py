"""Login, logout and automatic refresh against the refresh-token contract of the backend.

/auth/token returns a refresh token next to the access token; the refresh token is
single-use (every /auth/refresh rotates it) and /auth/logout is the only way to end
the session server-side: dropping the local state alone leaves it usable until it expires.
"""
import asyncio
import base64
import json
import time

import pytest
import streamlit as st
from grinning_cat_python_sdk.models.api.tokens import TokenOutput
from requests.exceptions import HTTPError

from app import main, utils
from app.routes import login


def _http_error(status: int, headers: dict | None = None) -> HTTPError:
    class _Response:
        pass

    _Response.status_code = status
    _Response.headers = headers or {}
    return HTTPError("rejected", response=_Response())


def _jwt(exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class _FakeAuth:
    def __init__(self, token=None, refreshed=None, token_error=None, refresh_error=None, logout_error=None):
        self.token_output = token
        self.refreshed = refreshed
        self.token_error = token_error
        self.refresh_error = refresh_error
        self.logout_error = logout_error
        self.refresh_calls = []
        self.logged_out = []

    def token(self, username, password):
        if self.token_error:
            raise self.token_error
        return self.token_output

    def refresh(self, refresh_token):
        self.refresh_calls.append(refresh_token)
        if self.refresh_error:
            raise self.refresh_error
        return self.refreshed

    def logout(self, refresh_token):
        self.logged_out.append(refresh_token)
        if self.logout_error:
            raise self.logout_error


def _fake_client(auth):
    class _Client:
        def __init__(self, *_args, **_kwargs):
            self.auth = auth

    return _Client


class _NoSleep:
    sleep = staticmethod(lambda _s: None)


@pytest.fixture
def login_form(monkeypatch, stub_streamlit_widgets):
    monkeypatch.setattr(st, "text_input", lambda label, **kwargs: "admin" if label == "Username" else "secret")
    monkeypatch.setattr(st, "sidebar", type("_Sidebar", (), {"warning": staticmethod(lambda *a, **k: None)}))
    monkeypatch.setattr(login, "show_overlay_spinner", lambda *args, **kwargs: st.empty())
    monkeypatch.setattr(login, "time", _NoSleep)


@pytest.fixture
def persisted(monkeypatch):
    """Records what is written to localStorage by set_with_expiry (key -> (value, expires_in))."""
    written = {}
    monkeypatch.setattr(
        utils,
        "set_with_expiry",
        lambda key, value, token, expires_in=None: written.__setitem__(key, (value, expires_in)),
    )
    monkeypatch.setattr(
        login,
        "set_with_expiry",
        lambda key, value, token, expires_in=None: written.__setitem__(key, (value, expires_in)),
    )
    return written


# --- login --------------------------------------------------------------------

def test_login_persists_the_refresh_token(session_state, no_rerun, login_form, persisted, monkeypatch):
    """Without it, neither logout nor the automatic refresh have a token to work with."""
    auth = _FakeAuth(token=TokenOutput(access_token="jwt", refresh_token="refresh-1", refresh_expires_in=600))
    monkeypatch.setattr(login, "GrinningCatClient", _fake_client(auth))
    monkeypatch.setattr(login, "build_me_data", lambda: {})

    with pytest.raises(no_rerun):
        login.login_page()

    assert session_state["token"] == "jwt"
    assert session_state["refresh_token"] == "refresh-1"
    assert persisted["token"][0] == "jwt"
    assert persisted["refresh_token"] == ("refresh-1", 600)


def test_login_without_a_refresh_token_still_works(session_state, no_rerun, login_form, persisted, monkeypatch):
    """Older backends do not issue one."""
    monkeypatch.setattr(login, "GrinningCatClient", _fake_client(_FakeAuth(token=TokenOutput(access_token="jwt"))))
    monkeypatch.setattr(login, "build_me_data", lambda: {})

    with pytest.raises(no_rerun):
        login.login_page()

    assert session_state["token"] == "jwt"
    assert "refresh_token" not in session_state
    assert list(persisted) == ["token"]


@pytest.mark.parametrize(
    "error, expected",
    [
        (_http_error(401), "Invalid username or password"),
        (_http_error(429, {"Retry-After": "120"}), "Too many attempts, try again in 120 seconds"),
        (_http_error(429), "Too many attempts, try again later"),
    ],
)
def test_login_reports_a_readable_error(session_state, login_form, monkeypatch, error, expected):
    shown = []
    monkeypatch.setattr(login, "GrinningCatClient", _fake_client(_FakeAuth(token_error=error)))
    monkeypatch.setattr(login, "clear_auth_cookies", lambda: None)
    monkeypatch.setattr(st, "error", lambda message, *args, **kwargs: shown.append(message))

    login.login_page()

    assert len(shown) == 1 and expected in shown[0]
    assert "token" not in session_state


# --- logout -------------------------------------------------------------------

def _stub_logout_side_effects(monkeypatch, cleared):
    monkeypatch.setattr(main, "clear_auth_cookies", lambda: cleared.append(True))
    monkeypatch.setattr(main, "time", _NoSleep)
    monkeypatch.setattr(st, "toast", lambda *args, **kwargs: None)


def test_logout_revokes_the_session_on_the_backend(session_state, no_rerun, monkeypatch):
    session_state.update({"token": "jwt", "refresh_token": "refresh-1", "me": {}})
    auth = _FakeAuth()
    cleared = []
    monkeypatch.setattr(utils, "GrinningCatClient", _fake_client(auth))
    _stub_logout_side_effects(monkeypatch, cleared)

    with pytest.raises(no_rerun):
        main._logout("Logged out successfully.", "🚪")

    assert auth.logged_out == ["refresh-1"]
    assert cleared == [True]
    assert "token" not in session_state and "refresh_token" not in session_state


def test_logout_completes_even_if_the_backend_is_unreachable(session_state, no_rerun, monkeypatch):
    """A failed revocation must never trap the user in a session they want to leave."""
    session_state.update({"token": "jwt", "refresh_token": "refresh-1"})
    auth = _FakeAuth(logout_error=_http_error(503))
    cleared = []
    monkeypatch.setattr(utils, "GrinningCatClient", _fake_client(auth))
    _stub_logout_side_effects(monkeypatch, cleared)

    with pytest.raises(no_rerun):
        main._logout("Logged out successfully.", "🚪")

    assert auth.logged_out == ["refresh-1"]
    assert cleared == [True]
    assert "token" not in session_state


def test_logout_without_a_refresh_token_does_not_call_the_backend(session_state, no_rerun, monkeypatch):
    session_state["token"] = "jwt"
    cleared = []
    monkeypatch.setattr(utils, "GrinningCatClient", lambda *a, **k: pytest.fail("nothing to revoke"))
    _stub_logout_side_effects(monkeypatch, cleared)

    with pytest.raises(no_rerun):
        main._logout("Logged out successfully.", "🚪")

    assert cleared == [True]


def test_a_forced_logout_remembers_the_refused_refresh_token(session_state, no_rerun, monkeypatch):
    """Removing the localStorage entries is asynchronous, like for the access token."""
    session_state.update({"token": "stale", "refresh_token": "refresh-1"})
    monkeypatch.setattr(utils, "GrinningCatClient", _fake_client(_FakeAuth()))
    _stub_logout_side_effects(monkeypatch, [])

    with pytest.raises(no_rerun):
        main._logout("expired", "⏱️", keep_message=True)

    assert session_state["rejected_token"] == "stale"
    assert session_state["rejected_refresh_token"] == "refresh-1"


def test_clear_auth_cookies_drops_the_refresh_token(monkeypatch):
    removed = []
    monkeypatch.setattr(utils, "remove_local_storage", lambda key, *a, **k: removed.append(key))

    utils.clear_auth_cookies()

    assert "refresh_token" in removed and "token" in removed


# --- automatic refresh --------------------------------------------------------

def test_refresh_session_swaps_and_persists_both_tokens(session_state, persisted, monkeypatch):
    """The refresh token is single-use: the rotated one must replace the old one everywhere."""
    session_state.update({"token": "old-jwt", "refresh_token": "refresh-1"})
    auth = _FakeAuth(refreshed=TokenOutput(access_token="new-jwt", refresh_token="refresh-2", refresh_expires_in=900))
    monkeypatch.setattr(utils, "GrinningCatClient", _fake_client(auth))

    assert utils.refresh_session() == utils.REFRESH_OK

    assert auth.refresh_calls == ["refresh-1"]
    assert session_state["token"] == "new-jwt"
    assert session_state["refresh_token"] == "refresh-2"
    assert persisted["token"][0] == "new-jwt"
    assert persisted["refresh_token"] == ("refresh-2", 900)


def test_refresh_session_reports_a_refused_refresh_token(session_state, monkeypatch):
    session_state["refresh_token"] = "refresh-1"
    monkeypatch.setattr(utils, "GrinningCatClient", _fake_client(_FakeAuth(refresh_error=_http_error(401))))

    assert utils.refresh_session() == utils.REFRESH_REJECTED


@pytest.mark.parametrize("error", [_http_error(503), _http_error(429), ConnectionError("down")])
def test_refresh_session_keeps_the_session_on_a_transient_error(session_state, monkeypatch, error):
    session_state.update({"token": "old-jwt", "refresh_token": "refresh-1"})
    monkeypatch.setattr(utils, "GrinningCatClient", _fake_client(_FakeAuth(refresh_error=error)))

    assert utils.refresh_session() == utils.REFRESH_ERROR
    assert session_state["token"] == "old-jwt" and session_state["refresh_token"] == "refresh-1"


def test_refresh_session_without_a_refresh_token(session_state, monkeypatch):
    monkeypatch.setattr(utils, "GrinningCatClient", lambda *a, **k: pytest.fail("nothing to refresh"))

    assert utils.refresh_session() == utils.REFRESH_REJECTED


def test_access_token_expiry_detection():
    now = int(time.time())
    assert utils.access_token_expires_soon(_jwt(now + 10)) is True
    assert utils.access_token_expires_soon(_jwt(now - 10)) is True
    assert utils.access_token_expires_soon(_jwt(now + 3600)) is False
    # API keys and other opaque strings have no expiry to track
    assert utils.access_token_expires_soon("an-api-key") is False
    assert utils.access_token_expires_soon(None) is False


def test_the_token_is_refreshed_before_it_expires(session_state, monkeypatch):
    session_state.update({"token": _jwt(int(time.time()) + 5), "refresh_token": "refresh-1"})
    calls = []
    monkeypatch.setattr(main, "refresh_session_synced", lambda: calls.append(True) or utils.REFRESH_OK)

    assert main._ensure_fresh_token() is True
    assert calls == [True]


def test_a_valid_token_is_not_refreshed(session_state, monkeypatch):
    session_state.update({"token": _jwt(int(time.time()) + 3600), "refresh_token": "refresh-1"})
    monkeypatch.setattr(main, "refresh_session_synced", lambda: pytest.fail("token still valid"))

    assert main._ensure_fresh_token() is True


def test_no_refresh_is_attempted_without_a_refresh_token(session_state, monkeypatch):
    session_state["token"] = _jwt(int(time.time()) - 5)
    monkeypatch.setattr(main, "refresh_session_synced", lambda: pytest.fail("nothing to refresh"))

    assert main._ensure_fresh_token() is True


def test_a_refused_refresh_sends_the_user_back_to_login(session_state, no_rerun, monkeypatch):
    session_state.update({"token": _jwt(int(time.time()) - 5), "refresh_token": "refresh-1"})
    monkeypatch.setattr(main, "refresh_session_synced", lambda: utils.REFRESH_REJECTED)
    monkeypatch.setattr(utils, "GrinningCatClient", _fake_client(_FakeAuth()))
    _stub_logout_side_effects(monkeypatch, [])

    with pytest.raises(no_rerun):
        main._ensure_fresh_token()

    assert session_state["auth_error"] == "Your session has expired. Please log in again."
    assert "token" not in session_state


def test_a_transient_refresh_error_keeps_the_session(session_state, monkeypatch):
    session_state.update({"token": _jwt(int(time.time()) - 5), "refresh_token": "refresh-1"})
    monkeypatch.setattr(main, "refresh_session_synced", lambda: utils.REFRESH_ERROR)

    assert main._ensure_fresh_token() is True
    assert "token" in session_state


def test_the_run_waits_while_the_storage_of_the_other_tabs_is_read(session_state, monkeypatch):
    session_state.update({"token": _jwt(int(time.time()) - 5), "refresh_token": "refresh-1"})
    monkeypatch.setattr(main, "refresh_session_synced", lambda: utils.REFRESH_PENDING)

    assert main._ensure_fresh_token() is False


def test_the_wait_continues_on_the_next_rerun_even_if_the_token_looks_valid(session_state, monkeypatch):
    """The sync is a two-render cycle: it must be completed, not dropped."""
    session_state.update({
        "token": _jwt(int(time.time()) + 3600), "refresh_token": "refresh-1", "_sync_nonce": "n1",
    })
    calls = []
    monkeypatch.setattr(main, "refresh_session_synced", lambda: calls.append(True) or utils.REFRESH_OK)

    assert main._ensure_fresh_token() is True
    assert calls == [True]


def test_a_token_refused_by_the_backend_forces_a_synced_refresh(session_state, no_rerun, monkeypatch):
    """The token can be refused before its exp claim (clock skew, backend restarted with another secret)."""
    session_state.update({"token": "stale", "refresh_token": "refresh-1"})
    monkeypatch.setattr(main, "build_me_data", lambda: (_ for _ in ()).throw(_http_error(401)))

    with pytest.raises(no_rerun):
        main._get_cookie_me()

    assert session_state["_force_refresh"] is True
    assert session_state["token"] == "stale"  # still logged in: the refresh happens on the next run


def test_the_forced_refresh_runs_even_if_the_token_looks_valid(session_state, monkeypatch):
    session_state.update({
        "token": _jwt(int(time.time()) + 3600), "refresh_token": "refresh-1", "_force_refresh": True,
    })
    calls = []
    monkeypatch.setattr(main, "refresh_session_synced", lambda: calls.append(True) or utils.REFRESH_OK)

    assert main._ensure_fresh_token() is True
    assert calls == [True]
    assert "_force_refresh" not in session_state


def test_a_token_refused_again_after_the_refresh_logs_out(session_state, no_rerun, monkeypatch):
    """No refresh/refuse loop: one renewal per refusal."""
    session_state.update({"token": "fresh", "refresh_token": "refresh-2", "_renewed_after_refusal": True})
    monkeypatch.setattr(main, "build_me_data", lambda: (_ for _ in ()).throw(_http_error(401)))
    monkeypatch.setattr(utils, "GrinningCatClient", _fake_client(_FakeAuth()))
    _stub_logout_side_effects(monkeypatch, [])

    with pytest.raises(no_rerun):
        main._get_cookie_me()

    assert session_state["auth_error"] == "Your session has expired. Please log in again."


def test_a_refused_token_without_a_refresh_token_logs_out(session_state, no_rerun, monkeypatch):
    session_state["token"] = "stale"
    monkeypatch.setattr(main, "build_me_data", lambda: (_ for _ in ()).throw(_http_error(401)))
    _stub_logout_side_effects(monkeypatch, [])

    with pytest.raises(no_rerun):
        main._get_cookie_me()

    assert session_state["auth_error"] == "Your session has expired. Please log in again."


def test_a_successful_me_rearms_the_renewal(session_state, monkeypatch):
    session_state.update({"token": "fresh", "refresh_token": "refresh-2", "_renewed_after_refusal": True})
    monkeypatch.setattr(main, "build_me_data", lambda: {"username": "admin"})

    assert main._get_cookie_me() == {"username": "admin"}
    assert "_renewed_after_refusal" not in session_state


# --- several tabs of the same browser -----------------------------------------
# localStorage is shared, session_state is not: when a tab rotates the single-use
# refresh token, the copy held by the other tabs is stale and presenting it would
# be read by the backend as theft, closing the session for everyone.

@pytest.fixture
def storage(monkeypatch):
    """A fake localStorage read through get_with_expiry(); remembers the component keys used."""
    content, component_keys = {}, []

    def read(key, component_key=None):
        component_keys.append(component_key)
        return content.get(key)

    monkeypatch.setattr(utils, "get_with_expiry", read)
    read.content, read.component_keys = content, component_keys
    return read


def test_the_first_sync_call_only_asks_for_the_storage(session_state, storage, monkeypatch):
    session_state.update({"token": _jwt(0), "refresh_token": "refresh-1"})
    monkeypatch.setattr(utils, "GrinningCatClient", lambda *a, **k: pytest.fail("must wait for the storage"))

    assert utils.refresh_session_synced() == utils.REFRESH_PENDING

    assert session_state["_sync_nonce"]
    assert len(set(storage.component_keys)) == 2  # one read for token, one for refresh_token


def test_a_refresh_already_done_by_another_tab_is_adopted(session_state, storage, persisted, monkeypatch):
    """The other tab already rotated: use its tokens, do not call the backend with the stale one."""
    fresh = _jwt(int(time.time()) + 3600)
    session_state.update({"token": _jwt(0), "refresh_token": "refresh-1", "_sync_nonce": "n1"})
    storage.content.update({"token": fresh, "refresh_token": "refresh-2"})
    monkeypatch.setattr(utils, "GrinningCatClient", lambda *a, **k: pytest.fail("already refreshed elsewhere"))

    assert utils.refresh_session_synced() == utils.REFRESH_OK

    assert session_state["token"] == fresh
    assert session_state["refresh_token"] == "refresh-2"
    assert "_sync_nonce" not in session_state


def test_the_refresh_uses_the_token_stored_by_another_tab(session_state, storage, persisted, monkeypatch):
    """The other tab rotated but its access token has meanwhile expired too."""
    session_state.update({"token": _jwt(0), "refresh_token": "refresh-1", "_sync_nonce": "n1"})
    storage.content["refresh_token"] = "refresh-2"
    auth = _FakeAuth(refreshed=TokenOutput(access_token="new-jwt", refresh_token="refresh-3", refresh_expires_in=900))
    monkeypatch.setattr(utils, "GrinningCatClient", _fake_client(auth))

    assert utils.refresh_session_synced() == utils.REFRESH_OK

    assert auth.refresh_calls == ["refresh-2"]
    assert session_state["refresh_token"] == "refresh-3"


def test_the_refresh_uses_the_own_token_when_the_storage_is_unchanged(session_state, storage, persisted, monkeypatch):
    session_state.update({"token": _jwt(0), "refresh_token": "refresh-1", "_sync_nonce": "n1"})
    storage.content["refresh_token"] = "refresh-1"
    auth = _FakeAuth(refreshed=TokenOutput(access_token="new-jwt", refresh_token="refresh-2", refresh_expires_in=900))
    monkeypatch.setattr(utils, "GrinningCatClient", _fake_client(auth))

    assert utils.refresh_session_synced() == utils.REFRESH_OK

    assert auth.refresh_calls == ["refresh-1"]
    assert "_sync_nonce" not in session_state


def test_an_emptied_storage_is_left_to_the_backend_to_judge(session_state, storage, monkeypatch):
    """Another tab logged out: the backend refuses the revoked token and the user lands on the login."""
    session_state.update({"token": _jwt(0), "refresh_token": "refresh-1", "_sync_nonce": "n1"})
    auth = _FakeAuth(refresh_error=_http_error(401))
    monkeypatch.setattr(utils, "GrinningCatClient", _fake_client(auth))

    assert utils.refresh_session_synced() == utils.REFRESH_REJECTED

    assert auth.refresh_calls == ["refresh-1"]
    assert "_sync_nonce" not in session_state


def test_every_sync_reads_the_storage_anew(session_state, storage, monkeypatch):
    """Component results are memoized per key: reusing one would replay an old read."""
    session_state.update({"token": _jwt(0), "refresh_token": "refresh-1"})
    monkeypatch.setattr(utils, "GrinningCatClient", _fake_client(_FakeAuth(refresh_error=_http_error(503))))

    utils.refresh_session_synced()
    first = set(storage.component_keys)
    utils.refresh_session_synced()  # completes the first sync
    storage.component_keys.clear()
    utils.refresh_session_synced()  # starts another one

    assert not first & set(storage.component_keys)


# --- page refresh -------------------------------------------------------------

def _prepare_second_render(session_state, monkeypatch, stored):
    monkeypatch.delenv("GRINNING_CAT_API_KEY", raising=False)
    session_state.update({"_session_key": "sess", "initial_auth_check_done": True})
    monkeypatch.setattr(main, "_apply_custom_css", lambda: None)
    monkeypatch.setattr(
        main, "_check_status", lambda: session_state.__setitem__("status_connection", "Online")
    )
    monkeypatch.setattr(main, "time", _NoSleep)
    monkeypatch.setattr(st, "title", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "get_with_expiry", lambda key, *args, **kwargs: stored.get(key))


def test_both_tokens_are_read_back_after_a_page_refresh(session_state, no_rerun, monkeypatch):
    """Otherwise a logout after F5 could not revoke anything."""
    _prepare_second_render(session_state, monkeypatch, {"token": "jwt", "refresh_token": "refresh-1"})

    with pytest.raises(no_rerun):
        asyncio.run(main._main())

    assert session_state["token"] == "jwt"
    assert session_state["refresh_token"] == "refresh-1"


def test_an_expired_access_token_is_renewed_after_a_page_refresh(session_state, no_rerun, monkeypatch):
    """get_with_expiry() drops the access token at its exp, but the refresh token outlives it."""
    _prepare_second_render(session_state, monkeypatch, {"refresh_token": "refresh-1"})
    monkeypatch.setattr(main, "refresh_session", lambda: utils.REFRESH_OK)

    with pytest.raises(no_rerun):
        asyncio.run(main._main())

    assert session_state["refresh_token"] == "refresh-1"


def test_a_refused_refresh_token_after_a_page_refresh_shows_the_login(session_state, monkeypatch):
    _prepare_second_render(session_state, monkeypatch, {"refresh_token": "refresh-1"})
    cleared, shown = [], []
    monkeypatch.setattr(main, "refresh_session", lambda: utils.REFRESH_REJECTED)
    monkeypatch.setattr(main, "clear_auth_cookies", lambda: cleared.append(True))
    monkeypatch.setattr(main, "login_page", lambda: shown.append(True))

    asyncio.run(main._main())

    assert cleared == [True] and shown == [True]
    assert "token" not in session_state and "refresh_token" not in session_state


def test_a_refused_refresh_token_is_not_read_back_from_localstorage(session_state, monkeypatch):
    _prepare_second_render(session_state, monkeypatch, {"refresh_token": "refresh-1"})
    session_state["rejected_refresh_token"] = "refresh-1"
    shown = []
    monkeypatch.setattr(main, "refresh_session", lambda: pytest.fail("already refused"))
    monkeypatch.setattr(main, "login_page", lambda: shown.append(True))

    asyncio.run(main._main())

    assert shown == [True]


def test_agent_toggle_keeps_the_refresh_token(session_state, no_rerun, monkeypatch):
    session_state.update({"token": "jwt", "refresh_token": "refresh-1", "agent_id": "agent-a"})
    monkeypatch.setattr(
        main, "build_agents_options_select", lambda cookie_me, excluded_agents=None: {"agent-b": "agent-b"}
    )
    monkeypatch.setattr(st, "selectbox", lambda *args, **kwargs: "agent-b")
    monkeypatch.setattr(st, "divider", lambda *args, **kwargs: None)

    with pytest.raises(no_rerun):
        main._build_agents_toggle_select("sidebar_nav", {})

    assert session_state["refresh_token"] == "refresh-1"


# --- logout from another tab --------------------------------------------------
# The backend only revokes the refresh token: the access token of the other tabs
# stays valid until its exp, so they must notice the logout themselves.

def test_the_watcher_stays_quiet_until_another_tab_logs_out(session_state, monkeypatch):
    session_state["_session_key"] = "sess"
    calls = []
    monkeypatch.setattr(
        utils, "streamlit_js_eval", lambda js_expressions, key: calls.append((js_expressions, key)) or None
    )

    assert utils.logged_out_elsewhere() is False

    js, key = calls[0]
    assert "'storage'" in js and "token" in js and "refresh_token" in js
    assert "sess" in key


def test_the_watcher_fires_when_another_tab_logs_out(session_state, monkeypatch):
    session_state["_session_key"] = "sess"
    monkeypatch.setattr(utils, "streamlit_js_eval", lambda js_expressions, key: 1758380000000)

    assert utils.logged_out_elsewhere() is True


def test_a_logout_elsewhere_only_ends_the_local_session(session_state, no_rerun, monkeypatch):
    """The session is already revoked, and the other tab may be logging in again right now: this tab
    must neither call the backend nor remove the entries the other tab is writing."""
    session_state.update({
        "token": _jwt(int(time.time()) + 3600), "refresh_token": "refresh-1", "_session_key": "sess",
        "initial_auth_check_done": True,
    })
    cleared = []
    monkeypatch.setattr(main, "_apply_custom_css", lambda: None)
    monkeypatch.setattr(
        main, "_check_status", lambda: session_state.__setitem__("status_connection", "Online")
    )
    monkeypatch.setattr(main, "get_management_state", lambda: {})
    monkeypatch.setattr(main, "_render_management_banner", lambda: None)
    monkeypatch.setattr(main, "logged_out_elsewhere", lambda: True)
    monkeypatch.setattr(main, "revoke_session", lambda: pytest.fail("already revoked"))
    monkeypatch.setattr(main, "_render_page", lambda me: pytest.fail("must not render a logged out session"))
    _stub_logout_side_effects(monkeypatch, cleared)

    with pytest.raises(no_rerun):
        asyncio.run(main._main())

    assert cleared == []
    assert "token" not in session_state and "refresh_token" not in session_state
    assert session_state["auth_error"] == "You have been logged out from another tab."


def test_a_credentials_session_is_watched_on_every_run(session_state, monkeypatch):
    session_state.update({
        "token": _jwt(int(time.time()) + 3600), "refresh_token": "refresh-1", "_session_key": "sess",
        "status_connection": "Online", "initial_auth_check_done": True, "me": {"agents": []},
    })
    watched, rendered = [], []
    monkeypatch.setattr(main, "_apply_custom_css", lambda: None)
    monkeypatch.setattr(main, "_check_status", lambda: None)
    monkeypatch.setattr(main, "logged_out_elsewhere", lambda: watched.append(True) or False)
    monkeypatch.setattr(main, "_render_sidebar_navigation", lambda me: None)

    async def render(me):
        rendered.append(True)

    monkeypatch.setattr(main, "_render_page", render)

    asyncio.run(main._main())

    assert watched == [True] and rendered == [True]


def test_tokens_adopted_from_another_tab_rebuild_the_user(session_state, storage, persisted, monkeypatch):
    """Another tab may have logged in as somebody else: the cached 'me' would describe the wrong user."""
    fresh = _jwt(int(time.time()) + 3600)
    session_state.update({
        "token": _jwt(0), "refresh_token": "refresh-1", "_sync_nonce": "n1", "me": {"username": "old"},
    })
    storage.content.update({"token": fresh, "refresh_token": "refresh-2"})

    assert utils.refresh_session_synced() == utils.REFRESH_OK

    assert "me" not in session_state
