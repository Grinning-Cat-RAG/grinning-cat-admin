import asyncio
import json
import time
from typing import Dict
import streamlit as st
from dotenv import load_dotenv
from grinning_cat_python_sdk import GrinningCatClient
from streamlit_js_eval import remove_local_storage

from app.constants import CHECK_INTERVAL, WELCOME_MESSAGE
from app.env import get_env
from app.utils import (
    get_with_expiry,
    build_agents_options_select,
    build_client_configuration,
    cache_cookie_me,
    clear_auth_cookies,
    has_access,
    is_system_agent_selected,
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


def _rehydrate_me_from_api() -> Dict | None:
    """Rebuild the full 'me' dict from /auth/me when localStorage cannot provide it."""
    if not st.session_state.get("token"):
        return None
    try:
        cache_cookie_me()
        return st.session_state.get("me")
    except Exception as e:
        print(f"Error rehydrating me from API: {e}")
        return None


def _get_cookie_me() -> Dict | None:
    """Return the current user's 'me' dict from session_state or localStorage."""
    # session_state is authoritative within a Streamlit session
    if "me" in st.session_state:
        return st.session_state["me"]

    # On a true browser page-refresh session_state is empty; try localStorage.
    # Use a session-scoped key so Streamlit does not memoize a stale "" across
    # different browser sessions.
    cookie_me = get_with_expiry(
        "me", component_key=f"getLS_me_{st.session_state['_session_key']}"
    )
    if not cookie_me:
        return None

    try:
        me = json.loads(cookie_me)
    except json.JSONDecodeError as e:
        # Entries written before the JS-escaping fix are corrupted beyond
        # repair: rebuild from the API like a lightweight entry.
        print(f"Error decoding 'me' localStorage entry: {e}")
        me = _rehydrate_me_from_api()
        if me is None:
            # Nothing rewrote the entry: drop it so the next refresh does not
            # hit the same undecodable value again.
            remove_local_storage("me")
        return me

    # Lightweight entry (only username/id/exp written after the login fix):
    # re-fetch full data from the API.
    if "agents" not in me:
        return _rehydrate_me_from_api()

    st.session_state["me"] = me
    return me


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

    st.session_state.clear()
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


def _render_sidebar_navigation(cookie_me: Dict | None):
    """Render the sidebar navigation menu"""
    st.session_state["selected_page"] = st.session_state.get("selected_page")
    if not st.session_state.get("token"):
        st.session_state["selected_page"] = None
        return

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
            st.session_state.clear()
            clear_auth_cookies()

            st.toast("Logged out successfully.", icon="🚪")
            time.sleep(1)  # Wait for a moment before rerunning

            st.rerun()


async def _main():
    """Main application function"""
    # Apply custom styling
    _apply_custom_css()

    _check_status()
    if st.session_state["status_connection"] != "Online":
        st.title(WELCOME_MESSAGE)
        st.error("Grinning Cat backend is offline. Please check your connection.")
        return

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
    st.title(WELCOME_MESSAGE)

    if not st.session_state.get("initial_auth_check_done"):
        # First render: fire the async localStorage read and show a loading screen.
        st.session_state["initial_auth_check_done"] = True
        get_with_expiry(
            "token", component_key=f"getLS_token_{st.session_state['_session_key']}"
        )
        loading_page()
        return

    # Second render: the iframe has responded; read the cached result.
    cookie_token = get_with_expiry(
        "token", component_key=f"getLS_token_{st.session_state['_session_key']}"
    )
    if cookie_token:
        st.session_state["token"] = cookie_token
        time.sleep(0.5)
        st.rerun()
        return

    # No (valid) token found → show login page.
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
