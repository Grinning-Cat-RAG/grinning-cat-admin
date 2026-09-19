import time
from typing import Dict
import streamlit as st
from grinning_cat_python_sdk import GrinningCatClient

from app.utils import (
    build_agents_select,
    build_client_configuration,
    build_conversations_select,
    build_users_select,
    show_overlay_spinner,
    has_access,
    run_toast,
)


def _memory_collections(agent_id: str, cookie_me: Dict | None):
    run_toast()

    if not has_access("MEMORY", "READ", cookie_me):
        st.error("You do not have access to view memory collections.")
        return

    client = GrinningCatClient(build_client_configuration())
    st.header("Memory Collections")

    try:
        collections = client.memory.get_memory_collections(agent_id)

        if not collections.collections:
            st.info("No memory collections found for this agent")
            return

        st.write("### Available Memory Collections")
        for collection in collections.collections:
            col1, col2 = st.columns([0.8, 0.2])

            with col1:
                st.write(f"**{collection.name}**")
                st.write(f"Vectors count: {collection.vectors_count}")

            with col2:
                if has_access("MEMORY", "DELETE", cookie_me):
                    if st.button("Delete", key=f"destroy_{collection.name}", help="Permanently destroy this collection (check below confirmation)"):
                        st.session_state["collection_to_delete"] = collection.name
                else:
                    st.button(
                        "Delete",
                        key=f"destroy_{collection.name}",
                        disabled=True,
                        help="You do not have permission to delete memory collections.",
                    )

            st.divider()

        # Destroy confirmation
        if has_access("MEMORY", "DELETE", cookie_me) and (collection := st.session_state.get("collection_to_delete")):
            st.warning(f"⚠️ Are you sure you want to permanently destroy collection `{collection}`?")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Yes, Destroy Collection", type="primary"):
                    try:
                        spinner_container = show_overlay_spinner(f"Destroying collection {collection}...")

                        result = client.memory.delete_all_single_memory_collection_points(collection, agent_id)
                        if result.deleted[collection]:
                            st.toast(f"Collection {collection} destroyed successfully!", icon="✅")
                            st.session_state.pop("collection_to_delete", None)
                            time.sleep(1)  # Wait for a moment before rerunning
                        else:
                            st.toast(f"Failed to completely destroy collection {collection}", icon="❌")
                    except Exception as e:
                        st.toast(f"Error destroying collection: {e}", icon="❌")
                    finally:
                        spinner_container.empty()

                    st.rerun()
            with col2:
                if st.button("Cancel"):
                    st.session_state.pop("collection_to_delete", None)
                    st.rerun()
    except Exception as e:
        st.error(f"Error fetching memory collections: {e}")


