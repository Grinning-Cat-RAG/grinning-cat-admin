from typing import Dict

import streamlit as st
from grinning_cat_python_sdk import GrinningCatClient
from grinning_cat_python_sdk.models.dtos import Message

from app.constants import INTRO_MESSAGE
from app.utils import build_client_configuration, has_access, run_toast


async def chat(cookie_me: Dict):
    run_toast()

    st.header("Chat with the GrinningCat")

    if not has_access("CHAT", "WRITE", cookie_me):
        st.error("You do not have permission to access the chat functionality.")
        return

    if not (agent_id := st.session_state.get("agent_id")):
        return

    if not (user_id := st.session_state.get("user_id")):
        return

    messages_key = f"messages_{agent_id}_{user_id}"
    chat_id_key = f"chat_id_{agent_id}_{user_id}"

    st.session_state.setdefault(chat_id_key, None)
    st.session_state.setdefault(messages_key, [])

    if not st.session_state[messages_key] and INTRO_MESSAGE:
        st.session_state[messages_key].append({
            "role": "assistant",
            "content": INTRO_MESSAGE,
        })

    client = GrinningCatClient(build_client_configuration())

    user_message = st.chat_input(placeholder="Type your message here...")
    if user_message:
        st.session_state[messages_key].append({
            "role": "user",
            "content": user_message,
        })

        try:
            # Render past messages
            st.write("###     Conversation History")
            for message in st.session_state[messages_key]:
                with st.chat_message(message["role"]):
                    st.markdown(message["content"])

            # Show typewriter effect live while the response streams in
            with st.chat_message("assistant"):
                placeholder = st.empty()
                accumulated = ""
                placeholder.markdown("_Thinking..._  ⠋")  # <-- show while waiting

                def streaming_callback(event):
                    nonlocal accumulated
                    if isinstance(event, dict) and event.get("type") == "chat_token":
                        accumulated += event.get("content")
                        placeholder.markdown(accumulated + "▌")  # blinking cursor effect

                response = await client.message.send_websocket_message(
                    Message(text=user_message),
                    agent_id=agent_id,
                    user_id=user_id,
                    chat_id=st.session_state[chat_id_key],
                    callback=streaming_callback,
                )

                # Finalise: remove cursor, show clean text
                final_text = response.message.text
                placeholder.markdown(final_text)

            st.session_state[messages_key].append({
                "role": "assistant",
                "content": final_text,
            })
            st.session_state[chat_id_key] = response.chat_id
        except Exception as e:
            st.toast(f"Error sending message: {e}", icon="❌")

        return

    st.write("###     Conversation History")
    for message in st.session_state[messages_key]:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
