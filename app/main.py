import asyncio
import time
from typing import Dict
import streamlit as st
from dotenv import load_dotenv
from grinning_cat_python_sdk import GrinningCatClient
from requests.exceptions import HTTPError

from app.constants import CHECK_INTERVAL, DEFAULT_SYSTEM_KEY, WELCOME_MESSAGE
from app.env import get_env
from app.utils import (
    get_with_expiry,
    build_agents_options_select,
    build_client_configuration,
    build_me_data,
    clear_auth_cookies,
    get_management_state,
    has_access,
    is_api_key_mode,
    is_management_active,
    is_system_agent_selected,
    management_banner_message,
)
from app.routes.agentic_workflows import agentic_workflows_management
from app.routes.auth_handlers import auth_handlers_management
from app.routes.chunkers import chunkers_management
from app.routes.context_retriever import context_retrievers_management
from app.routes.embedders import embedders_management
from app.routes.ingestion import ingestion_management
from app.routes.file_managers import file_managers_management
from app.routes.llms import llms_management
from app.routes.loading import loading_page
from app.routes.login import login_page
from app.routes.memories import memory_management
from app.routes.message import chat
from app.routes.plugins import plugins_management
from app.routes.rabbit_hole import rabbit_hole_management
from app.routes.users import users_management
from app.routes.utilities import utilities_management
from app.routes.vector_databases import vector_databases_management
from app.routes.welcome import welcome


def _logout(message: str, icon: str, keep_message: bool = False):
    """Drop the session and the persisted token, then restart the script."""
    rejected_token = st.session_state.get("token")

    st.session_state.clear()
    clear_auth_cookies()
    if keep_message:
        # Survives the clear() above; login_page() displays and pops it.
        st.session_state["auth_error"] = message
        # Removing the localStorage entry is asynchronous: remember the token
        # the backend just refused so that a slow removal cannot feed it back
        # to us and spin the login/refuse cycle forever.
        st.session_state["rejected_token"] = rejected_token

    st.toast(message, icon=icon)
    time.sleep(1)  # let the asynchronous localStorage removal land first

    st.rerun()


def _get_cookie_me() -> Dict | None:
    """Return the current user's 'me' dict for a credentials session.

    Only the token is persisted across page refreshes, so 'me' is rebuilt from
    /auth/me whenever session_state is empty. Returns None in API-key mode,
    where there is no logged-in user to describe, and when the backend cannot
    be reached, so the next rerun retries.
    """
    # session_state is authoritative within a Streamlit session
    if "me" in st.session_state:
        return st.session_state["me"]

    if not st.session_state.get("token"):
        return None

    try:
        return build_me_data()
    except HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status in (401, 403):
            # The backend rejected the token: this is an expired or revoked
            # session, not an API-key one. Granting the API-key branch here
            # would show the whole admin UI to a user the backend refuses.
            _logout("Your session has expired. Please log in again.", "⏱️", keep_message=True)
        print(f"Error rehydrating me from API: {e}")
        return None
    except Exception as e:
        print(f"Error rehydrating me from API: {e}")
        return None


# Entries that identify the browser session rather than the selected agent.
# Wiping them logs the user out and drops the app into the API-key branch.
_SESSION_SCOPED_KEYS = (
    "token",
    "me",
    "_session_key",
    "status_connection",
    "initial_auth_check_done",
)


def _reset_agent_scoped_state():
    """Drop every per-agent entry (including widget keys), keeping the session."""
    preserved = {k: st.session_state[k] for k in _SESSION_SCOPED_KEYS if k in st.session_state}
    st.session_state.clear()
    st.session_state.update(preserved)


def _build_agents_toggle_select(k: str, cookie_me: Dict | None):
    excluded_agents = []
    if st.session_state.get("agent_id") is not None:
        excluded_agents.append(st.session_state["agent_id"])

    agent_options = build_agents_options_select(cookie_me, excluded_agents=excluded_agents)
    if len(agent_options) == 0:
        return

    menu_options = {"(Select an Agent)": None} | agent_options
    choice = st.selectbox("Toggle Agent", menu_options, key=f"agent_toggle_select_{k}")
    st.divider()

    if menu_options[choice] is None:
        return

    _reset_agent_scoped_state()
    st.session_state["agent_id"] = choice
    st.rerun()