def _view_conversation_history(agent_id: str, user_id: str, conversation_id: str, cookie_me: Dict | None):
    def pop_state_keys():
        for key in ["conversation_to_change_name", "conversation_to_delete"]:
            if key in st.session_state:
                st.session_state.pop(key, None)

    run_toast()

    if not has_access("MEMORY", "READ", cookie_me):
        st.error("You do not have access to view conversation history.")
        return

    client = GrinningCatClient(build_client_configuration())
    st.header("Conversation History")

    try:
        history = client.conversation.get_conversation_history(agent_id, user_id, conversation_id)

        if not history.history:
            st.info("No conversation history found for this user and conversation")
            return

        col1, col2, col3, col4 = st.columns([0.7, 0.07, 0.13, 0.1])
        with col1:
            for item in history.history:
                st.write(f"**{item.who}**: {item.content.text}")
                if item.content.image:
                    st.image(item.image, caption="Image", use_column_width=True)

        with col2:
            if has_access("MEMORY", "DELETE", cookie_me):
                if st.button("Delete", key=f"delete_{agent_id}_{user_id}_{conversation_id}", help="Delete this conversation (check below confirmation)"):
                    pop_state_keys()
                    st.session_state["conversation_to_delete"] = True
            else:
                st.button(
                    "Delete",
                    key=f"delete_{agent_id}_{user_id}_{conversation_id}",
                    disabled=True,
                    help="You do not have permission to delete this conversation.",
                )

        with col3:
            if has_access("MEMORY", "WRITE", cookie_me):
                if st.button(
                        "Change the name",
                        key=f"change_name_{agent_id}_{user_id}_{conversation_id}",
                        help="Change the name of this conversation"
                ):
                    pop_state_keys()
                    st.session_state["conversation_to_change_name"] = True
            else:
                st.button(
                    "Change the name",
                    key=f"change_name_{agent_id}_{user_id}_{conversation_id}",
                    disabled=True,
                    help="You do not have permission to change the conversation name.",
                )

        with col4:
            if has_access("MEMORY", "READ", cookie_me):
                if st.button(
                        "View files",
                        key=f"edit_files_{agent_id}_{user_id}_{conversation_id}",
                        help="Change the name of this conversation"
                ):
                    pop_state_keys()
                    _edit_chat_files(agent_id, conversation_id, cookie_me)
            else:
                st.button(
                    "View files",
                    key=f"edit_files_{agent_id}_{user_id}_{conversation_id}",
                    disabled=True,
                    help="You do not have permission to view the files in this conversation.",
                )

        # Change Name confirmation
        if has_access("MEMORY", "WRITE", cookie_me) and st.session_state.get("conversation_to_change_name"):
            new_conversation_name = st.text_input(
                "New Conversation Name",
                value=f"{conversation_id}_change_name",
                key="new_conversation_name_input",
            )

            col1, col2 = st.columns(2)
            with col1:
                if st.button("Change the Name", type="primary"):
                    try:
                        with st.spinner(f"Changing the name of the conversation {conversation_id}..."):
                            result = client.conversation.put_conversation_attributes(
                                name=new_conversation_name,
                                agent_id=agent_id,
                                user_id=user_id,
                                chat_id=conversation_id,
                            )
                        if result.changed:
                            st.toast("Conversation name successfully changed!", icon="✅")
                            st.session_state.pop("conversation_to_change_name", None)
                            time.sleep(1)  # Wait for a moment before rerunning
                            st.rerun()
                        else:
                            st.toast(
                                f"Failed to change the name of the conversation {conversation_id}",
                                icon="❌",
                            )
                    except Exception as e:
                        st.toast(
                            f"Error changing the name of the conversation {conversation_id}: {e}",
                            icon="❌",
                        )

            with col2:
                if st.button("Cancel"):
                    st.session_state.pop("conversation_to_change_name", None)
                    st.rerun()

        # Delete confirmation
        if has_access("MEMORY", "DELETE", cookie_me) and st.session_state.get("conversation_to_delete"):
            st.warning("⚠️ Are you sure you want to permanently delete this conversation history?")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Yes, Delete History", type="primary"):
                    try:
                        spinner_container = show_overlay_spinner(
                            f"Deleting conversation history {conversation_id} for Agent {agent_id}, User {user_id}..."
                        )

                        result = client.conversation.delete_conversation(agent_id, user_id, conversation_id)
                        if result.deleted:
                            st.toast("Conversation history deleted successfully!", icon="✅")
                            st.session_state.pop("conversation_to_delete", None)
                            time.sleep(1)  # Wait for a moment before rerunning
                        else:
                            st.toast("Failed to delete conversation history", icon="❌")
                    except Exception as e:
                        st.toast(f"Error deleting conversation history: {e}", icon="❌")
                    finally:
                        spinner_container.empty()

                    st.rerun()
            with col2:
                if st.button("Cancel"):
                    st.session_state.pop("conversation_to_delete", None)
                    st.rerun()
    except Exception as e:
        st.error(f"Error fetching conversation history: {e}")


