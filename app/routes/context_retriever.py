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


def _list_context_retrievers(agent_id: str, cookie_me: Dict):
    run_toast()

    if not has_access("CONTEXT_RETRIEVER", "READ", cookie_me):
        st.error("You do not have access to view context retrievers for this agent.")
        return

    client = GrinningCatClient(build_client_configuration())
    st.header("Context Retrievers")

    try:
        settings = client.context_retriever.get_context_retrievers_settings(agent_id)

        st.write("### Available Context Retrievers")
        if not settings.settings:
            st.info("No context retriever found")
            return

        for context_retriever in settings.settings:
            col1, col2, col3 = st.columns([0.8, 0.05, 0.15])
            is_selected = context_retriever.name == settings.selected_configuration
            with col1:
                with st.expander(context_retriever.name):
                    st.json(get_factory_settings(context_retriever, is_selected)[0])

            with col2:
                if is_selected:
                    st.write('<div class="picked">✅</div>', unsafe_allow_html=True)

            with col3:
                if has_access("CONTEXT_RETRIEVER", "WRITE", cookie_me):
                    if st.button("Edit" if is_selected else "Select", key=f"edit_{context_retriever.name}"):
                        _edit_context_retriever(agent_id, context_retriever.name, is_selected, cookie_me)
                else:
                    st.button(
                        "Edit",
                        key=f"edit_{context_retriever.name}",
                        disabled=True,
                        help="You do not have permission to edit context retrievers.",
                    )
    except Exception as e:
        st.error(f"Error fetching context retrievers: {e}")


@st.dialog(title="Edit Context Retriever", width="large")
def _edit_context_retriever(agent_id: str, context_retriever_name: str, is_selected: bool, cookie_me: Dict):
    if not has_access("CONTEXT_RETRIEVER", "WRITE", cookie_me):
        st.error("You do not have access to edit context retrievers for this agent.")
        return

    client = GrinningCatClient(build_client_configuration())

    st.subheader(f"Editing: **{context_retriever_name}**")
    try:
        context_retriever_settings, context_retriever_types = get_factory_settings(
            client.context_retriever.get_context_retriever_settings(context_retriever_name, agent_id),
            is_selected=is_selected
        )
        with st.form("edit_context_retriever_form", clear_on_submit=True, enter_to_submit=True):
            edited_settings = {}
            if context_retriever_settings:
                # Render the form
                edited_settings = render_json_form(context_retriever_settings, context_retriever_types)

            if not edited_settings:
                st.text("No settings available to edit. Click 'Save' to confirm or 'Back to list' to cancel.")

            if st.form_submit_button("Save"):
                try:
                    spinner_container = show_overlay_spinner("Saving settings...")
                    client.context_retriever.put_context_retriever_settings(
                        context_retriever=context_retriever_name,
                        agent_id=agent_id,
                        values=edited_settings,
                    )
                    st.session_state["toast"] = {
                        "message": f"Context retriever {context_retriever_name} updated successfully!", "icon": "✅"
                    }
                except json.JSONDecodeError:
                    st.session_state["toast"] = {"message": "Invalid JSON format", "icon": "❌"}
                except Exception as e:
                    st.session_state["toast"] = {"message": f"Error updating context retriever: {e}", "icon": "❌"}
                finally:
                    spinner_container.empty()

                st.rerun()
    except Exception as e:
        st.error(f"Error fetching context retriever settings: {e}")

    st.divider()
    if st.button("Back to list"):
        st.rerun()


def context_retrievers_management(cookie_me: Dict):
    st.title("Context Retrievers Management Dashboard")

    if "agent_id" in st.session_state:
        _list_context_retrievers(st.session_state["agent_id"], cookie_me)
