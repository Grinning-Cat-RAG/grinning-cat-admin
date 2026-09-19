import json
import time
from typing import Dict, List
import streamlit as st
from grinning_cat_python_sdk import GrinningCatClient

from app.constants import DEFAULT_SYSTEM_KEY
from app.utils import build_agents_select, show_overlay_spinner, build_client_configuration, run_toast, has_access


def _sanitize_selected_permissions(permissions: Dict[str, List[str]]) -> Dict[str, List[str]]:
    sanitized_permissions = {}
    for resource, perms in permissions.items():
        if len(perms) == 0:
            continue

        sanitized_permissions[resource] = perms

    return sanitized_permissions


def _sanitize_retrieved_permissions(permissions: Dict[str, List[str]], agent_key: str) -> Dict[str, List[str]]:
    sanitized_permissions = {}
    is_system = agent_key == DEFAULT_SYSTEM_KEY

    auth_admin_resources = ["SYSTEM", "CHESHIRE_CAT", "EMBEDDER"]

    for resource, perms in permissions.items():
        # Skip chat for system users or admin resources for non-system users
        if (
                (is_system and resource == "CHAT")
                or (not is_system and resource in auth_admin_resources)
        ):
            continue

        sanitized_permissions[resource] = perms

    return sanitized_permissions


def _create_user(agent_id: str, cookie_me: Dict | None):
    run_toast()

    if not has_access("USERS", "WRITE", cookie_me):
        st.error("You do not have permission to create users.")
        return

    client = GrinningCatClient(build_client_configuration())

    # Initialize form key in session state if not present
    st.session_state["user_form_key"] = st.session_state.get("user_form_key", 0)

    st.header("Create New User")
    with st.form(f"create_user_form_{st.session_state['user_form_key']}", enter_to_submit=False):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")

        metadata = st.text_area(
            "User Metadata (JSON-like string)",
            help="Optional metadata for the agent in JSON format",
            value="{}",
        )

        # Permissions editor
        st.subheader("Permissions")

        available_permissions = _sanitize_retrieved_permissions(client.auth.get_available_permissions(), agent_id)
        selected_permissions = {}
        for res, perms in available_permissions.items():
            st.write(f"**{res}**")
            cols = st.columns(len(perms))
            permissions = []
            for i, perm in enumerate(perms):
                is_checked = cols[i].checkbox(perm, key=f"{res}_{perm}_{st.session_state['user_form_key']}")
                if is_checked:
                    permissions.append(perm)
            selected_permissions[res] = permissions

            st.divider()

        if not st.form_submit_button("Create User"):
            return

        if not username or not password:
            st.error("Username and password are required")
            return

        if all(len(perms) == 0 for perms in selected_permissions.values()):
            st.error("At least one permission must be selected")
            return

        try:
            valid_metadata = True
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            valid_metadata = False
            metadata = {}

        spinner_container = show_overlay_spinner("Creating user...")
        try:
            result = client.users.post_user(
                agent_id, username, password, _sanitize_selected_permissions(selected_permissions), metadata
            )
            message = f"User {result.username} created successfully!"
            icon = "✅"
            if not valid_metadata:
                message += " Metadata must be a valid JSON string. Ignoring metadata."
                icon = "⚠️"

            st.toast(message, icon=icon)
            time.sleep(1)

            # Increment form key to reset the form on next rerun
            st.session_state["user_form_key"] += 1
            st.rerun()
        except Exception as e:
            st.toast(f"Error creating user: {e}", icon="❌")
        finally:
            spinner_container.empty()


def _list_users(agent_id: str, cookie_me: Dict | None):
    run_toast()

    if not has_access("USERS", "READ", cookie_me):
        st.error("You do not have permission to view users.")
        return

    client = GrinningCatClient(build_client_configuration())
    st.header("List All Users")

    try:
        users = client.users.get_users(agent_id)
        if not users:
            st.info("No user found")
            return

        st.write(f"Found {len(users)} users:")
        for user in users:
            col1, col2, col3, col4 = st.columns([0.7, 0.1, 0.1, 0.1])

            with col1:
                with st.expander(f"User: {user.username} (ID: {user.id})", icon="👤"):
                    st.json(user.model_dump())

            with col2:
                # Action buttons
                if st.button("View", key=f"view_{user.id}"):
                    _get_user(agent_id, user.id, cookie_me)

            with col3:
                if has_access("USERS", "WRITE", cookie_me):
                    if st.button("Update", key=f"update_{user.id}"):
                        _update_user(agent_id, user.id, cookie_me)
                else:
                    st.button("Update", key=f"update_{user.id}", disabled=True, help="No permission to update")

            with col4:
                if has_access("USERS", "DELETE", cookie_me):
                    if st.button("Delete", key=f"delete_{user.id}", help="Permanently delete this item (check below confirmation)"):
                        st.session_state["user_to_delete"] = user
                else:
                    st.button("Delete", key=f"delete_{user.id}", disabled=True, help="No permission to delete")

        # Delete confirmation
        if not (user := st.session_state.get("user_to_delete")):
            return

        if not has_access("USERS", "DELETE", cookie_me):
            st.error("You do not have permission to delete users.")
            st.session_state.pop("user_to_delete", None)
            return

        st.warning(f"⚠️ Are you sure you want to permanently delete user `{user.id}`?")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Yes, Delete User", type="primary"):
                try:
                    spinner_container = show_overlay_spinner(f"Deleting user {user.id}...")
                    client.users.delete_user(user.id, agent_id)
                    st.toast(f"Admin {user.id} deleted successfully!", icon="✅")
                    st.session_state.pop("user_to_delete", None)
                    time.sleep(1)  # Wait for a moment before rerunning
                except Exception as e:
                    st.error(f"Error deleting user: {e}", icon="❌")
                finally:
                    spinner_container.empty()
                st.rerun()
        with col2:
            if st.button("Cancel"):
                st.session_state.pop("user_to_delete", None)
                st.rerun()
    except Exception as e:
        st.error(f"Error fetching users: {e}")