@st.dialog(title="Edit Vector Database", width="large")
def _edit_chat_files(agent_id: str, conversation_id: str, cookie_me: Dict | None):
    def download_file(file_name):
        try:
            response = client.file_manager.get_file(agent_id, file_name)
            return response.content
        except Exception as ex:
            st.toast(f"Error downloading file: {ex}", icon="❌")
            return None

    if not has_access("MEMORY", "READ", cookie_me):
        st.error("You do not have access to list the files in this conversation.")
        return

    client = GrinningCatClient(build_client_configuration())
    try:
        files = client.file_manager.get_file_manager_attributes(agent_id, conversation_id)
        if not files.files:
            st.info("No files found in this conversation.")
            return

        for file in files.files:
            col1, col2, col3 = st.columns([0.7, 0.15, 0.15])

            with col1:
                # Extracted-image files store the source document under
                # metadata.source and the saved image file name under
                # metadata.image_file: count both (disjoint queries).
                chunks_source = client.memory.get_memory_points(
                    agent_id=agent_id,
                    collection="episodic",
                    metadata={"source": file.name, "chat_id": conversation_id},
                )
                chunks_image = client.memory.get_memory_points(
                    agent_id=agent_id,
                    collection="episodic",
                    metadata={"image_file": file.name, "chat_id": conversation_id},
                )
                chunks_count = len(chunks_source.points) + len(chunks_image.points)

                with st.expander(f"{file.name} ({file.size} bytes)"):
                    st.write(f"**Name**: {file.name}")
                    st.write(f"**Size**: {file.size}")
                    st.write(f"**Last modified**: {file.last_modified}")
                    st.write(f"**Chunks**: {chunks_count}")

            with col2:
                # Use a regular button instead of download_button
                if st.button("Download", key=f"download_{file.name}"):
                    # Only fetch the file content when button is clicked
                    file_content = download_file(file.name)
                    if file_content:
                        # Store in session state to trigger download
                        st.session_state[f"download_content_{file.name}"] = file_content
                        st.toast("Download started!", icon="✅")
                        st.rerun()

                # Check if we have content ready to download
                if f"download_content_{file.name}" in st.session_state:
                    # Create the actual download button with the fetched content
                    st.download_button(
                        label="Click to save",
                        data=st.session_state[f"download_content_{file.name}"],
                        file_name=file.name,
                        key=f"save_{file.name}"
                    )
                    # Clear the session state after download
                    st.session_state.pop(f"download_content_{file.name}", None)

            with col3:
                if has_access("MEMORY", "DELETE", cookie_me):
                    if st.button("Delete", key=f"delete_{file.name}", help="Permanently delete this file (check below confirmation)"):
                        st.session_state["file_to_delete"] = file
                else:
                    st.button("Delete", key=f"delete_{file.name}", disabled=True, help="You do not have permission to delete files")

        # Delete confirmation
        if not (file := st.session_state.get("file_to_delete")):
            return

        if not has_access("MEMORY", "DELETE", cookie_me):
            st.error("You do not have permission to delete files.")
            st.session_state.pop("file_to_delete", None)
            return

        st.warning(f"⚠️ Are you sure you want to permanently delete file `{file.name}`?")

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Yes, Delete File", type="primary"):
                try:
                    spinner_container = show_overlay_spinner(f"Deleting file {file.name}...")

                    client.file_manager.delete_file(agent_id, file.name, chat_id=conversation_id)
                    st.toast(f"File {file.name} deleted successfully!", icon="✅")
                    st.session_state.pop("file_to_delete", None)
                    time.sleep(1)  # Wait for a moment before rerunning
                except Exception as e:
                    st.error(f"Error deleting admin: {e}", icon="❌")
                finally:
                    spinner_container.empty()

                st.rerun()
        with col2:
            if st.button("Cancel"):
                st.session_state.pop("file_to_delete", None)
                st.rerun()
    except Exception as e:
        st.toast(f"Error fetching files: {e}", icon="❌")


# Streamlit UI
def memory_management(cookie_me: Dict | None):
    st.title("Memory Management Dashboard")

    build_agents_select("memory", cookie_me)
    if not (agent_id := st.session_state.get("agent_id")):
        return

    # Navigation
    menu_options = {
        "(Select a menu)": {
            "page": None,
            "permission": True,
        },
        "List Memory Collections": {
            "page": "list_collections",
            "permission": has_access("MEMORY", "READ", cookie_me),
        },
        "View Conversation History": {
            "page": "view_conversation_history",
            "permission": has_access("MEMORY", "READ", cookie_me),
        },
    }
    if not any(option["permission"] for option in menu_options.values() if option["page"]):
        st.error("You do not have access to any memory management features.")
        return

    choices = {
        name: details["page"]
        for name, details in menu_options.items()
        if details["permission"]
    }

    choice = st.selectbox("Menu", choices)
    if not choice:
        return

    if menu_options[choice]["page"] == "list_collections":
        _memory_collections(agent_id, cookie_me)
        return

    if menu_options[choice]["page"] == "view_conversation_history":
        build_users_select("memory", agent_id, cookie_me)
        if not (user_id := st.session_state.get("user_id")):
            return

        build_conversations_select("memory", agent_id, user_id)
        if not (conversation_id := st.session_state.get("conversation_id")):
            return

        _view_conversation_history(agent_id, user_id, conversation_id, cookie_me)