def _apply_custom_css():
    """Apply custom CSS for enhanced styling"""
    hide_dev_toolbar = """
/* Hide the ENTIRE development toolbar */
.stDeployButton {display: none;}

/* If the above doesn't work, try these selectors */
#stDeployButton {display: none;}
button[kind="header"] {display: none;}
div[data-testid="stToolbar"] {display: none;}
div[data-testid="stDecoration"] {display: none;}
div[data-testid="stStatusWidget"] {display: none;}

/* Hide the hamburger menu too */
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
"""

    st.markdown(f"""
<style>
{hide_dev_toolbar if get_env('GRINNING_CAT_ENVIRONMENT') == 'prod' else ''}

/* Main content area */
.main .block-container {{
    padding-top: 2rem;
    padding-bottom: 2rem;
}}

/* Custom card styling */
.info-card {{
    background: white;
    padding: 1.5rem;
    border-radius: 10px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
    margin: 1rem 0;
    border-left: 4px solid #667eea;
}}

/* Navigation title styling */
.nav-title {{
    font-size: 1.5rem;
    font-weight: bold;
    // color: #2c3e50;
    margin-bottom: 1rem;
    padding: 0.5rem;
    border-bottom: 2px solid #667eea;
}}

/* Status indicators */
.status-indicator {{
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    margin-right: 8px;
}}

.status-online {{ background-color: #2ecc71; }}
.status-offline {{ background-color: #e74c3c; }}
.status-warning {{ background-color: #f39c12; }}

.picked {{ margin-top: 0.65rem; margin-left: 0.5rem; margin-right: auto; }}
</style>
""", unsafe_allow_html=True)


@st.fragment(run_every=CHECK_INTERVAL)  # Run every 5 seconds
def _check_status():
    """Check backend status and display it"""
    current_status = st.session_state.get("status_connection", "Warning")
    try:
        client = GrinningCatClient(build_client_configuration())
        client.health_check.liveness()
        status_connection = "Online"
    except Exception:
        status_connection = "Offline"

    st.session_state["status_connection"] = status_connection
    if current_status != status_connection:
        st.rerun()


def _render_management_banner():
    """Show the instance's management message while management mode is on."""
    if not is_management_active(st.session_state.get("management")):
        return

    message = management_banner_message(st.session_state.get("management"))
    st.sidebar.warning(
        f"🛠️ The instance is in management mode.{f' {message}' if message else ''}",
    )


