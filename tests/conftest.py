"""Shared test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def no_real_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure tests never pick up a real API key from the developer's shell."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
