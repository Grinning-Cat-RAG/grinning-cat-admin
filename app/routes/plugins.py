import base64
import os
import tempfile
import json
import time
from typing import Dict, List
import streamlit as st
from grinning_cat_python_sdk import GrinningCatClient
from grinning_cat_python_sdk.models.api.plugins import PluginCollectionOutput

from app.constants import ASSETS_PATH, DEFAULT_SYSTEM_KEY
from app.utils import (
    get_settings,
    run_toast,
    show_overlay_spinner,
    build_client_configuration,
    render_json_form,
    has_access,
    build_agents_select,
)

# Pagination settings
ITEMS_PER_PAGE = 20


def _image_to_base64(img_path: str) -> str:
    """Convert an image file to a base64 string."""
    if not os.path.exists(img_path) or not os.path.isfile(img_path):
        raise FileNotFoundError(f"Image file not found: {img_path}")

    with open(img_path, "rb") as img_file:
        b64_string = base64.b64encode(img_file.read()).decode("utf-8")
    return b64_string


def _render_pagination_controls(section_key: str, current_page: int, total_pages: int):
    """Render pagination controls for a section."""
    col0, col1, col2, col3, col4 = st.columns([0.20, 0.08, 0.14, 0.08, 0.3])

    with col1:
        if st.button("← Previous", key=f"{section_key}_prev", disabled=current_page == 0):
            st.session_state[f"{section_key}_page"] -= 1
            st.rerun()

    with col2:
        page_input = st.number_input(
            "Go to page",
            min_value=1,
            max_value=total_pages,
            value=current_page + 1,
            step=1,
            key=f"{section_key}_page_input",
            label_visibility="collapsed"
        )

        st.markdown(
            f"<div style='text-align: center; margin-top: 0.5em;'>Page {current_page + 1} of {total_pages}</div>",
            unsafe_allow_html=True
        )

        if page_input != current_page + 1:
            st.session_state[f"{section_key}_page"] = page_input - 1
            st.rerun()

    with col3:
        if st.button("Next →", key=f"{section_key}_next", disabled=current_page >= total_pages - 1):
            st.session_state[f"{section_key}_page"] += 1
            st.rerun()


def _paginate_items(items: List, section_key: str, items_per_page: int):
    """Paginate a list of items and return the current page items."""
    # Initialize session state for pagination
    if f"{section_key}_page" not in st.session_state:
        st.session_state[f"{section_key}_page"] = 0

    total_items = len(items)
    total_pages = (total_items - 1) // items_per_page + 1 if total_items > 0 else 1
    current_page = st.session_state[f"{section_key}_page"]

    # Calculate pagination range
    start_idx = current_page * items_per_page
    end_idx = min(start_idx + items_per_page, total_items)
    paginated_items = items[start_idx:end_idx]

    return paginated_items, current_page, total_pages


def _render_installed_plugin_common_parts(col0, col1, p):
    """Render common parts of an installed plugin row."""
    with col0:
        img = (
            p.thumb
            if p.thumb
            else f"data:image/png;base64,{_image_to_base64(os.path.join(ASSETS_PATH, 'placeholder_plugin.png'))}"
        )
        st.markdown(
            f'<img src="{img}" alt="" style="width: 100%; margin-bottom: 2em;" />',
            unsafe_allow_html=True
        )
        # st.image(img, width="stretch")

    with col1:
        with st.expander(f"{p.name} (ID: {p.id})", icon="🔌"):
            st.json(p.model_dump())


def _render_installed_plugin_agents(p, untoggling_plugins_ids: List[str], cookie_me: Dict | None):
    """Render a single installed plugin row."""
    col0, col1, col2 = st.columns([0.05, 0.63, 0.32])

    _render_installed_plugin_common_parts(col0, col1, p)

    with col2:
        if not has_access("PLUGIN", "WRITE", cookie_me):
            return

        if st.button("Manage", key=f"manage_{p.id}"):
            manage_plugin(p.id, untoggling_plugins_ids)


