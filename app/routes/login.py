import time
import streamlit as st
from grinning_cat_python_sdk import GrinningCatClient

from app.utils import (
    show_overlay_spinner,
    build_client_configuration,
    clear_auth_cookies,
    set_with_expiry,
    build_me_data,
)


def login_page():
    st.header("Login Page")

    auth_error = st.session_state.pop("auth_error", None)
    if auth_error:
        st.error(auth_error)

    st.sidebar.warning("Please log in to access the admin features.")

    # Render login form
    with st.form(key="login_form"):
        username = st.text_input("Username", placeholder="Enter your username")
        password = st.text_input("Password", type="password", placeholder="Enter your password")

        if not st.form_submit_button(label="Login"):
            return

        if not username or not password:
            st.error("Please enter both username and password.")
            return

        spinner_container = show_overlay_spinner(f"Authenticating {username}...")
        try:
            client = GrinningCatClient(build_client_configuration())
            token_response = client.auth.token(username, password)
            token = token_response.access_token

            st.session_state["token"] = token

            # Persist the token with a simulated expiry envelope; it is the only
            # thing that survives a page refresh. The user data goes to
            # session_state only, and _get_cookie_me() rebuilds it from the API
            # after a refresh.
            set_with_expiry("token", token, token)
            build_me_data()

            st.toast("Login successful!", icon="✅")

            spinner_container.empty()

            time.sleep(1)  # Wait for a moment before rerunning
            st.rerun()
        except Exception as e:
            clear_auth_cookies()

            spinner_container.empty()
            st.error(f"Error during authentication: {e}")
            return