def _render_sidebar_navigation(cookie_me: Dict | None):
    """Render the sidebar navigation menu"""
    st.session_state["selected_page"] = st.session_state.get("selected_page")
    if not st.session_state.get("token") and not is_api_key_mode():
        st.session_state["selected_page"] = None
        return

    # In management mode the backend answers 404 on every route but the
    # mgmt_message plugin's own ones, so every other section would only show
    # errors: System is the one that can switch the mode back off.
    # Only once an agent is picked, though: every System entry is gated on the
    # system agent, and the welcome screen is the only place offering the
    # selector, so forcing the page earlier would leave an empty sidebar.
    if is_management_active(st.session_state.get("management")) and st.session_state.get("agent_id"):
        st.session_state["selected_page"] = "system"

    # Navigation menu with icons
    navigation_options = {
        "menu_chat": {
            "💬 Chat": {
                "page": "chat",
                "allowed": has_access("CHAT", None, cookie_me),
            },
            "🗂️ Memory & Chats": {
                "page": "memory",
                "allowed": has_access("MEMORY", None, cookie_me) and not is_system_agent_selected(),
            },
            "📚 Knowledge Base": {
                "page": "rag",
                "allowed": has_access("UPLOAD", None, cookie_me) and not is_system_agent_selected(),
            },
        },
        "menu_users": {
            "👥 Users": {
                "page": "users",
                "allowed": has_access("USERS", None, cookie_me),
            },
        },
        "menu_ai": {
            "🧬 AI Models": {
                "page": "ai_models",
                "allowed": has_access("LLM", None, cookie_me) and not is_system_agent_selected(),
            },
            "⚡ Agentic Workflows": {
                "page": "agentic_workflows",
                "allowed": has_access("AGENTIC_WORKFLOW", None, cookie_me) and not is_system_agent_selected(),
            },
            "🧠 Embedders": {
                "page": "embedders",
                "allowed": has_access("EMBEDDER", None, cookie_me, only_admin=True),
            },
            "⚙️ Ingestion": {
                "page": "ingestion",
                "allowed": has_access("SYSTEM", None, cookie_me, only_admin=True),
            },
        },
        "menu_data": {
            "🔪 Chunkers": {
                "page": "chunkers",
                "allowed": has_access("CHUNKER", None, cookie_me) and not is_system_agent_selected(),
            },
            "👨‍💼 Context Retrievers": {
                "page": "context_retrievers",
                "allowed": has_access("CONTEXT_RETRIEVER", None, cookie_me) and not is_system_agent_selected(),
            },
            "🔗 Vector Databases": {
                "page": "vector_databases",
                "allowed": has_access("VECTOR_DATABASE", None, cookie_me) and not is_system_agent_selected(),
            },
        },
        "menu_infra": {
            "🔌 Plugins": {
                "page": "plugins",
                "allowed": has_access("PLUGIN", None, cookie_me),
            },
            "🔐 Authentication Handlers": {
                "page": "auth_handlers",
                "allowed": has_access("AUTH_HANDLER", None, cookie_me) and not is_system_agent_selected(),
            },
            "📁 File Handlers": {
                "page": "file_handlers",
                "allowed": has_access("FILE_MANAGER", None, cookie_me) and not is_system_agent_selected(),
            },
        },
        "menu_system": {
            "⚙️ System": {
                "page": "system",
                "allowed": (
                    has_access("CHESHIRE_CAT", None, cookie_me, only_admin=True)
                    or has_access("SYSTEM", None, cookie_me, only_admin=True)
                ),
            },
        },
    }

    if is_management_active(st.session_state.get("management")):
        navigation_options = {"menu_system": navigation_options["menu_system"]}
        if not navigation_options["menu_system"]["⚙️ System"]["allowed"]:
            # no agent selected, or one that cannot reach System: say so, or
            # the sidebar would just be empty with no hint of what to do
            st.sidebar.info(
                f"Select the `{DEFAULT_SYSTEM_KEY}` agent to manage management mode."
            )

    # Create the navigation menu
    with st.sidebar:
        # Custom title with styling
        st.sidebar.markdown(f"""
<div class="nav-title">
    💬 Current Agent: {st.session_state.get("agent_id", "N/A")}
</div>
""", unsafe_allow_html=True)

        for menu_key, menu_items in navigation_options.items():
            for item_name, item_keys in menu_items.items():
                if not item_keys["allowed"]:
                    continue
                button = st.button(
                    item_name,
                    key=f"nav_{item_keys['page']}",
                    type="secondary",
                    use_container_width=True,
                    disabled=(
                            st.session_state.get("status_connection", None) != "Online"
                            or st.session_state["selected_page"] == item_keys["page"]
                    ),
                )
                if button:
                    st.session_state["selected_page"] = item_keys["page"]
                    if not cookie_me:
                        st.session_state.pop("agent_id", None)
                        st.session_state.pop("user_id", None)
                        st.session_state.pop("conversation_id", None)
                    st.rerun()  # Force immediate rerun

            if any(item["allowed"] for item in menu_items.values()):
                # Add separator
                st.divider()

        if st.session_state.get("agent_id") and cookie_me:
            _build_agents_toggle_select("sidebar_nav", cookie_me)

        # System status section
        status_connection = st.session_state.get("status_connection", "Warning")
        st.markdown(f"""
### 📡 System Status: <span class="status-indicator status-{status_connection.lower()}"></span> {status_connection}
""", unsafe_allow_html=True)

        # Add separator
        st.divider()

        if not cookie_me:
            st.info("""You are logged in with the default API key.
For security reasons, please consider creating admin users and logging in by credentials.""")

            return

        # logout button
        logout_button = st.button("Logout", type="primary", use_container_width=True)
        if logout_button:
            _logout("Logged out successfully.", "🚪")


