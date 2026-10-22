"""Shared test fixtures."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def no_real_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure tests never pick up a real API key from the developer's shell."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def isolated_state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point history storage at a temporary directory, never the real home."""
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return state_home
