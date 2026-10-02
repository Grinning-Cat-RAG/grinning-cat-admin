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


def _list_chunkers(agent_id: str, cookie_me: Dict):
    run_toast()

    if not has_access("CHUNKER", "READ", cookie_me):
        st.error("You do not have access to view chunkers for this agent.")
        return

    client = GrinningCatClient(build_client_configuration())
    st.header("Chunkers")

    try:
        settings = client.chunker.get_chunkers_settings(agent_id)

        st.write("### Available Chunkers")
        if not settings.settings:
            st.info("No chunker found")
            return

        for chunker in settings.settings:
            col1, col2, col3 = st.columns([0.8, 0.05, 0.15])
            is_selected = chunker.name == settings.selected_configuration
            with col1:
                with st.expander(chunker.name):
                    st.json(get_factory_settings(chunker, is_selected)[0])

            with col2:
                if is_selected:
                    st.write('<div class="picked">✅</div>', unsafe_allow_html=True)

            with col3:
                if has_access("CHUNKER", "WRITE", cookie_me):
                    if st.button("Edit" if is_selected else "Select", key=f"edit_{chunker.name}"):
                        _edit_chunker(agent_id, chunker.name, is_selected, cookie_me)
                else:
                    st.button(
                        "Edit",
                        key=f"edit_{chunker.name}",
                        disabled=True,
                        help="You do not have permission to edit chunkers.",
                    )
    except Exception as e:
        st.error(f"Error fetching chunkers: {e}")


@st.dialog(title="Edit Chunker", width="large")
def _edit_chunker(agent_id: str, chunker_name: str, is_selected: bool, cookie_me: Dict):
    if not has_access("CHUNKER", "WRITE", cookie_me):
        st.error("You do not have access to edit chunkers for this agent.")
        return

    client = GrinningCatClient(build_client_configuration())

    st.subheader(f"Editing: **{chunker_name}**")
    try:
        chunker_settings, chunker_types = get_factory_settings(
            client.chunker.get_chunker_settings(chunker_name, agent_id),
            is_selected=is_selected
        )
        with st.form("edit_chunker_form", clear_on_submit=True, enter_to_submit=True):
            edited_settings = {}
            if chunker_settings:
                # Render the form
                edited_settings = render_json_form(chunker_settings, chunker_types)

            if not edited_settings:
                st.text("No settings available to edit. Click 'Save' to confirm or 'Back to list' to cancel.")

            if st.form_submit_button("Save"):
                try:
                    spinner_container = show_overlay_spinner("Saving settings...")
                    client.chunker.put_chunker_settings(
                        chunker=chunker_name,
                        agent_id=agent_id,
                        values=edited_settings,
                    )
                    st.session_state["toast"] = {
                        "message": f"Chunker {chunker_name} updated successfully!", "icon": "✅"
                    }
                except json.JSONDecodeError:
                    st.session_state["toast"] = {"message": "Invalid JSON format", "icon": "❌"}
                except Exception as e:
                    st.session_state["toast"] = {"message": f"Error updating chunker: {e}", "icon": "❌"}
                finally:
                    spinner_container.empty()

                st.rerun()
    except Exception as e:
        st.error(f"Error fetching chunker settings: {e}")

    st.divider()
    if st.button("Back to list"):
        st.rerun()


def chunkers_management(cookie_me: Dict):
    st.title("Chunkers Management Dashboard")

    if "agent_id" in st.session_state:
        _list_chunkers(st.session_state["agent_id"], cookie_me)
