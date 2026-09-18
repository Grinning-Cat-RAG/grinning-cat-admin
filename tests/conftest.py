import sys
from pathlib import Path

import pytest
import streamlit as st

# `make install` syncs with --no-install-project, so the `app` package is not
# importable from site-packages: point at the working tree explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class Rerun(BaseException):
    """Stand-in for st.rerun(), which aborts the Streamlit script run.

    Streamlit's own RerunException derives from BaseException so that a broad
    `except Exception` cannot swallow it; the stand-in must do the same or a
    test could pass on code that eats the rerun.
    """


@pytest.fixture
def session_state(monkeypatch):
    """Replace st.session_state with a plain dict.

    Outside `streamlit run` the real proxy only warns and behaves erratically;
    a dict exposes the same mapping API the app relies on (get/pop/clear/in).
    """
    state = {}
    monkeypatch.setattr(st, "session_state", state)
    return state


@pytest.fixture
def no_rerun(monkeypatch):
    def _rerun(*_args, **_kwargs):
        raise Rerun()

    monkeypatch.setattr(st, "rerun", _rerun)
    return Rerun


@pytest.fixture
def stub_streamlit_widgets(monkeypatch):
    """Silence the layout/feedback calls a form-driven page makes."""
    import contextlib

    class _Container:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    class _Placeholder:
        container = staticmethod(contextlib.nullcontext)

        @staticmethod
        def empty():
            return None

    for name in ("header", "error", "warning", "markdown", "toast", "caption", "divider"):
        monkeypatch.setattr(st, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(st, "form", lambda *args, **kwargs: _Container())
    monkeypatch.setattr(st, "form_submit_button", lambda *args, **kwargs: True)
    monkeypatch.setattr(st, "empty", lambda: _Placeholder())