def _render_installed_plugin_admins(
    p,
    client: GrinningCatClient,
    untoggling_plugins_ids: List[str],
    cookie_me: Dict | None,
    core_plugins_ids: List[str] | None = None,
):
    """Render a single installed plugin row."""
    col0, col1, col2, col3 = st.columns([0.05, 0.63, 0.1, 0.22])

    _render_installed_plugin_common_parts(col0, col1, p)

    with col2:
        if (
                has_access("SYSTEM", "READ", cookie_me, only_admin=True)
                and st.button("View Details", key=f"view_{p.id}")
        ):
            view_plugin_details(p.id)

    with col3:
        # Toggle / Untoggle or Uninstall button on a system level
        if has_access("SYSTEM", "DELETE", cookie_me, only_admin=True):
            if p.id in core_plugins_ids:
                if p.id not in untoggling_plugins_ids and st.button(
                        f"{'Untoggle' if p.local_info['active'] else 'Toggle'} Plugin",
                        key=f"toggle_{p.id}",
                        help=f"{'Untoggle' if p.local_info['active'] else 'Toggle'} this plugin. This is a core plugin and cannot be uninstalled.",
                ):
                    spinner_container = show_overlay_spinner("Toggling plugin...")
                    try:
                        client.admins.put_toggle_plugin(p.id)
                        st.toast(f"Plugin {p.id} toggled successfully!", icon="✅")
                        time.sleep(1)
                    except Exception as e:
                        st.error(f"Error toggling plugin: {e}", icon="❌")
                    finally:
                        spinner_container.empty()
                    st.rerun()
            else:
                if st.button(
                        "Uninstall Plugin",
                        key=f"uninstall_{p.id}",
                        help="Uninstall this plugin (check below confirmation)",
                ):
                    st.session_state["plugin_to_uninstall"] = p.id


def _list_plugins(cookie_me: Dict | None):
    run_toast()

    if not has_access("PLUGIN", "READ", cookie_me):
        st.error("You do not have access to view plugins.")
        return

    st.header("Available Plugins")

    force_system = not cookie_me
    build_agents_select("plugins", cookie_me, force_system_agent=force_system)
    if "agent_id" not in st.session_state:
        return

    # Search functionality
    search_query = st.text_input("Search plugins", "")

    client = GrinningCatClient(build_client_configuration())

    if st.session_state.get("agent_id") == DEFAULT_SYSTEM_KEY:
        _list_plugins_admins(client, search_query, cookie_me)
        if force_system:
            st.session_state.pop("agent_id", None)
        return

    _list_plugins_agents(client, search_query, cookie_me)


def _list_plugins_agents(client: GrinningCatClient, search_query: str, cookie_me: Dict | None):
    if not (agent_id := st.session_state.get("agent_id")):
        return

    try:
        plugins = client.plugins.get_available_plugins(agent_id, plugin_name=search_query)
        _list_plugins_installed(client, plugins, cookie_me)
    except Exception as e:
        st.error(f"Error fetching plugins: {e}")


