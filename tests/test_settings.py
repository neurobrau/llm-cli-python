"""Tests for the settings.json format and store."""

import json
import os
import stat
from pathlib import Path

import pytest

from neurobrau_cli import settings
from neurobrau_cli.settings import ModelPreferences, SettingsStore, UserSettings


@pytest.fixture
def store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(tmp_path / "config" / "neurobrau" / "settings.json")


def sample() -> UserSettings:
    return (
        UserSettings("brief")
        .with_model("openai", "gpt-5", reasoning="low", max_output_tokens=1024,
                    max_output_tokens_set=True)
        .with_model("ollama", "hf.co/org/model:Q4", reasoning="on")
        .with_model("ollama", "qwen3.5:4b", max_output_tokens=None, max_output_tokens_set=True)
    )


def test_default_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert settings.default_settings_path({"XDG_CONFIG_HOME": "/cfg"}) == Path(
        "/cfg/neurobrau/settings.json"
    )
    expected = tmp_path / ".config" / "neurobrau" / "settings.json"
    assert settings.default_settings_path({"XDG_CONFIG_HOME": "relative"}) == expected
    assert settings.default_settings_path({}) == expected


def test_documented_json_format(store: SettingsStore) -> None:
    assert store.save(sample()) is None

    assert json.loads(store.path.read_text()) == {
        "schema_version": 1,
        "answer_style": "brief",
        "providers": {
            "ollama": {"models": {
                "hf.co/org/model:Q4": {"reasoning": "on"},
                "qwen3.5:4b": {"max_output_tokens": None},
            }},
            "openai": {"models": {"gpt-5": {"reasoning": "low", "max_output_tokens": 1024}}},
        },
    }


def test_round_trip(store: SettingsStore) -> None:
    store.save(sample())

    loaded, warnings = SettingsStore(store.path).load()

    assert warnings == []
    assert loaded.answer_style == "brief"
    assert loaded.for_model("openai", "gpt-5") == ModelPreferences("low", 1024, True)
    assert loaded.for_model("ollama", "hf.co/org/model:Q4").reasoning == "on"
    assert loaded.for_model("ollama", "qwen3.5:4b") == ModelPreferences(None, None, True)
    assert loaded.for_model("ollama", "other") == ModelPreferences()


def test_missing_file_gives_defaults(store: SettingsStore) -> None:
    assert store.load() == (UserSettings(), [])
    assert not store.path.exists()


@pytest.mark.parametrize("content", [b"{", b"[]", b'{"schema_version": 2}', b"\xff"])
def test_invalid_file_is_reported_and_never_overwritten(
    store: SettingsStore, content: bytes
) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(content)

    loaded, warnings = store.load()
    save_warning = store.save(sample())

    assert loaded == UserSettings()
    assert "Ignoring invalid settings file" in warnings[0]
    assert save_warning is not None and "not saved" in save_warning
    assert store.path.read_bytes() == content


def test_invalid_values_are_skipped_individually(store: SettingsStore) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({
        "schema_version": 1,
        "answer_style": "poetic",
        "providers": {
            "openai": {"models": {
                "good": {"reasoning": "low"},
                "bad-tokens": {"max_output_tokens": -5},
                "bad-bool": {"max_output_tokens": True},
                "bad-reasoning": {"reasoning": 3},
            }},
            "ollama": "nope",
        },
        "future": True,
    }))

    loaded, warnings = store.load()

    assert loaded.answer_style == "normal"
    assert set(loaded.models) == {("openai", "good")}
    assert len(warnings) == 5


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_permissions(store: SettingsStore) -> None:
    old = os.umask(0o022)
    try:
        store.save(sample())
    finally:
        os.umask(old)

    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


def test_no_credentials_are_written(store: SettingsStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")

    store.save(sample())

    assert "sk-test-secret" not in store.path.read_text()
    assert set(json.loads(store.path.read_text())) == {"schema_version", "answer_style", "providers"}


def test_save_error_is_reported(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")

    warning = SettingsStore(blocker / "neurobrau" / "settings.json").save(sample())

    assert warning is not None and "Could not save settings" in warning


def test_style_instructions_are_distinct() -> None:
    assert settings.STYLE_INSTRUCTIONS["normal"] is None
    brief, detailed = settings.STYLE_INSTRUCTIONS["brief"], settings.STYLE_INSTRUCTIONS["detailed"]
    assert "brief" in brief.lower() and "example" in detailed.lower()
    assert "reason" not in brief.lower()  # style is not reasoning effort
