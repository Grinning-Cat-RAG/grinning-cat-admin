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


def _list_llms(agent_id: str, cookie_me: Dict):
    run_toast()

    if not has_access("LLM", "READ", cookie_me):
        st.error("You do not have access to view LLMs for this agent.")
        return

    client = GrinningCatClient(build_client_configuration())
    st.header("LLMs")

    try:
        settings = client.large_language_model.get_large_language_models_settings(agent_id)

        st.write("### Available LLMs")
        if not settings.settings:
            st.info("No LLM found")
            return

        for llm in settings.settings:
            col1, col2, col3 = st.columns([0.8, 0.05, 0.15])
            is_selected = llm.name == settings.selected_configuration
            with col1:
                with st.expander(llm.name):
                    st.json(get_factory_settings(llm, is_selected)[0])

            with col2:
                if is_selected:
                    st.write('<div class="picked">✅</div>', unsafe_allow_html=True)

            with col3:
                if has_access("LLM", "WRITE", cookie_me):
                    if st.button("Edit" if is_selected else "Select", key=f"edit_{llm.name}"):
                        _edit_llm(agent_id, llm.name, is_selected, cookie_me)
                else:
                    st.button(
                        "Edit",
                        key=f"edit_{llm.name}",
                        disabled=True,
                        help="You do not have permission to edit LLMs.",
                    )
    except Exception as e:
        st.error(f"Error fetching LLMs: {e}")


@st.dialog(title="Edit LLM", width="large")
def _edit_llm(agent_id: str, llm_name: str, is_selected: bool, cookie_me: Dict):
    if not has_access("LLM", "WRITE", cookie_me):
        st.error("You do not have access to edit LLMs for this agent.")
        return

    client = GrinningCatClient(build_client_configuration())

    st.subheader(f"Editing: **{llm_name}**")
    try:
        llm_settings, llm_types = get_factory_settings(
            client.large_language_model.get_large_language_model_settings(llm_name, agent_id),
            is_selected=is_selected
        )
        with st.form("edit_llm_form", clear_on_submit=True, enter_to_submit=True):
            edited_settings = {}
            if llm_settings:
                # Render the form
                edited_settings = render_json_form(llm_settings, llm_types)

            if not edited_settings:
                st.text("No settings available to edit. Click 'Save' to confirm or 'Back to list' to cancel.")

            if st.form_submit_button("Save"):
                try:
                    spinner_container = show_overlay_spinner("Saving settings...")
                    client.large_language_model.put_large_language_model_settings(
                        llm=llm_name,
                        agent_id=agent_id,
                        values=edited_settings,
                    )
                    st.session_state["toast"] = {
                        "message": f"LLM {llm_name} updated successfully!", "icon": "✅"
                    }
                except json.JSONDecodeError:
                    st.session_state["toast"] = {"message": "Invalid JSON format", "icon": "❌"}
                except Exception as e:
                    st.session_state["toast"] = {"message": f"Error updating LLM: {e}", "icon": "❌"}
                finally:
                    spinner_container.empty()

                st.rerun()
    except Exception as e:
        st.error(f"Error fetching LLM settings: {e}")

    st.divider()
    if st.button("Back to list"):
        st.rerun()


def llms_management(cookie_me: Dict):
    st.title("LLMs Management Dashboard")

    if "agent_id" in st.session_state:
        _list_llms(st.session_state["agent_id"], cookie_me)
