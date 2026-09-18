import json
import time
from typing import Dict
import streamlit as st
from grinning_cat_python_sdk import GrinningCatClient
from grinning_cat_python_sdk.models.api.nested.plugins import PluginSettingsOutput
from grinning_cat_python_sdk.utils import deserialize

from app.utils import (
    show_overlay_spinner,
    build_client_configuration,
    has_access,
    is_management_active,
    run_toast,
    build_me_data,
    get_settings,
    render_json_form,
)
from app.constants import DEFAULT_SYSTEM_KEY


def _factory_reset(cookie_me: Dict | None):
    run_toast()

    if not has_access("SYSTEM", "DELETE", cookie_me, only_admin=True):
        st.error("You do not have permission to perform a factory reset.")
        return

    client = GrinningCatClient(build_client_configuration())
    st.header("Factory Reset")

    st.warning("""
    ⚠️ **Danger Zone** ⚠️  
    This will completely reset the entire application to its initial state.  
    All settings, memories, and plugins will be deleted.  
    This action cannot be undone!
    """)

    if not st.button("Perform Factory Reset", type="primary"):
        return

    spinner_container = show_overlay_spinner("Performing factory reset...")
    try:
        result = client.utils.post_factory_reset()

        if result.deleted_settings and result.deleted_plugin_folders and result.deleted_memories:
            st.toast("Factory reset completed successfully!", icon="✅")
        else:
            st.toast("Factory reset partially failed", icon="❌")
        st.json({
            "Settings deleted": result.deleted_settings,
            "Plugin folders deleted": result.deleted_plugin_folders,
            "Memories deleted": result.deleted_memories
        })
    except Exception as e:
        st.toast(f"Error performing factory reset: {e}", icon="❌")
    finally:
        spinner_container.empty()


def _list_agents(cookie_me: Dict | None):
    def pop_state_keys():
        for key in ["agent_to_clone", "agent_to_reset", "agent_to_destroy", "new_agent_id_input"]:
            if key in st.session_state:
                st.session_state.pop(key, None)

    run_toast()

    if not has_access("CHESHIRE_CAT", "READ", cookie_me, only_admin=True):
        st.error("You do not have permission to view agents.")
        return

    client = GrinningCatClient(build_client_configuration())
    st.header("Agent Management")

    try:
        agents = client.utils.get_agents()

        if not agents:
            st.info("No agents found")
            return

        st.write("### Existing Agents")
        for agent in agents:
            col0, col1, col2, col3, col4 = st.columns(5)
            col0.write(f"**Agent ID**: `{agent.agent_id}`")

            with col1:
                if has_access("CHESHIRE_CAT", "WRITE", cookie_me, only_admin=True):
                    if st.button("Update", key=f"update_{agent.agent_id}"):
                        _update_agent(agent.agent_id, agent.metadata, cookie_me)
                else:
                    st.button(
                        "Update",
                        key=f"update_{agent.agent_id}",
                        help="You do not have permission to update agents",
                        disabled=True
                    )

            with col2:
                if has_access("CHESHIRE_CAT", "WRITE", cookie_me, only_admin=True):
                    if st.button(
                            "Clone",
                            key=f"clone_{agent.agent_id}",
                            help="Clone this agent and all associated data"
                    ):
                        pop_state_keys()
                        st.session_state["agent_to_clone"] = agent.agent_id
                else:
                    st.button(
                        "Clone",
                        key=f"clone_{agent.agent_id}",
                        help="You do not have permission to clone agents",
                        disabled=True
                    )

            with col3:
                if has_access("CHESHIRE_CAT", "WRITE", cookie_me, only_admin=True):
                    if st.button(
                            "Reset",
                            key=f"reset_{agent.agent_id}",
                            help="Reset this agent settings and memories"
                    ):
                        pop_state_keys()
                        st.session_state["agent_to_reset"] = agent.agent_id
                else:
                    st.button(
                        "Reset",
                        key=f"reset_{agent.agent_id}",
                        help="You do not have permission to reset agents",
                        disabled=True
                    )

            with col4:
                if has_access("CHESHIRE_CAT", "DELETE", cookie_me, only_admin=True):
                    if st.button(
                            "Destroy",
                            key=f"destroy_{agent.agent_id}",
                            help="Permanently destroy this agent and all associated data"
                    ):
                        pop_state_keys()
                        st.session_state["agent_to_destroy"] = agent.agent_id
                else:
                    st.button(
                        "Destroy",
                        key=f"destroy_{agent.agent_id}",
                        help="You do not have permission to destroy agents",
                        disabled=True
                    )

            st.divider()

        # Clone confirmation
        if (
                has_access("CHESHIRE_CAT", "WRITE", cookie_me, only_admin=True)
                and (agent := st.session_state.get("agent_to_clone"))
        ):
            st.warning(f"⚠️ Are you sure you want to clone agent `{agent}`?")

            new_agent_id = st.text_input("New Agent ID", value=f"{agent}_clone", key="new_agent_id_input")

            col1, col2 = st.columns(2)
            with col1:
                if st.button("Yes, Clone Agent", type="primary"):
                    try:
                        with st.spinner(f"Cloning agent {agent}..."):
                            result = client.utils.post_agent_clone(agent_id=agent, new_agent_id=new_agent_id)
                        if result.cloned:
                            st.toast(f"Agent {agent} cloned successfully!", icon="✅")
                            st.session_state.pop("agent_to_clone", None)
                            if cookie_me:
                                build_me_data()
                            time.sleep(1)  # Wait for a moment before rerunning
                            st.rerun()
                        else:
                            st.toast(f"Failed to clone agent {agent}", icon="❌")
                    except Exception as e:
                        st.toast(f"Error cloning agent: {e}", icon="❌")

            with col2:
                if st.button("Cancel"):
                    st.session_state.pop("agent_to_clone", None)
                    st.rerun()

        # Reset confirmation
        if (
                has_access("CHESHIRE_CAT", "WRITE", cookie_me, only_admin=True)
                and (agent := st.session_state.get("agent_to_reset"))
        ):
            st.warning(f"⚠️ Are you sure you want to permanently reset agent `{agent}`?")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Yes, Reset Agent", type="primary"):
                    try:
                        with st.spinner(f"Resetting agent {agent}..."):
                            result = client.utils.post_agent_reset(agent_id=agent)
                        if result.deleted_settings:
                            st.toast(f"Agent {agent} reset successfully!", icon="✅")
                            st.session_state.pop("agent_to_reset", None)
                            time.sleep(1)  # Wait for a moment before rerunning
                            st.rerun()
                        else:
                            st.toast(f"Failed to reset agent {agent}", icon="❌")
                    except Exception as e:
                        st.toast(f"Error resetting agent: {e}", icon="❌")

            with col2:
                if st.button("Cancel"):
                    st.session_state.pop("agent_to_reset", None)
                    st.rerun()

        # Destroy confirmation
        if (
                has_access("CHESHIRE_CAT", "DELETE", cookie_me, only_admin=True)
                and (agent := st.session_state.get("agent_to_destroy"))
        ):
            st.warning(f"⚠️ Are you sure you want to permanently destroy agent `{agent}`?")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Yes, Destroy Agent", type="primary"):
                    try:
                        with st.spinner(f"Destroying agent {agent}..."):
                            result = client.utils.post_agent_destroy(agent_id=agent)
                        if result.deleted_settings and result.deleted_memories:
                            st.toast(f"Agent {agent} destroyed successfully!", icon="✅")
                            st.session_state.pop("agent_to_destroy", None)
                            if cookie_me:
                                build_me_data()
                            time.sleep(1)  # Wait for a moment before rerunning
                            st.rerun()
                        else:
                            st.toast(f"Failed to completely destroy agent {agent}", icon="❌")
                    except Exception as e:
                        st.toast(f"Error destroying agent: {e}", icon="❌")
            with col2:
                if st.button("Cancel"):
                    st.session_state.pop("agent_to_destroy", None)
                    st.rerun()

    except Exception as e:
        st.error(f"Error fetching agents: {e}")


