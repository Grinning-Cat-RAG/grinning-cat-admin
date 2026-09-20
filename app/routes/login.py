import time
import streamlit as st
from grinning_cat_python_sdk import GrinningCatClient
from requests.exceptions import HTTPError

from app.utils import (
    show_overlay_spinner,
    build_client_configuration,
    clear_auth_cookies,
    set_with_expiry,
    build_me_data,
)


def _describe_login_error(error: Exception) -> str:
    if isinstance(error, HTTPError) and error.response is not None:
        if error.response.status_code == 401:
            return "Invalid username or password."
        if error.response.status_code == 429:
            retry_after = str(error.response.headers.get("Retry-After", ""))
            if retry_after.isdigit():
                return f"Too many attempts, try again in {retry_after} seconds."
            return "Too many attempts, try again later."
    return f"Error during authentication: {error}"


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

            # Persist the tokens with a simulated expiry envelope; they are the
            # only things that survive a page refresh. The user data goes to
            # session_state only, and _get_cookie_me() rebuilds it from the API
            # after a refresh.
            set_with_expiry("token", token, token)
            if token_response.refresh_token:
                st.session_state["refresh_token"] = token_response.refresh_token
                set_with_expiry(
                    "refresh_token", token_response.refresh_token, token, token_response.refresh_expires_in
                )
            build_me_data()

            st.toast("Login successful!", icon="✅")

            spinner_container.empty()

            time.sleep(1)  # Wait for a moment before rerunning
            st.rerun()
        except Exception as e:
            clear_auth_cookies()

            spinner_container.empty()
            st.error(_describe_login_error(e))
            return
