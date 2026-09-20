import base64
import json
import time
import uuid
from typing import Dict, Any, List, Tuple
from grinning_cat_python_sdk.models.api.nested.plugins import PluginSettingsOutput
from slugify import slugify
import streamlit as st
from requests.exceptions import HTTPError
from grinning_cat_python_sdk import GrinningCatClient, Configuration
from grinning_cat_python_sdk.models.api.factories import FactoryObjectSettingOutput
from streamlit_js_eval import get_local_storage, set_local_storage, remove_local_storage, streamlit_js_eval

from app.constants import DEFAULT_SYSTEM_KEY
from app.env import get_env, get_env_bool

REFRESH_OK = "ok"
REFRESH_REJECTED = "rejected"  # the backend refused the refresh token (or there is none): the session is over
REFRESH_ERROR = "error"  # transient failure: keep the session, retry on the next rerun
REFRESH_PENDING = "pending"  # waiting for the localStorage read: end this run, the next one completes the sync

# refresh this long before the access token's exp, so no request goes out with a token about to expire
ACCESS_TOKEN_REFRESH_MARGIN_SECONDS = 30


def get_settings(
    settings: PluginSettingsOutput, is_selected: bool
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    # values come from the saved config when selected, from scheme defaults otherwise
    values = dict(settings.value) if is_selected else {}

    types = {}
    if settings.scheme:
        for k, v in settings.scheme.properties.items():
            if k not in values:
                values[k] = v.default
            descriptor = {"type": v.type or "string"}
            # PropertySettingsOutput exposes JSON-Schema metadata directly; also
            # fall back to `extra` for older SDK versions that only surface it there.
            for field in ("description", "enum", "format"):
                val = getattr(v, field, None)
                if val is None and v.extra and isinstance(v.extra, dict):
                    val = v.extra.get(field)
                if val is not None:
                    descriptor[field] = val
            types[k] = descriptor
    return values, types


def get_factory_settings(
    factory: FactoryObjectSettingOutput, is_selected: bool
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Get the settings of a factory instance.

    Args:
        factory: The factory instance to get settings from.
        is_selected: A boolean indicating if the factory is selected.

    Returns:
        A tuple containing two dictionaries:
            - The first dictionary contains the current values of the factory settings.
            - The second dictionary contains per-field descriptors with the field
              type and any JSON-Schema metadata (description, enum, format).
    """
    def get_type(v):
        if "type" in v:
            return v["type"]
        if "anyOf" in v:
            tmp_types = [t.get("type") for t in v["anyOf"] if "type" in t and t.get("type") != "null"]
            return tmp_types[0] if tmp_types else "string"
        return "string"

    # values come from the saved config when selected, from scheme defaults otherwise
    values = dict(factory.value) if is_selected else {}

    types = {}
    if factory.scheme:
        for k, v in factory.scheme.get("properties", {}).items():
            if isinstance(v, dict):
                if k not in values:
                    values[k] = v.get("default")
                descriptor = {"type": get_type(v)}
                for field in ("description", "enum", "format"):
                    if field in v and v[field] is not None:
                        descriptor[field] = v[field]
                types[k] = descriptor
    return values, types


def build_agents_options_select(cookie_me: Dict | None, excluded_agents: List[str] | None = None) -> Dict[str, str]:
    if cookie_me:  # login by credentials
        agents = [agent["agent_name"] for agent in cookie_me.get("agents", [])]
    else:  # login by API key
        client = GrinningCatClient(build_client_configuration())
        agents = [agent.agent_id for agent in client.utils.get_agents()]

    return {
        agent: slugify(agent) for agent in agents if agent not in (excluded_agents or [])
    }


def build_agents_select(k: str, cookie_me: Dict | None, force_system_agent: bool = False):
    if st.session_state.get("agent_id") is not None and cookie_me is not None:
        return  # already selected and logged by credentials

    # Navigation
    agent_options = build_agents_options_select(cookie_me)
    if force_system_agent and DEFAULT_SYSTEM_KEY not in agent_options:
        agent_options = {DEFAULT_SYSTEM_KEY: slugify(DEFAULT_SYSTEM_KEY)} | agent_options
    if len(agent_options) == 0:
        st.info("No agents found. Please create an agent first.")
        return

    menu_options = {"(Select an Agent)": None} | agent_options
    choice = st.selectbox("Agents", menu_options, key=f"agent_select_{k}")
    if menu_options[choice] is None:
        st.info("Please select an agent to manage.")
        st.session_state.pop("agent_id", None)
        if not cookie_me:
            st.session_state.pop("user_id", None)
            st.session_state.pop("conversation_id", None)
        return

    st.session_state["agent_id"] = choice


def build_users_select(k: str, agent_id: str, cookie_me: Dict | None):
    if st.session_state.get("user_id") is not None and cookie_me is not None:
        return  # already selected

    if cookie_me:  # login by credentials
        agent_match = next((agent for agent in cookie_me.get("agents", []) if agent.get("agent_name") == agent_id), None)
        if not agent_match:
            st.error("Agent not found in user data.")
            return
        st.session_state["user_id"] = agent_match.get("user", {}).get("id")
        return

    client = GrinningCatClient(build_client_configuration())
    users = client.users.get_users(agent_id)

    # Navigation
    menu_options = {"(Select an User)": None} | {user.username: user.id for user in users}
    choice = st.selectbox("Users", menu_options, key=f"user_select_{k}")
    if menu_options[choice] is None:
        st.info("Please select an user to manage.")
        st.session_state.pop("user_id", None)
        return

    st.session_state["user_id"] = menu_options[choice]


def build_conversations_select(k: str, agent_id: str, user_id: str):
    client = GrinningCatClient(build_client_configuration())
    conversations = client.conversation.get_conversations(agent_id, user_id)

    if not conversations:
        st.info("No conversations found for this user.")
        st.session_state.pop("user_id", None)
        st.session_state.pop("conversation_id", None)
        return

    useful_conversations = {
        conversation.name: conversation.chat_id for conversation in conversations if conversation.num_messages
    }
    if not useful_conversations:
        st.info("No conversations found for this user.")
        st.session_state.pop("user_id", None)
        st.session_state.pop("conversation_id", None)
        return

    # Navigation
    menu_options = {"(Select a Conversation)": None} | useful_conversations
    choice = st.selectbox("Conversations", menu_options, key=f"conversation_select_{k}")
    if menu_options[choice] is None:
        st.info("Please select a conversation to manage.")
        st.session_state.pop("conversation_id", None)
        return

    st.session_state["conversation_id"] = menu_options[choice]


def run_toast():
    if st.session_state.get("toast") is None:
        return
    toast = st.session_state["toast"]
    st.toast(toast["message"], icon=toast["icon"])
    st.session_state.pop("toast", None)


def show_overlay_spinner(message="Processing..."):
    """Show a full-page overlay spinner"""
    spinner_container = st.empty()
    with spinner_container.container():
        st.markdown(f"""
<style>
.overlay-spinner {{
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background-color: rgba(0, 0, 0, 0.5);
    z-index: 9999;
    display: flex;
    justify-content: center;
    align-items: center;
    color: white;
    font-size: 18px;
}}
.spinner {{
    border: 4px solid #f3f3f3;
    border-top: 4px solid #3498db;
    border-radius: 50%;
    width: 40px;
    height: 40px;
    animation: spin 2s linear infinite;
    margin-right: 15px;
}}
@keyframes spin {{
    0% {{ transform: rotate(0deg); }}
    100% {{ transform: rotate(360deg); }}
}}
</style>
<div class="overlay-spinner">
    <div class="spinner"></div>
    <div>{message}</div>
</div>
""", unsafe_allow_html=True)
    return spinner_container


def get_management_state() -> Dict[str, Any]:
    """Read the instance's management banner from the mgmt_message plugin.

    GET /mgmt_message/global_message is public by design and is one of the few
    routes the core keeps answering while management mode is on, so it is the
    only reliable way for the UI to know. A failed read returns {}: the admin
    must not lock itself out of its own System page over an unreachable banner.
    """
    try:
        return GrinningCatClient(build_client_configuration()).custom.get_global_message() or {}
    except Exception as e:
        print(f"Error reading the management banner: {e}")
        return {}


def is_management_active(management: Dict[str, Any] | None = None) -> bool:
    return bool((management or {}).get("management_active"))


def management_banner_message(management: Dict[str, Any] | None = None) -> str:
    """The text to show while management mode is on.

    The management message is the one describing the maintenance; the global
    notice is used as a fallback when the former was left empty.
    """
    management = management or {}
    return management.get("management_message") or management.get("global_message") or ""


def is_api_key_mode() -> bool:
    """True when the UI is configured to talk to the backend with a static API key."""
    return bool(get_env("GRINNING_CAT_API_KEY"))


def build_client_configuration():
    # The SDK sends auth_key as `Authorization: Bearer <...>` and the backend
    # routes it by shape (JWT -> credentials auth, anything else -> API-key
    # auth), so a credentials token always wins over the configured API key.
    return Configuration(
        host=get_env("GRINNING_CAT_API_HOST").replace("https://", "").replace("http://", ""),
        port=int(get_env("GRINNING_CAT_API_PORT")),
        auth_key=st.session_state.get("token") or get_env("GRINNING_CAT_API_KEY"),
        secure_connection=get_env_bool("GRINNING_CAT_API_SECURE_CONNECTION"),
    )


def render_json_form(data: Dict, types: Dict, prefix: str = "") -> Dict:
    """Recursively render form fields for JSON data.

    `types` maps each field name to either a plain type string (legacy) or a
    descriptor dict with keys: type, description, enum, format. Descriptors
    come from the JSON-Schema produced by Pydantic, so the description is
    shown as a help tooltip, enum values become a chooser and password-like
    fields are rendered masked (Streamlit adds a native show/hide eye icon).
    """

    def infer_type() -> str:
        if value is None:
            return types.get(key, "string") if isinstance(types.get(key), str) else "string"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "float"
        if isinstance(value, str):
            return "string"
        if isinstance(value, (list, dict)):
            return "json"
        return "string"

    def get_descriptor() -> Dict[str, Any]:
        t = types.get(key, "string")
        if isinstance(t, dict):
            return t
        return {"type": t}

    def is_secret_field() -> bool:
        desc = get_descriptor()
        if desc.get("format") in ("password", "api-key", "secret", "bearer"):
            return True
        name = key.lower()
        return (
            "password" in name
            or name.endswith("api_key")
            or name.endswith("apikey")
            or name.endswith("secret")
            or name.endswith("_key")
            or name.endswith("_token")
            or name == "token"
        )

    def create_input_field() -> Any:
        desc = get_descriptor()
        field_type = infer_type()
        hint = desc.get("description")

        # Enum values -> chooser (selectbox)
        enum_values = desc.get("enum")
        if isinstance(enum_values, list) and enum_values:
            options = [str(o) for o in enum_values]
            current = "" if value is None else str(value)
            if current not in options:
                options = [current] + options if current else options
            index = options.index(current) if current in options else 0
            return st.selectbox(key, options, index=index, key=path, help=hint)

        if field_type == "boolean":
            return st.checkbox(key, value=value, key=path, help=hint)
        if field_type == "integer":
            return st.number_input(key, value=value, step=1, key=path, help=hint)
        if field_type == "float":
            return st.number_input(key, value=value, step=0.1, format="%.2f", key=path, help=hint)
        if field_type == "string":
            if is_secret_field():
                # type="password" gives Streamlit's native show/hide eye toggle
                return st.text_input(key, value=value, type="password", key=path, help=hint)
            if isinstance(value, str) and "\n" in value:
                # Multi-line strings (e.g. prompt templates) -> textarea
                return st.text_area(key, value=value, height=150, key=path, help=hint)
            return st.text_input(key, value=value, key=path, help=hint)
        if field_type == "json":
            # For nested structures, show as editable JSON text
            json_str = json.dumps(value, indent=2)
            r = st.text_area(key, value=json_str, height=100, key=path, help=hint)
            try:
                return json.loads(r)
            except:
                st.error(f"Invalid JSON in field '{key}'")
                return value
        return value

    result = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        result[key] = create_input_field()

    return result


def has_access(resource: str, required_role: str | None, cookie_me: Dict | None, only_admin: bool | None = False) -> bool:
    """Check if the logged-in user has the required role."""
    if not cookie_me: # logged by API key
        return True

    agent_id = st.session_state.get("agent_id")
    if not agent_id:
        return False

    if only_admin and agent_id != DEFAULT_SYSTEM_KEY:
        return False

    try:
        # in cookie_me.agents find the one with agent_id
        agent_match = next((agent for agent in cookie_me.get("agents", []) if agent.get("agent_name") == agent_id), None)
        if not agent_match:
            return False

        user_permissions = agent_match.get("user", {}).get("permissions", {}).get(resource, [])
        return required_role in user_permissions if required_role else len(user_permissions) > 0
    except json.JSONDecodeError:
        return False


def clear_auth_cookies():
    """Clear authentication-related localStorage entries.

    'me' is no longer written by this app, but entries left by older versions
    must still be dropped on logout.
    """
    remove_local_storage("token")
    remove_local_storage("refresh_token")
    remove_local_storage("me")


def is_system_agent_selected() -> bool:
    return st.session_state.get("agent_id") == DEFAULT_SYSTEM_KEY


def _jwt_exp(token: str | None) -> int | None:
    """The 'exp' claim (unix timestamp) of a JWT, or None if it is not a decodable JWT (e.g. an API key)."""
    try:
        payload = token.split(".")[1]
        # base64url decode with padding
        padding = "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload + padding)).get("exp")
        return exp if isinstance(exp, int) else None
    except Exception:
        return None


def _get_exp_from_jwt(token: str) -> int:
    """
    Extract the 'exp' claim (unix timestamp) from a JWT token payload.
    Falls back to now + GRINNING_CAT_JWT_EXPIRE_MINUTES if the token is not a
    decodable JWT (e.g. API-key mode), so the expiry simulation always yields a
    timestamp.
    """
    exp = _jwt_exp(token)
    if exp is not None:
        return exp

    return int(time.time()) + int(get_env("GRINNING_CAT_JWT_EXPIRE_MINUTES")) * 60


def access_token_expires_soon(token: str | None) -> bool:
    """True if the access token is expired or about to be. Tokens without an exp claim never are."""
    exp = _jwt_exp(token)
    return exp is not None and exp - time.time() <= ACCESS_TOKEN_REFRESH_MARGIN_SECONDS


def _escape_for_js_string(value: str) -> str:
    r"""
    Escape a value for the single-quoted JS string literal that
    streamlit_js_eval builds: localStorage.setItem('<key>', '<value>').

    The value is interpolated verbatim, so the JS parser consumes its escape
    sequences: every backslash produced by json.dumps() would be eaten (the
    nested \" of the 'me' envelope becoming a bare ", which no longer decodes)
    and a single quote would terminate the literal altogether.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def set_with_expiry(key: str, value: str, token: str, expires_in: int | None = None):
    """
    Write a value to localStorage wrapped in an envelope:
        {"value": ..., "expire": <unix timestamp>}
    The expire timestamp is derived from the JWT exp claim so the simulated
    expiry is aligned with the server-side token validity, or from `expires_in`
    (seconds) for values that are not JWTs, like the opaque refresh token.
    """
    expire = int(time.time()) + expires_in if expires_in is not None else _get_exp_from_jwt(token)
    envelope = {"value": value, "expire": expire}
    set_local_storage(key, _escape_for_js_string(json.dumps(envelope)))


def get_with_expiry(key: str, component_key: str | None = None) -> str | None:
    """
    Read a value from localStorage, honoring the simulated expiry envelope.
    Returns None (and removes the entry) if the stored expire timestamp is in
    the past. If the stored value is not an envelope (legacy/plain data), it is
    returned as-is for backward compatibility.
    """
    raw = get_local_storage(key, component_key=component_key)
    if not raw:
        return None

    try:
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or "expire" not in envelope:
            # Not an envelope written by us: legacy value, treat as valid.
            return envelope if isinstance(envelope, str) else raw
        if int(envelope.get("expire", 0)) < int(time.time()):
            # Simulated expiry reached: drop the entry and treat as logged out.
            remove_local_storage(key)
            return None
        return envelope.get("value")
    except json.JSONDecodeError:
        # Plain (non-JSON) value stored by older code or tests.
        return raw


def _decode_agents(raw_agents: list) -> list:
    """
    Decode the agents list from the JWT response.
    Each element may be either a dict (already decoded) or a JSON string
    (as returned by model_dump() on the SDK's JWTPayload model).
    """
    result = []
    for item in raw_agents:
        if isinstance(item, str):
            try:
                result.append(json.loads(item))
            except json.JSONDecodeError:
                pass
        elif isinstance(item, dict):
            result.append(item)
    return result


def build_me_data() -> Dict:
    """
    Call /auth/me, store the result in st.session_state["me"] and return it.

    Nothing is written to localStorage: only the token is persisted there, and
    'me' is rebuilt from the API whenever session_state is empty. That keeps
    the permissions the UI enforces in sync with the backend and avoids racing
    the asynchronous set_local_storage() against a st.rerun().
    """
    client = GrinningCatClient(build_client_configuration())
    res = client.auth.me(st.session_state.get("token"))
    raw = res.model_dump()

    decoded_agents = _decode_agents(raw.get("agents", []))
    first_user = decoded_agents[0].get("user", {}) if decoded_agents else {}
    me_data = {
        "username": raw.get("sub", ""),
        "id": first_user.get("id", ""),
        "exp": raw.get("exp", ""),
        "agents": decoded_agents,
    }
    st.session_state["me"] = me_data
    return me_data


def _persist_tokens(tokens) -> None:
    """Keep session_state and localStorage in sync with the tokens the backend just issued."""
    st.session_state["token"] = tokens.access_token
    set_with_expiry("token", tokens.access_token, tokens.access_token)
    if tokens.refresh_token:
        st.session_state["refresh_token"] = tokens.refresh_token
        set_with_expiry("refresh_token", tokens.refresh_token, tokens.access_token, tokens.refresh_expires_in)


def refresh_session() -> str:
    """
    Exchange the refresh token for a new access token AND a new refresh token.

    The refresh token is single-use: the backend consumes it and treats a second
    presentation as theft, closing the whole session. The rotated one therefore
    replaces the old one in session_state and localStorage right away.
    Returns REFRESH_OK, REFRESH_REJECTED or REFRESH_ERROR.
    """
    refresh_token = st.session_state.get("refresh_token")
    if not refresh_token:
        return REFRESH_REJECTED

    try:
        tokens = GrinningCatClient(build_client_configuration()).auth.refresh(refresh_token)
    except HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        # 401: expired, revoked, reused or user changed. Anything else (429, 5xx) says nothing about the session.
        return REFRESH_REJECTED if status == 401 else REFRESH_ERROR
    except Exception as e:
        print(f"Error refreshing the session: {e}")
        return REFRESH_ERROR

    _persist_tokens(tokens)
    return REFRESH_OK


def refresh_session_synced() -> str:
    """
    refresh_session() for a browser with several tabs of the app open.

    localStorage is shared between tabs, session_state is not: if another tab has
    already rotated the single-use refresh token, the copy held by this one is
    stale, and presenting it would be read by the backend as theft and close the
    session for every tab. So the storage is read first, in two runs (the read is
    asynchronous, like every localStorage access): the first returns
    REFRESH_PENDING, the next one adopts whatever the other tab stored and only
    calls the backend if that is still needed.
    """
    nonce = st.session_state.get("_sync_nonce")
    if nonce is None:
        # a new key per sync: component results are memoized per key
        nonce = uuid.uuid4().hex
        st.session_state["_sync_nonce"] = nonce
        for key in ("token", "refresh_token"):
            get_with_expiry(key, component_key=f"getLS_sync_{key}_{nonce}")
        return REFRESH_PENDING

    st.session_state.pop("_sync_nonce")
    stored_token = get_with_expiry("token", component_key=f"getLS_sync_token_{nonce}")
    stored_refresh_token = get_with_expiry("refresh_token", component_key=f"getLS_sync_refresh_token_{nonce}")

    if stored_refresh_token:
        if stored_refresh_token != st.session_state.get("refresh_token"):
            # another tab took over the session, maybe logging in as somebody else: 'me' must be rebuilt
            st.session_state.pop("me", None)
        st.session_state["refresh_token"] = stored_refresh_token
    if stored_token and not access_token_expires_soon(stored_token):
        st.session_state["token"] = stored_token
        return REFRESH_OK
    return refresh_session()


# Resolves when a storage event, fired by ANOTHER tab of the same browser, leaves no credentials in localStorage.
# The expression is evaluated once per component key and the listener lives as long as the component is
# rendered: no rerun happens until the promise resolves.
_LOGOUT_WATCH_JS = """
new Promise(resolve => {
  window.addEventListener('storage', () => {
    if (localStorage.getItem('token') === null && localStorage.getItem('refresh_token') === null) {
      resolve(Date.now());
    }
  });
})
"""


def logged_out_elsewhere() -> bool:
    """
    True once another tab of this browser has logged out.

    The backend only revokes the refresh token: the access token held by the other
    tabs stays valid until its exp, so without this they would keep working.
    """
    fired = streamlit_js_eval(
        js_expressions=_LOGOUT_WATCH_JS, key=f"watch_logout_{st.session_state.get('_session_key')}"
    )
    return fired is not None


def revoke_session() -> None:
    """Best effort: ask the backend to revoke the session. Never blocks a logout."""
    refresh_token = st.session_state.get("refresh_token")
    if not refresh_token:
        return

    try:
        GrinningCatClient(build_client_configuration()).auth.logout(refresh_token)
    except Exception as e:
        print(f"Error revoking the session: {e}")
