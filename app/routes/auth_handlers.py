import json
from typing import Dict
import streamlit as st
from grinning_cat_python_sdk import GrinningCatClient

from app.utils import (
    get_factory_settings,
    run_toast,
    show_overlay_spinner,
    build_client_configuration,
    render_json_form,
    has_access,
)


def _list_auth_handlers(agent_id: str, cookie_me: Dict):
    run_toast()

    if not has_access("AUTH_HANDLER", "READ", cookie_me):
        st.error("You do not have access to view authentication handlers for this agent.")
        return

    client = GrinningCatClient(build_client_configuration())
    st.header("Authentication Handlers")

    try:
        settings = client.auth_handler.get_auth_handlers_settings(agent_id)

        st.write("### Available Authentication Handlers")
        if not settings.settings:
            st.info("No authentication handler found")
            return

        for handler in settings.settings:
            col1, col2, col3 = st.columns([0.8, 0.05, 0.15])
            is_selected = handler.name == settings.selected_configuration
            with col1:
                with st.expander(handler.name):
                    st.json(get_factory_settings(handler, is_selected)[0])

            with col2:
                if is_selected:
                    st.write('<div class="picked">✅</div>', unsafe_allow_html=True)

            with col3:
                if has_access("AUTH_HANDLER", "WRITE", cookie_me):
                    if st.button("Edit" if is_selected else "Select", key=f"edit_{handler.name}"):
                        _edit_auth_handler(agent_id, handler.name, is_selected, cookie_me)
                else:
                    st.button(
                        "Edit",
                        key=f"edit_{handler.name}",
                        disabled=True,
                        help="You do not have permission to edit authentication handlers."
                    )
    except Exception as e:
        st.error(f"Error fetching authentication handlers: {e}")


@st.dialog(title="Edit Authentication Handler", width="large")
def _edit_auth_handler(agent_id: str, handler_name: str, is_selected: bool, cookie_me: Dict):
    if not has_access("AUTH_HANDLER", "WRITE", cookie_me):
        st.error("You do not have access to edit authentication handlers for this agent.")
        return

    client = GrinningCatClient(build_client_configuration())

    st.subheader(f"Editing: **{handler_name}**")
    try:
        handler_settings, handler_types = get_factory_settings(
            client.auth_handler.get_auth_handler_settings(handler_name, agent_id),
            is_selected=is_selected
        )
        with st.form("edit_auth_handler_form", clear_on_submit=True, enter_to_submit=True):
            edited_settings = {}
            if handler_settings:
                # Render the form
                edited_settings = render_json_form(handler_settings, handler_types)

            if not edited_settings:
                st.text("No settings available to edit. Click 'Save' to confirm or 'Back to list' to cancel.")

            if st.form_submit_button("Save"):
                try:
                    spinner_container = show_overlay_spinner("Saving settings...")
                    client.auth_handler.put_auth_handler_settings(
                        auth_handler=handler_name,
                        agent_id=agent_id,
                        values=edited_settings,
                    )
                    st.session_state["toast"] = {
                        "message": f"Handler {handler_name} updated successfully!", "icon": "✅"
                    }
                except json.JSONDecodeError:
                    st.session_state["toast"] = {"message": "Invalid JSON format", "icon": "❌"}
                except Exception as e:
                    st.session_state["toast"] = {"message": f"Error updating handler: {e}", "icon": "❌"}
                finally:
                    spinner_container.empty()

                st.rerun()
    except Exception as e:
        st.error(f"Error fetching handler settings: {e}")

    st.divider()
    if st.button("Back to list"):
        st.rerun()


def auth_handlers_management(cookie_me: Dict):
    st.title("Authentication Handlers Management Dashboard")

    if "agent_id" in st.session_state:
        _list_auth_handlers(st.session_state["agent_id"], cookie_me)
