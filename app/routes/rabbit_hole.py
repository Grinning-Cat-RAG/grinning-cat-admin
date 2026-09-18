import os
import tempfile
import time
from typing import Dict
import streamlit as st
from grinning_cat_python_sdk import GrinningCatClient
import json

from app.utils import build_agents_select, show_overlay_spinner, build_client_configuration, has_access, run_toast


def _upload_files(agent_id: str, cookie_me: Dict | None):
    def add_file_pair():
        st.session_state["file_metadata_pairs"].append({"file": None, "metadata": "{}"})

    run_toast()

    if not has_access("UPLOAD", "WRITE", cookie_me):
        st.error("You do not have permission to upload files.")
        return

    client = GrinningCatClient(build_client_configuration())
    st.header("Upload Files")

    allowed_file_types = client.rabbit_hole.get_allowed_mime_types(agent_id)
    st.markdown(f"""**Allowed file types**: {', '.join(allowed_file_types.allowed)}""")

    st.session_state["file_metadata_pairs"] = st.session_state.get(
        "file_metadata_pairs", [{"file": None, "metadata": "{}"}],
    )
    st.session_state["remove_index"] = st.session_state.get("remove_index")

    #  Add file button outside the form
    cols = st.columns([4, 1])
    with cols[0]:
        st.write("### Files to upload")
    with cols[1]:
        if st.button("➕ Add file"):
            add_file_pair()
            st.rerun()  # Refresh to show the new file input

    if st.session_state["remove_index"] is not None:
        del st.session_state["file_metadata_pairs"][st.session_state["remove_index"]]
        st.session_state["remove_index"] = None
        st.rerun()

    with st.form("upload_files_form", clear_on_submit=True, enter_to_submit=False):
        # Display each file-metadata pair
        for i, pair in enumerate(st.session_state["file_metadata_pairs"]):
            col1, col2, col3 = st.columns([1, 1, 0.5])
            with col1:
                pair["file"] = st.file_uploader(f"File {i + 1}", key=f"file_{i}")
            with col2:
                pair["metadata"] = st.text_area(
                    f"Metadata {i + 1}",
                    value=pair["metadata"],
                    key=f"metadata_{i}",
                    height=150,
                    help="Enter metadata as JSON for this specific file"
                )
            with col3:
                if len(st.session_state["file_metadata_pairs"]) > 1:
                    if st.form_submit_button(f"❌ Remove this file (#{i+1})", help="Check to remove this file from the upload list"):
                        st.session_state["remove_index"] = i
                        st.rerun()

        if not st.form_submit_button("📤 Upload Files"):
            return

        file_paths = []
        metadata_dict = {}
        has_errors = False
        temp_files = []  # Keep track of temporary files for cleanup

        for i, pair in enumerate(st.session_state["file_metadata_pairs"]):
            if not pair["file"]:
                st.error(f"Please select a file for File {i + 1}")
                has_errors = True
                continue

            try:
                # Validate JSON metadata
                metadata = json.loads(pair["metadata"])

                # Save uploaded file to temporary location
                uploaded_file = pair["file"]
                temp_file = tempfile.NamedTemporaryFile(
                    delete=False,
                    suffix=f"_{uploaded_file.name}"
                )
                temp_file.write(uploaded_file.getbuffer())
                temp_file.close()

                # Add to our lists
                file_paths.append(temp_file.name)
                temp_files.append(temp_file.name)  # For cleanup
                metadata_dict[uploaded_file.name] = metadata
            except json.JSONDecodeError:
                st.error(f"Invalid JSON format in metadata for File {i + 1}")
                has_errors = True

        if has_errors or not file_paths:
            return

        try:
            spinner_container = show_overlay_spinner("Loading files to RAG...")

            client.rabbit_hole.post_files(
                file_paths=file_paths,
                agent_id=agent_id,
                metadata=metadata_dict,
            )
            st.toast(f"Successfully uploaded {len(file_paths)} files!", icon="✅")
            # Clear the files after successful upload
            st.session_state["file_metadata_pairs"] = [{"file": None, "metadata": "{}"}]
        except Exception as e:
            st.toast(f"Error uploading files: {e}", icon="❌")
        finally:
            spinner_container.empty()  # Remove the spinner
            # Clean up temporary files
            for temp_file_path in temp_files:
                try:
                    os.unlink(temp_file_path)
                except OSError:
                    pass  # Ignore cleanup errors