def _list_plugins_admins(client: GrinningCatClient, search_query: str, cookie_me: Dict | None):
    try:
        plugins = client.admins.get_available_plugins(plugin_name=search_query)
        core_plugins_ids = client.custom.get_custom("/admins/core_plugins", DEFAULT_SYSTEM_KEY)
        _list_plugins_installed(client, plugins, cookie_me, core_plugins_ids)

        # Uninstall confirmation
        if (
                has_access("SYSTEM", "WRITE", cookie_me, only_admin=True)
                and (plugin := st.session_state.get("plugin_to_uninstall"))
        ):
            st.warning(f"⚠️ Are you sure you want to permanently uninstall plugin `{plugin}`?")
            col1, col2 = st.columns(2)

            with col1:
                if st.button("Yes, Uninstall Plugin", type="primary"):
                    spinner_container = show_overlay_spinner("Uninstalling plugin...")
                    try:
                        client.admins.delete_plugin(plugin)
                        st.toast(f"Plugin {plugin} uninstalled successfully!", icon="✅")
                        st.session_state.pop("plugin_to_uninstall", None)
                    except Exception as e:
                        st.error(f"Error uninstalling plugin: {e}", icon="❌")
                    finally:
                        spinner_container.empty()
                    st.rerun()

            with col2:
                if st.button("Cancel"):
                    st.session_state.pop("plugin_to_uninstall", None)
                    st.rerun()

        st.divider()

        # ==================== REGISTRY PLUGINS ====================
        st.subheader("Registry plugins")
        st.write(f"Found {len(plugins.registry)} plugins:")

        if plugins.registry:
            sorted_registry = sorted(
                plugins.registry, key=lambda x: (x.name or "").lower()
            )
            paginated_registry, current_page, total_pages = _paginate_items(
                sorted_registry, "registry", ITEMS_PER_PAGE
            )

            # Display paginated registry plugins
            for registry_plugin in paginated_registry:
                plugin_url = registry_plugin.id

                col0, col1, col2 = st.columns([0.05, 0.65, 0.3])

                with col0:
                    img = (
                        registry_plugin.thumb
                        if registry_plugin.thumb
                        else f"data:image/png;base64,{_image_to_base64(os.path.join(ASSETS_PATH, 'placeholder_plugin.png'))}"
                    )
                    st.markdown(
                        f'<img src="{img}" alt="" style="width: 100%; margin-bottom: 2em;" />',
                        unsafe_allow_html=True
                    )
                    # st.image(img, width="stretch")

                with col1:
                    with st.expander(f"{registry_plugin.name} (Version: {registry_plugin.version})", icon="🔌"):
                        st.write(f"**Version**: {registry_plugin.version}")
                        st.write(f"**Author**: {registry_plugin.author_name} - [Profile]({registry_plugin.author_url})")
                        st.write(f"**Description**: {registry_plugin.description or 'No description provided'}")
                        st.write(f"**Plugin URL**: [Link]({plugin_url})")
                        st.write(f"**Tags**: {registry_plugin.tags or 'No tags'}")

                with col2:
                    if (
                            has_access("PLUGIN", "WRITE", cookie_me)
                            and st.button("Install Plugin", key=f"install_{registry_plugin.name}")
                    ):
                        spinner_container = show_overlay_spinner("Installing plugin...")
                        try:
                            client.admins.post_install_plugin_from_registry(url=plugin_url)
                            st.toast("Installation successful!", icon="✅")
                        except Exception as e:
                            st.toast(f"Error installing plugin: {e}. Try to manually download and install the plugin from its repository.", icon="❌")
                        finally:
                            spinner_container.empty()
                        st.rerun()

            # Pagination controls for registry plugins
            if total_pages > 1:
                _render_pagination_controls("registry", current_page, total_pages)
    except Exception as e:
        st.error(f"Error fetching plugins: {e}")


def _list_plugins_installed(
    client: GrinningCatClient,
    plugins: PluginCollectionOutput,
    cookie_me: Dict | None,
    core_plugins_ids: List[str] | None = None,
):
    if not plugins:
        st.info("No plugins found matching your search")
        return

    # ==================== INSTALLED PLUGINS ====================
    st.subheader("Installed plugins")
    st.markdown(f"Plugins (found {len(plugins.installed)} plugins):")

    if plugins.installed:
        untoggling_plugins_ids = client.custom.get_custom("/admins/core_plugins/untoggling", DEFAULT_SYSTEM_KEY)

        sorted_installed = sorted(
            plugins.installed, key=lambda x: (x.name or "").lower()
        )
        paginated_installed, current_page, total_pages = _paginate_items(
            sorted_installed, "installed", ITEMS_PER_PAGE,
        )

        # Display paginated installed plugins
        for p in paginated_installed:
            if st.session_state.get("agent_id") == DEFAULT_SYSTEM_KEY:
                _render_installed_plugin_admins(p, client, untoggling_plugins_ids, cookie_me, core_plugins_ids)
            else:
                _render_installed_plugin_agents(p, untoggling_plugins_ids, cookie_me)

        # Pagination controls for installed plugins
        if total_pages > 1:
            _render_pagination_controls("installed", current_page, total_pages)