async def _main():
    """Main application function"""
    # Apply custom styling
    _apply_custom_css()

    _check_status()
    if st.session_state["status_connection"] != "Online":
        st.title(WELCOME_MESSAGE)
        st.error("Grinning Cat backend is offline. Please check your connection.")
        return

    # Refreshed on every run, never cached: the whole point is to notice when
    # the mode is switched on or off, including from this very UI.
    st.session_state["management"] = get_management_state()
    _render_management_banner()

    # Assign a stable per-session key used to avoid Streamlit memoizing
    # get_local_storage() results across different browser sessions.
    if "_session_key" not in st.session_state:
        import uuid
        st.session_state["_session_key"] = uuid.uuid4().hex

    # If token is already in session_state this is an internal rerun (e.g.
    # post-login): skip the async localStorage cycle and go straight to the app.
    if st.session_state.get("token"):
        cookie_me = _get_cookie_me()
        _render_sidebar_navigation(cookie_me)
        await _render_page(cookie_me)
        return

    # --- First render after a true browser page-refresh ---
    # session_state is empty; we need to read the token from localStorage
    # asynchronously (via the web component).
    if not st.session_state.get("initial_auth_check_done"):
        # First render: fire the async localStorage read and show a loading screen.
        st.session_state["initial_auth_check_done"] = True
        get_with_expiry(
            "token", component_key=f"getLS_token_{st.session_state['_session_key']}"
        )
        st.title(WELCOME_MESSAGE)
        loading_page()
        return

    # Second render: the iframe has responded; read the cached result.
    cookie_token = get_with_expiry(
        "token", component_key=f"getLS_token_{st.session_state['_session_key']}"
    )
    if cookie_token and cookie_token != st.session_state.get("rejected_token"):
        st.session_state["token"] = cookie_token
        time.sleep(0.5)
        st.rerun()
        return

    # No stored credentials: run against the configured API key when there is
    # one, otherwise ask for a username and password.
    if is_api_key_mode():
        _render_sidebar_navigation(None)
        await _render_page(None)
        return

    st.title(WELCOME_MESSAGE)
    login_page()


async def _render_page(cookie_me: Dict | None):
    """Dispatch to the correct page based on selected_page."""
    current_page = st.session_state["selected_page"]

    if current_page == "chat":
        if "messages" in st.session_state:
            st.session_state.pop("messages", None)

        await chat(cookie_me)
        return

    if current_page == "ai_models":
        llms_management(cookie_me)
        return

    if current_page == "agentic_workflows":
        agentic_workflows_management(cookie_me)
        return

    if current_page == "auth_handlers":
        auth_handlers_management(cookie_me)
        return

    if current_page == "chunkers":
        chunkers_management(cookie_me)
        return

    if current_page == "context_retrievers":
        context_retrievers_management(cookie_me)
        return

    if current_page == "embedders":
        embedders_management(cookie_me)
        return

    if current_page == "ingestion":
        ingestion_management(cookie_me)
        return

    if current_page == "file_handlers":
        file_managers_management(cookie_me)
        return

    if current_page == "rag":
        rabbit_hole_management(cookie_me)
        return

    if current_page == "plugins":
        plugins_management(cookie_me)
        return

    if current_page == "users":
        users_management(cookie_me)
        return

    if current_page == "vector_databases":
        vector_databases_management(cookie_me)
        return

    if current_page == "memory":
        memory_management(cookie_me)
        return

    if current_page == "system":
        utilities_management(cookie_me)
        return

    welcome(cookie_me)


# ----- Main application -----
if __name__ == "__main__":
    st.set_page_config(
        page_title="Grinning Cat Admin UI",
        layout="wide",
        page_icon="🐱",
        initial_sidebar_state="expanded",
        menu_items={
            "Get Help": "mailto:matteo.cacciola@gmail.com",
            "Report a bug": "mailto:matteo.cacciola@gmail.com",
            "About": "Grinning Cat Admin UI - A Streamlit application for managing the Grinning Cat backend.",
        }
    )

    load_dotenv()
    asyncio.run(_main())
