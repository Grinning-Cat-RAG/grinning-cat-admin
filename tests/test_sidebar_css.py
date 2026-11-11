"""The prod CSS must keep the sidebar expand control visible on Streamlit 1.67."""
import pytest
import streamlit as st

from app import main


def _captured_css(monkeypatch, environment: str) -> str:
    captured = []
    monkeypatch.setattr(main, "get_env", lambda name: environment)
    monkeypatch.setattr(st, "markdown", lambda body, *a, **k: captured.append(body))
    main._apply_custom_css()
    return "\n".join(captured)


def test_prod_css_keeps_the_sidebar_control_visible(monkeypatch):
    css = _captured_css(monkeypatch, "prod")

    assert 'div[data-testid="stToolbar"] {display: none;}' not in css
    assert "stExpandSidebarButton" in css
    assert "headerNoPadding" in css
    assert "stSidebarCollapsedControl" not in css