@st.dialog(title="Plugin Details", width="large")
def view_plugin_details(plugin_id: str):
    client = GrinningCatClient(build_client_configuration())
    try:
        plugin_details = client.admins.get_plugin_details(plugin_id).data

        # Basic info
        st.subheader("Basic Information")
        col1, col2 = st.columns(2)
        col1.write(f"**Name**: {plugin_details['name']}")
        col1.write(f"**Version**: {plugin_details['version']}")
        col1.write(f"**Author**: {plugin_details.get('author', 'Unknown')}")
        col2.write(f"**Active**: {'✅' if plugin_details.get('local_info', {}).get('active') else '❌'}")
        col2.write(f"**Plugin ID**: {plugin_id}")

        # Description
        st.write(f"**Description**: {plugin_details.get('description', 'No description provided')}")

        # Hooks
        if hooks := plugin_details.get("local_info", {}).get("hooks"):
            st.subheader("Hooks")
            for hook in hooks:
                st.write(f"- {hook['name']} (priority: {hook['priority']})")

        # Tools
        if tools := plugin_details.get("local_info", {}).get("tools"):
            st.subheader("Tools")
            for tool in tools:
                st.write(f"- {tool['name']}")

        # Forms
        if forms := plugin_details.get("local_info", {}).get("forms"):
            st.subheader("Forms")
            for form in forms:
                st.write(f"- {form['name']}")

        # MCP clients
        if mcp_clients := plugin_details.get("local_info", {}).get("mcp_clients"):
            st.subheader("MCP Clients")
            for client_info in mcp_clients:
                st.write(f"- {client_info['name']}")

        # Endpoints
        if endpoints := plugin_details.get("local_info", {}).get("endpoints"):
            st.subheader("API Endpoints")
            for endpoint in endpoints:
                st.write(f"- {endpoint['name']} (tags: {', '.join(endpoint['tags'])})")
    except Exception as e:
        st.error(f"Error fetching plugin details: {e}")
        if st.button("Back to list"):
            st.rerun()


@st.dialog(title="Manage Plugin", width="large")
def manage_plugin(plugin_id: str, untoggling_plugins_ids: List[str]):
    if not (agent_id := st.session_state.get("agent_id")):
        return

    client = GrinningCatClient(build_client_configuration())

    # fetch the plugin
    try:
        r = client.plugins.get_available_plugins(agent_id, plugin_id)
        plugins_active = [r for r in r.installed if r.local_info and r.local_info.get("active")]
        is_plugin_active = any(plugin.id == plugin_id for plugin in plugins_active)
    except Exception as e:
        st.error(f"Error fetching plugin: {e}")
        return

    # if the plugin is installed, fetch its settings and display them in a form to be edited
    st.header(f"Manage Plugin: {plugin_id}")

    plugin_settings = {}
    if is_plugin_active:
        try:
            plugin_settings, plugin_types = get_settings(
                client.plugins.get_plugin_settings(plugin_id, agent_id),
                is_selected=True
            )
            if plugin_settings:
                st.subheader("Plugin Settings")
                with st.form("plugin_settings_form", clear_on_submit=True, enter_to_submit=True):
                    # Render the form
                    edited_settings = render_json_form(plugin_settings, plugin_types)

                    if st.form_submit_button("Save Changes"):
                        spinner_container = show_overlay_spinner("Saving plugin settings...")
                        try:
                            client.plugins.put_plugin_settings(plugin_id, agent_id, edited_settings)
                            st.session_state["toast"] = {
                                "message": f"Plugin {plugin_id} settings updated successfully!", "icon": "✅"
                            }
                        except json.JSONDecodeError:
                            st.session_state["toast"] = {"message": "Invalid JSON format", "icon": "❌"}
                        except Exception as e:
                            st.session_state["toast"] = {"message": f"Error updating plugin settings: {e}", "icon": "❌"}
                        finally:
                            spinner_container.empty()
                        st.rerun()
        except Exception as e:
            st.error(f"Error fetching plugin settings: {e}")
    else:
        st.warning("""This plugin is not currently active for the selected agent.
You have to activate the plugin before managing its settings.""")

        try:
            # for a plugin that is not active on this agent the per-agent read
            # is unavailable: the system-level read serves the plugin defaults
            plugin_settings = client.admins.get_plugin_settings(plugin_id)
            with st.expander("Plugin's default configuration", icon="⚙️"):
                st.json(get_settings(plugin_settings, is_selected=False)[0])
        except Exception as e:
            st.error(f"Error fetching plugin settings: {e}")

    st.divider()

    col1, col2, col3 = st.columns(3)
    # in any case, display a button to toggle / untoggle the plugin
    with col1:
        if plugin_id not in untoggling_plugins_ids and st.button(f"{'Untoggle' if is_plugin_active else 'Toggle'} Plugin"):
            spinner_container = show_overlay_spinner(f"{'Untoggling' if is_plugin_active else 'Toggling'} plugin...")
            try:
                client.plugins.put_toggle_plugin(plugin_id, agent_id)
                st.session_state["toast"] = {
                    "message": f"Plugin {plugin_id} {'untoggled' if is_plugin_active else 'toggled'} successfully!",
                    "icon": "✅",
                }
            except Exception as e:
                st.session_state["toast"] = {
                    "message": f"Error {'untoggling' if is_plugin_active else 'toggling'} plugin: {e}",
                    "icon": "❌"
                }
            finally:
                spinner_container.empty()
            st.rerun()

    with col2:
        if is_plugin_active and plugin_settings and st.button("Reset Plugin"):
            spinner_container = show_overlay_spinner("Resetting the plugin to the factory status...")
            try:
                client.plugins.post_plugin_reset_settings(plugin_id, agent_id)
                st.session_state["toast"] = {
                    "message": f"Plugin {plugin_id} reset successfully!",
                    "icon": "✅",
                }
            except Exception as e:
                st.session_state["toast"] = {
                    "message": f"Error resetting plugin: {e}",
                    "icon": "❌"
                }
            finally:
                spinner_container.empty()
            st.rerun()

    with col3:
        if st.button("Back to list"):
            st.rerun()