@st.dialog(title="User Details", width="large")
def _get_user(agent_id: str, user_id: str, cookie_me: Dict | None):
    if not has_access("USERS", "READ", cookie_me):
        st.error("You do not have permission to view user details.")
        return

    client = GrinningCatClient(build_client_configuration())
    st.header(f"User Details for ID: {user_id}")

    try:
        user_data = client.users.get_user(user_id, agent_id)
        st.json(user_data.model_dump())
    except Exception as e:
        st.error(f"Error fetching user `{user_id}`: {e}")


@st.dialog(title="Update Details", width="large")
def _update_user(agent_id: str, user_id: str, cookie_me: Dict | None):
    if not has_access("USERS", "WRITE", cookie_me):
        st.error("You do not have permission to update users.")
        return

    client = GrinningCatClient(build_client_configuration())
    st.header(f"Update User ID: {user_id}")

    try:
        user_data = client.users.get_user(user_id, agent_id)
    except Exception:
        st.error(f"User with ID `{user_id}` not found")
        return

    with st.form("update_user_form", enter_to_submit=False):
        new_username = st.text_input("Username", value=user_data.username)
        new_password = st.text_input("Password (leave blank to keep current)", type="password")

        new_metadata = st.text_area(
            "User Metadata (JSON-like string)",
            help="Optional metadata for the agent in JSON format",
            value=json.dumps(user_data.metadata, indent=2) if user_data.metadata else "{}"
        )

        # Permissions editor
        st.subheader("Permissions")

        current_permissions = user_data.permissions
        available_permissions = _sanitize_retrieved_permissions(client.auth.get_available_permissions(), agent_id)

        selected_permissions = {}
        for res, perms in available_permissions.items():
            st.write(f"**{res}**")
            cols = st.columns(len(perms))
            permissions = []
            for i, perm in enumerate(perms):
                is_checked = cols[i].checkbox(perm, value=perm in current_permissions.get(res, []), key=f"{res}_{perm}")
                if is_checked:
                    permissions.append(perm)
            selected_permissions[res] = permissions

            st.divider()

        if not st.form_submit_button("Update User"):
            return

        if not new_username:
            st.error("Username cannot be empty")
            return

        try:
            valid_metadata = True
            new_metadata = json.loads(new_metadata)
        except json.JSONDecodeError:
            valid_metadata = False
            new_metadata = {}

        try:
            spinner_container = show_overlay_spinner(f"Updating user {user_id}...")

            result = client.users.put_user(
                agent_id=agent_id,
                user_id=user_id,
                username=new_username,
                password=new_password or None,
                permissions=_sanitize_selected_permissions(selected_permissions) or None,
                metadata=new_metadata,
            )

            message = f"User {result.username} updated successfully!"
            icon = "✅"
            if not valid_metadata:
                message += " Metadata must be a valid JSON string. Ignoring metadata."
                icon = "⚠️"

            st.session_state["toast"] = {"message": message, "icon": icon}
        except Exception as e:
            st.session_state["toast"] = {"message": f"Error updating user `{user_id}`: {e}", "icon": "❌"}
        finally:
            spinner_container.empty()
            time.sleep(1)
            st.rerun()


# Streamlit UI
def users_management(cookie_me: Dict | None):
    st.title("User Management Dashboard")

    build_agents_select("users", cookie_me)
    if not (agent_id := st.session_state.get("agent_id")):
        return

    # Navigation
    menu_options = {
        "(Select a menu)": {
            "page": None,
            "permission": True,
        },
        "List Users": {
            "page": "list_users",
            "permission": has_access("USERS", "READ", cookie_me),
        },
        "Create User": {
            "page": "create_user",
            "permission": has_access("USERS", "WRITE", cookie_me),
        },
    }
    if not any(option["permission"] for option in menu_options.values() if option["page"]):
        st.error("You do not have access to any user management features.")
        return

    choices = {
        name: details["page"]
        for name, details in menu_options.items()
        if details["permission"]
    }

    choice = st.selectbox("Menu", choices)

    if menu_options[choice]["page"] == "list_users":
        _list_users(agent_id, cookie_me)
        return

    if menu_options[choice]["page"] == "create_user":
        _create_user(agent_id, cookie_me)
