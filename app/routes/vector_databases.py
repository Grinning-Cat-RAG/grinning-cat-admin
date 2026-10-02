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


def _list_vector_databases(agent_id: str, cookie_me: Dict):
    run_toast()

    if not has_access("VECTOR_DATABASE", "READ", cookie_me):
        st.error("You do not have access to view vector databases for this agent.")
        return

    client = GrinningCatClient(build_client_configuration())
    st.header("Vector Databases")

    try:
        settings = client.vector_database.get_vector_databases_settings(agent_id)

        st.write("### Available Vector Databases")
        if not settings.settings:
            st.info("No vector database found")
            return

        for vector_database in settings.settings:
            col1, col2, col3 = st.columns([0.8, 0.05, 0.15])
            is_selected = vector_database.name == settings.selected_configuration
            with col1:
                with st.expander(vector_database.name):
                    st.json(get_factory_settings(vector_database, is_selected)[0])

            with col2:
                if is_selected:
                    st.write('<div class="picked">✅</div>', unsafe_allow_html=True)

            with col3:
                if has_access("VECTOR_DATABASE", "WRITE", cookie_me):
                    if st.button("Edit" if is_selected else "Select", key=f"edit_{vector_database.name}"):
                        _edit_vector_database(agent_id, vector_database.name, is_selected, cookie_me)
                else:
                    st.button(
                        "Edit",
                        key=f"edit_{vector_database.name}",
                        disabled=True,
                        help="You do not have permission to edit vector databases.",
                    )
    except Exception as e:
        st.error(f"Error fetching vector databases: {e}")


@st.dialog(title="Edit Vector Database", width="large")
def _edit_vector_database(agent_id: str, vector_database_name: str, is_selected: bool, cookie_me: Dict):
    if not has_access("VECTOR_DATABASE", "WRITE", cookie_me):
        st.error("You do not have access to edit vector databases for this agent.")
        return

    client = GrinningCatClient(build_client_configuration())

    st.subheader(f"Editing: **{vector_database_name}**")
    try:
        vector_db_settings, vector_db_types = get_factory_settings(
            client.vector_database.get_vector_database_settings(vector_database_name, agent_id),
            is_selected=is_selected
        )
        with st.form("edit_vector_database_form", clear_on_submit=True, enter_to_submit=True):
            edited_settings = {}
            if vector_db_settings:
                # Render the form
                edited_settings = render_json_form(vector_db_settings, vector_db_types)

            if not edited_settings:
                st.text("No settings available to edit. Click 'Save' to confirm or 'Back to list' to cancel.")

            if st.form_submit_button("Save"):
                try:
                    spinner_container = show_overlay_spinner("Saving settings...")
                    client.vector_database.put_vector_database_settings(
                        vector_database=vector_database_name,
                        agent_id=agent_id,
                        values=edited_settings,
                    )
                    st.session_state["toast"] = {
                        "message": f"Vector database {vector_database_name} updated successfully!", "icon": "✅"
                    }
                except json.JSONDecodeError:
                    st.session_state["toast"] = {"message": "Invalid JSON format", "icon": "❌"}
                except Exception as e:
                    st.session_state["toast"] = {"message": f"Error updating vector database: {e}", "icon": "❌"}
                finally:
                    spinner_container.empty()

                st.rerun()
    except Exception as e:
        st.error(f"Error fetching vector database settings: {e}")

    st.divider()
    if st.button("Back to list"):
        st.rerun()


def vector_databases_management(cookie_me: Dict):
    st.title("Vector Databases Management Dashboard")

    if "agent_id" in st.session_state:
        _list_vector_databases(st.session_state["agent_id"], cookie_me)