def _install_plugin_from_file():
    client = GrinningCatClient(build_client_configuration())
    st.header("Install Plugin from File")

    with st.form("upload_plugin_form", clear_on_submit=True, enter_to_submit=False):
        uploaded_file = st.file_uploader(
            "Choose a plugin ZIP file",
            type=["zip"],
            accept_multiple_files=False
        )

        submitted = st.form_submit_button("Install Plugin")
        if submitted and uploaded_file is not None:
            spinner_container = show_overlay_spinner("Installing plugin from file...")

            # Create a temporary directory
            with tempfile.TemporaryDirectory() as tmp_dir:
                try:
                    # Create the temporary file path with the original filename
                    tmp_file_path = os.path.join(tmp_dir, uploaded_file.name)

                    # Write the uploaded content to the temporary file
                    with open(tmp_file_path, "wb") as tmp_file:
                        tmp_file.write(uploaded_file.getvalue())

                    # Pass the temporary file path to the SDK method
                    result = client.admins.post_install_plugin_from_zip(path_zip=tmp_file_path)
                    st.toast(f"Plugin {uploaded_file.name} is being installed!", icon="✅")
                    st.json(result.model_dump())
                except Exception as e:
                    st.toast(f"Error installing plugin: {e}", icon="❌")
                finally:
                    spinner_container.empty()
        elif submitted:
            st.toast("Please select a file to upload", icon="⚠️")


def plugins_management(cookie_me: Dict | None):
    st.title("Plugins Management Dashboard")

    # Navigation
    menu_options = {
        "(Select a menu)": {
            "page": None,
            "permission": True,
        },
        "Browse Plugins": {
            "page": "browse_plugins",
            "permission": has_access("PLUGIN", None, cookie_me),
        },
        "Install from File": {
            "page": "install_from_file",
            "permission": has_access("SYSTEM", "WRITE", cookie_me, only_admin=True),
        },
    }
    if not any(option["permission"] for option in menu_options.values() if option["page"]):
        st.error("You do not have access to any plugin management features.")
        return

    choices = {
        name: details["page"]
        for name, details in menu_options.items()
        if details["permission"]
    }

    choice = st.selectbox("Menu", choices)
    if not choice:
        return

    if menu_options[choice]["page"] == "browse_plugins":
        _list_plugins(cookie_me)
        return

    if menu_options[choice]["page"] == "install_from_file":
        _install_plugin_from_file()