def _create_agent(cookie_me: Dict | None):
    run_toast()

    if not has_access("CHESHIRE_CAT", "WRITE", cookie_me, only_admin=True):
        st.error("You do not have permission to create agents.")
        return

    client = GrinningCatClient(build_client_configuration())
    st.header("Create New Agent")

    with st.form("create_agent_form", clear_on_submit=True, enter_to_submit=False):
        agent_id = st.text_input("Agent ID", help="Unique identifier for the new agent")
        metadata = st.text_area(
            "Agent Metadata (JSON-like string)",
            help="Optional metadata for the agent in JSON format",
            value="{}",
        )

        if not st.form_submit_button("Create Agent"):
            return

        if not agent_id:
            st.error("Agent ID is required")
            return

        try:
            metadata = json.loads(metadata) if metadata else None
        except json.JSONDecodeError:
            st.warning("Metadata must be a valid JSON string. Ignoring metadata.")
            metadata = None

        try:
            spinner_container = show_overlay_spinner(f"Creating agent {agent_id}...")
            result = client.utils.post_agent_create(agent_id=agent_id, metadata=metadata)
            if result.created:
                st.toast(f"Agent {agent_id} created successfully!", icon="✅")
                if cookie_me:
                    build_me_data()
                time.sleep(1)  # Wait for a moment before rerunning
                st.rerun()
            else:
                st.toast(f"Failed to create agent {agent_id}", icon="❌")
        except Exception as e:
            st.toast(f"Error creating agent: {e}", icon="❌")
        finally:
            spinner_container.empty()


