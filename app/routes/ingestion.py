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


def _list_ingestion(cookie_me: Dict):
    run_toast()

    if not has_access("SYSTEM", "READ", cookie_me, only_admin=True):
        st.error("You do not have access to view ingestion engines.")
        return

    client = GrinningCatClient(build_client_configuration())
    st.header("Ingestion Engine")

    try:
        settings = client.ingestion.get_ingestions_settings()

        st.write("### Available Ingestion Configurations")
        if not settings.settings:
            st.info("No ingestion configuration found")
            return

        for ingestion in settings.settings:
            col1, col2, col3 = st.columns([0.8, 0.05, 0.15])
            is_selected = ingestion.name == settings.selected_configuration
            with col1:
                with st.expander(ingestion.name):
                    st.json(get_factory_settings(ingestion, is_selected)[0])

            with col2:
                if is_selected:
                    st.write('<div class="picked">✅</div>', unsafe_allow_html=True)

            with col3:
                if has_access("SYSTEM", "WRITE", cookie_me, only_admin=True):
                    if st.button("Edit" if is_selected else "Select", key=f"edit_{ingestion.name}"):
                        _edit_ingestion(ingestion.name, is_selected, cookie_me)
                else:
                    st.button(
                        "Edit",
                        key=f"edit_{ingestion.name}",
                        disabled=True,
                        help="You do not have permission to edit ingestion engines.",
                    )
    except Exception as e:
        st.error(f"Error fetching ingestion engines: {e}")


@st.dialog(title="Edit Ingestion Engine", width="large")
def _edit_ingestion(ingestion_name: str, is_selected: bool, cookie_me: Dict):
    if not has_access("SYSTEM", "WRITE", cookie_me, only_admin=True):
        st.error("You do not have access to edit ingestion engines.")
        return

    client = GrinningCatClient(build_client_configuration())

    st.subheader(f"Editing: **{ingestion_name}**")
    try:
        ingestion_settings, ingestion_types = get_factory_settings(
            client.ingestion.get_ingestion_settings(ingestion_name),
            is_selected=is_selected,
        )
        with st.form("edit_ingestion_form", clear_on_submit=True, enter_to_submit=True):
            edited_settings = {}
            if ingestion_settings:
                # Render the form
                edited_settings = render_json_form(ingestion_settings, ingestion_types)

            if not edited_settings:
                st.text("No settings available to edit. Click 'Save' to confirm or 'Back to list' to cancel.")

            if st.form_submit_button("Save"):
                try:
                    spinner_container = show_overlay_spinner("Saving settings...")
                    client.ingestion.put_ingestion_settings(
                        ingestion=ingestion_name,
                        values=edited_settings,
                    )
                    st.session_state["toast"] = {
                        "message": f"Ingestion {ingestion_name} updated and selected successfully!",
                        "icon": "✅",
                    }
                except json.JSONDecodeError:
                    st.session_state["toast"] = {"message": "Invalid JSON format", "icon": "❌"}
                except Exception as e:
                    st.session_state["toast"] = {"message": f"Error updating ingestion: {e}", "icon": "❌"}
                finally:
                    spinner_container.empty()

                st.rerun()
    except Exception as e:
        st.error(f"Error fetching ingestion settings: {e}")

    st.divider()
    if st.button("Back to list"):
        st.rerun()


def ingestion_management(cookie_me: Dict):
    st.title("Ingestion Engine Management Dashboard")

    _list_ingestion(cookie_me)