def _upload_url(agent_id: str, cookie_me: Dict | None):
    run_toast()

    if not has_access("UPLOAD", "WRITE", cookie_me):
        st.error("You do not have permission to upload files.")
        return

    client = GrinningCatClient(build_client_configuration())
    st.header("Upload from URL")

    with st.form("upload_url_form", clear_on_submit=True, enter_to_submit=False):
        url = st.text_input(
            "Website URL",
            placeholder="https://example.com"
        )

        metadata = st.text_area(
            "Metadata (JSON format)",
            value="{}",
            help="Enter metadata to be stored with the content"
        )

        if not st.form_submit_button("Upload URL"):
            return

        try:
            spinner_container = show_overlay_spinner("Loading URL to RAG...")

            metadata_dict = json.loads(metadata)
            client.rabbit_hole.post_web(
                web_url=url,
                agent_id=agent_id,
                metadata=metadata_dict
            )
            st.toast(f"URL {url} is being ingested!", icon="✅")
        except json.JSONDecodeError:
            st.toast("Invalid JSON format in metadata", icon="❌")
        except Exception as e:
            st.toast(f"Error uploading URL: {e}", icon="❌")
        finally:
            spinner_container.empty()


def _list_files(agent_id: str, cookie_me: Dict | None):
    def download_file(file_name):
        try:
            response = client.file_manager.get_file(agent_id, file_name)
            return response.content
        except Exception as ex:
            st.toast(f"Error downloading file: {ex}", icon="❌")
            return None

    run_toast()

    if not has_access("MEMORY", "READ", cookie_me):
        st.error("You do not have permission to view uploaded files.")
        return

    client = GrinningCatClient(build_client_configuration())
    st.header("Uploaded Files")

    try:
        files = client.file_manager.get_file_manager_attributes(agent_id)

        # print the total size and number of files
        st.write(f"**Total files uploaded**: {len(files.files)}")
        st.write(f"**Total size of uploaded files**: {files.size} bytes")

        for file in files.files:
            col1, col2, col3 = st.columns([0.8, 0.1, 0.1])

            with col1:
                # Extracted-image files store the source PDF under metadata.source
                # and the saved image file name under metadata.image_file: count
                # both (the two point sets are disjoint).
                chunks_source = client.memory.get_memory_points(
                    agent_id=agent_id,
                    collection="declarative",
                    metadata={"source": file.name},
                )
                chunks_image = client.memory.get_memory_points(
                    agent_id=agent_id,
                    collection="declarative",
                    metadata={"image_file": file.name},
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
                    if st.button("Delete", key=f"delete_{file.name}", help="Permanently delete this file"):
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

                    client.file_manager.delete_file(agent_id, file.name)
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


def rabbit_hole_management(cookie_me: Dict | None):
    st.title("Knowledge Base Management")

    st.info("**Disclaimer**: If you want to store the files of the Knowledge Base to a specific file manager, please select one in the **File Managers** section.")

    build_agents_select("rabbit_hole", cookie_me)
    if not (agent_id := st.session_state.get("agent_id")):
        return

    # Navigation
    menu_options = {
        "(Select a menu)": {
            "page": None,
            "permission": True,
        },
        "Upload Files": {
            "page": "upload_files",
            "permission": has_access("UPLOAD", "WRITE", cookie_me),
        },
        "Upload from URL": {
            "page": "upload_url",
            "permission": has_access("UPLOAD", "WRITE", cookie_me),
        },
        "View Uploaded Files": {
            "page": "list_files",
            "permission": has_access("MEMORY", "READ", cookie_me),
        },
    }
    if not any(option["permission"] for option in menu_options.values() if option["page"]):
        st.error("You do not have access to any Knowledge Base management features.")
        return

    choices = {
        name: details["page"]
        for name, details in menu_options.items()
        if details["permission"]
    }

    choice = st.selectbox("Menu", choices)

    if menu_options[choice]["page"] == "upload_files":
        _upload_files(agent_id, cookie_me)
        return

    if menu_options[choice]["page"] == "upload_url":
        _upload_url(agent_id, cookie_me)
        return

    if menu_options[choice]["page"] == "list_files":
        _list_files(agent_id, cookie_me)
