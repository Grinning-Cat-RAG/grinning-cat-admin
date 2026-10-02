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


def _list_embedders(cookie_me: Dict):
    run_toast()

    if not has_access("EMBEDDER", "READ", cookie_me, only_admin=True):
        st.error("You do not have access to view embedders.")
        return

    client = GrinningCatClient(build_client_configuration())
    st.header("Embedders")

    try:
        settings = client.embedder.get_embedders_settings()

        st.write("### Available Embedders")
        if not settings.settings:
            st.info("No embedder found")
            return

        for embedder in settings.settings:
            col1, col2, col3 = st.columns([0.8, 0.05, 0.15])
            is_selected = embedder.name == settings.selected_configuration
            with col1:
                with st.expander(embedder.name):
                    st.json(get_factory_settings(embedder, is_selected)[0])

            with col2:
                if is_selected:
                    st.write('<div class="picked">✅</div>', unsafe_allow_html=True)

            with col3:
                if has_access("EMBEDDER", "WRITE", cookie_me, only_admin=True):
                    if st.button("Edit" if is_selected else "Select", key=f"edit_{embedder.name}"):
                        _edit_embedder(embedder.name, is_selected, cookie_me)
                else:
                    st.button(
                        "Edit",
                        key=f"edit_{embedder.name}",
                        disabled=True,
                        help="You do not have permission to edit embedders.",
                    )
    except Exception as e:
        st.error(f"Error fetching embedders: {e}")


@st.dialog(title="Edit Embedder", width="large")
def _edit_embedder(embedder_name: str, is_selected: bool, cookie_me: Dict):
    if not has_access("EMBEDDER", "WRITE", cookie_me, only_admin=True):
        st.error("You do not have access to edit embedders.")
        return

    client = GrinningCatClient(build_client_configuration())

    st.subheader(f"Editing: **{embedder_name}**")
    try:
        embedder_settings, embedder_types = get_factory_settings(
            client.embedder.get_embedder_settings(embedder_name),
            is_selected=is_selected,
        )
        with st.form("edit_embedder_form", clear_on_submit=True, enter_to_submit=True):
            edited_settings = {}
            if embedder_settings:
                # Render the form
                edited_settings = render_json_form(embedder_settings, embedder_types)

            if not edited_settings:
                st.text("No settings available to edit. Click 'Save' to confirm or 'Back to list' to cancel.")

            if st.form_submit_button("Save"):
                try:
                    spinner_container = show_overlay_spinner("Saving settings...")
                    client.embedder.put_embedder_settings(
                        embedder=embedder_name,
                        values=edited_settings,
                    )
                    st.session_state["toast"] = {
                        "message": f"Embedder {embedder_name} updated successfully!", "icon": "✅"
                    }
                except json.JSONDecodeError:
                    st.session_state["toast"] = {"message": "Invalid JSON format", "icon": "❌"}
                except Exception as e:
                    st.session_state["toast"] = {"message": f"Error updating embedder: {e}", "icon": "❌"}
                finally:
                    spinner_container.empty()

                st.rerun()
    except Exception as e:
        st.error(f"Error fetching embedder settings: {e}")

    st.divider()
    if st.button("Back to list"):
        st.rerun()


def embedders_management(cookie_me: Dict):
    st.title("Embedders Management Dashboard")

    _list_embedders(cookie_me)