@st.dialog(title="Update Details", width="large")
def _update_agent(agent_id: str, metadata: Dict, cookie_me: Dict | None):
    if not has_access("CHESHIRE_CAT", "WRITE", cookie_me):
        st.error("You do not have permission to update agents.")
        return

    client = GrinningCatClient(build_client_configuration())
    st.header(f"Update Agent ID: {agent_id}")

    with st.form("update_agent_form", enter_to_submit=False):
        new_metadata = st.text_area(
            "Agent Metadata",
            value=json.dumps(metadata, indent=2),
            help="Optional metadata for the agent in JSON format",
        )

        if not st.form_submit_button("Update Agent"):
            return

        try:
            new_metadata = json.loads(new_metadata) if new_metadata else None
        except json.JSONDecodeError:
            st.session_state["toast"] = {
                "message": "Metadata must be a valid JSON string. Ignoring metadata.", "icon": "⚠️",
            }
            st.rerun()

        try:
            spinner_container = show_overlay_spinner(f"Updating metadata for Agent {agent_id}...")

            result = client.utils.put_agent(agent_id=agent_id,  metadata=new_metadata or {})
            if result.updated:
                st.session_state["toast"] = {"message": f"Agent {agent_id} updated successfully!", "icon": "✅"}
            else:
                st.session_state["toast"] = {"message": f"Failed to update agent `{agent_id}`", "icon": "❌"}
        except Exception as e:
            st.session_state["toast"] = {"message": f"Error updating agent `{agent_id}`: {e}", "icon": "❌"}
        finally:
            spinner_container.empty()
            time.sleep(1)
            st.rerun()


def _management_mode(cookie_me: Dict | None):
    run_toast()

    if not has_access("SYSTEM", "WRITE", cookie_me, only_admin=True):
        st.error("You do not have access to Management mode.")
        return

    client = GrinningCatClient(build_client_configuration())

    st.header("Management Mode")
    st.caption(
        "Configure the global management/notice message of the instance. "
        "While management mode is active every route answers 404, for every user: "
        "this page keeps working because it reads and writes through the plugin's "
        "own routes, which are the only ones left open."
    )

    try:
        # The plugin owns its read route too, and it is the only one the core
        # keeps answering while management mode is on. get_custom() hands back
        # the parsed JSON, so it has to be deserialized before get_settings(),
        # which works on a PluginSettingsOutput (.value / .scheme).
        plugin_settings, types = get_settings(
            deserialize(
                client.custom.get_custom("/mgmt_message/settings", DEFAULT_SYSTEM_KEY),
                PluginSettingsOutput,
            ),
            is_selected=True,
        )
    except Exception as e:
        st.error(f"Error fetching management message settings: {e}")
        return

    with st.form("management_mode_form", clear_on_submit=False, enter_to_submit=False):
        edited_settings = {}
        if plugin_settings:
            edited_settings = render_json_form(plugin_settings, types)

        if not edited_settings:
            st.text("No settings available. Click 'Save' to apply defaults.")

        if st.form_submit_button("Save"):
            try:
                spinner_container = show_overlay_spinner("Saving management message settings...")
                # The plugin owns its write route (PUT /mgmt_message/settings,
                # SYSTEM WRITE) because the agent-level plugin settings routes
                # refuse it: mgmt_message is a system plugin. It persists on
                # the system agent's plugin key, system:plugins:mgmt_message,
                # which is what the read above serves.
                client.custom.put_custom(
                    "/mgmt_message/settings",
                    DEFAULT_SYSTEM_KEY,
                    payload=edited_settings,
                )
                st.session_state["toast"] = {
                    "message": "Management message settings updated successfully!", "icon": "✅"
                }
            except json.JSONDecodeError:
                st.session_state["toast"] = {"message": "Invalid JSON format", "icon": "❌"}
            except Exception as e:
                st.session_state["toast"] = {"message": f"Error updating management message settings: {e}", "icon": "❌"}
            finally:
                spinner_container.empty()
            st.rerun()


def utilities_management(cookie_me: Dict | None):
    st.title("System Management Dashboard")

    # Navigation
    menu_options = {
        "(Select a menu)": {
            "page": None,
            "permission": True,
        },
        "Agent Management": {
            "page": "agent_management",
            "permission": has_access("CHESHIRE_CAT", "READ", cookie_me, only_admin=True),
        },
        "Create Agent": {
            "page": "create_agent",
            "permission": has_access("CHESHIRE_CAT", "WRITE", cookie_me, only_admin=True),
        },
        "Management mode": {
            "page": "management_mode",
            "permission": has_access("SYSTEM", "WRITE", cookie_me, only_admin=True),
        },
        "Factory Reset": {
            "page": "factory_reset",
            "permission": has_access("SYSTEM", "DELETE", cookie_me, only_admin=True),
        },
    }
    if is_management_active(st.session_state.get("management")):
        # Everything else in here talks to routes the core is answering 404 on:
        # leave only the entry that can switch management mode back off, and
        # drop the placeholder so the form shows straight away.
        menu_options = {"Management mode": menu_options["Management mode"]}

    if not any(option["permission"] for option in menu_options.values() if option["page"]):
        st.error("You do not have access to any utilities.")
        return

    choices = {
        name: details["page"]
        for name, details in menu_options.items()
        if details["permission"]
    }

    choice = st.selectbox("Menu", choices)

    if menu_options[choice]["page"] == "agent_management":
        _list_agents(cookie_me)
        return

    if menu_options[choice]["page"] == "create_agent":
        _create_agent(cookie_me)
        return

    if menu_options[choice]["page"] == "management_mode":
        _management_mode(cookie_me)
        return

    if menu_options[choice]["page"] == "factory_reset":
        _factory_reset(cookie_me)
