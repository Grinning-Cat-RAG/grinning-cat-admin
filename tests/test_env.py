"""The .env is written by hand, so its values must be read the way they look."""
from pathlib import Path

import pytest

from app import env


@pytest.mark.parametrize("raw", ["true", "True", "TRUE", " true ", "1"])
def test_get_env_bool_is_case_insensitive(monkeypatch, raw):
    monkeypatch.setenv("GRINNING_CAT_API_SECURE_CONNECTION", raw)

    assert env.get_env_bool("GRINNING_CAT_API_SECURE_CONNECTION") is True


@pytest.mark.parametrize("raw", ["false", "False", "0", ""])
def test_get_env_bool_rejects_everything_else(monkeypatch, raw):
    monkeypatch.setenv("GRINNING_CAT_API_SECURE_CONNECTION", raw)

    assert env.get_env_bool("GRINNING_CAT_API_SECURE_CONNECTION") is False


def test_get_env_bool_falls_back_to_the_declared_default(monkeypatch):
    monkeypatch.delenv("GRINNING_CAT_API_SECURE_CONNECTION", raising=False)

    assert env.get_env_bool("GRINNING_CAT_API_SECURE_CONNECTION") is False


def test_every_supported_variable_is_read_by_the_app():
    """A variable documented in .env.example but read nowhere is a promise the
    app does not keep: GRINNING_CAT_API_KEY was exactly that."""
    app_dir = Path(env.__file__).parent
    sources = {
        path: path.read_text(encoding="utf-8")
        for path in app_dir.rglob("*.py")
        if path.name != "env.py"
    }
    assert sources, "no application sources found"

    for name in env.get_supported_env_variables():
        assert any(name in source for source in sources.values()), (
            f"{name} is declared as supported but never read"
        )
