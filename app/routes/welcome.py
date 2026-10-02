from typing import Dict
import streamlit as st

from app.constants import WELCOME_MESSAGE
from app.utils import build_agents_select


def welcome(cookie_me: Dict):
    # show a welcome message if no page is selected
    st.title(WELCOME_MESSAGE)
    if cookie_me and not st.session_state.get("agent_id"):
        st.markdown("Select an agent from the dropdown below to get started.")
        build_agents_select("main", cookie_me)

        # Trigger reload when agent is selected
        if st.session_state.get("agent_id"):
            st.rerun()

    st.markdown("Use the sidebar to navigate through the different sections of the admin interface.")
