"""Tests for resolving effective settings against model capabilities."""

from pathlib import Path

import pytest

from neurobrau_cli.engine import ChatEngine
from neurobrau_cli.preferences import (
    Overrides,
    Preferences,
    label,
    map_reasoning,
    resolve_model_settings,
)
from neurobrau_cli.providers.base import ErrorKind, ProviderError, ReasoningSupport
from neurobrau_cli.providers.openai_provider import OpenAIProvider
from neurobrau_cli.settings import (
    DEFAULT_CHOICE,
    ModelPreferences,
    SettingsError,
    SettingsStore,
    UserSettings,
)

ON_OFF = ReasoningSupport(("off", "on"), known=True, default="off", allows_default=False)
LEVELS = ReasoningSupport(("low", "medium", "high"), known=True)


class StubProvider:
    name = label_ = "stub"

    def __init__(self, support: ReasoningSupport | Exception, limit: int | None = None) -> None:
        self.support, self.limit, self.support_calls, self.limit_calls = support, limit, 0, 0

    def reasoning_support(self, model: str) -> ReasoningSupport:
        self.support_calls += 1
        if isinstance(self.support, Exception):
            raise self.support
        return self.support

    def max_output_limit(self, model: str) -> int | None:
        self.limit_calls += 1
        return self.limit


def resolve(provider, prefs=ModelPreferences(), overrides=Overrides(), **kwargs):
    return resolve_model_settings(provider, "m", prefs, overrides, **kwargs)


# ----- OpenAI capability table -----


@pytest.mark.parametrize(
    ("model", "reasoning"),
    [("gpt-6-luna", "none"), ("gpt-5", None), ("gpt-4o", None), ("mystery-model", None)],
)
def test_openai_defaults(model: str, reasoning: str | None) -> None:
    resolved, notices = resolve_model_settings(OpenAIProvider(), model, ModelPreferences(), Overrides())

    assert (resolved.reasoning, resolved.reasoning_explicit, notices) == (reasoning, False, [])


def test_openai_unknown_model_passes_explicit_cli_value_through() -> None:
    resolved, _ = resolve_model_settings(
        OpenAIProvider(), "mystery-model", ModelPreferences(), Overrides(reasoning="high"),
        strict=True,
    )

    assert (resolved.reasoning, resolved.reasoning_explicit) == ("high", True)


def test_openai_known_model_rejects_unsupported_cli_value() -> None:
    with pytest.raises(SettingsError, match="gpt-5 supports reasoning: Minimal, Low, Medium, High"):
        resolve_model_settings(
            OpenAIProvider(), "gpt-5", ModelPreferences(), Overrides(reasoning="none"), strict=True
        )


def test_none_on_model_without_reasoning_sends_nothing() -> None:
    resolved, notices = resolve_model_settings(
        OpenAIProvider(), "gpt-4o", ModelPreferences(), Overrides(reasoning="none"), strict=True
    )

    assert (resolved.reasoning, notices) == (None, [])


def test_reasoning_on_model_without_reasoning_is_an_error() -> None:
    with pytest.raises(SettingsError, match="gpt-4o does not support reasoning"):
        resolve_model_settings(
            OpenAIProvider(), "gpt-4o", ModelPreferences(), Overrides(reasoning="low"), strict=True
        )


# ----- mapping and fallbacks -----


@pytest.mark.parametrize(
    ("support", "value", "expected"),
    [
        (ON_OFF, "none", "off"),
        (ON_OFF, "low", "on"),
        (ON_OFF, "on", "on"),
        (LEVELS, "high", "high"),
        (LEVELS, "xhigh", None),
        (ReasoningSupport(("none", "low"), known=True), "off", "none"),
    ],
)
def test_map_reasoning(support: ReasoningSupport, value: str, expected: str | None) -> None:
    assert map_reasoning(support, value) == expected


def test_cli_override_beats_saved_preference() -> None:
    resolved, _ = resolve(StubProvider(LEVELS), ModelPreferences("low"), Overrides(reasoning="high"))

    assert resolved.reasoning == "high"


def test_unsupported_cli_value_falls_back_when_not_strict() -> None:
    resolved, notices = resolve(StubProvider(ON_OFF), overrides=Overrides(reasoning="xhigh"))

    assert resolved.reasoning == "on"  # an effort name means "on" for On/Off models
    resolved, notices = resolve(StubProvider(LEVELS), overrides=Overrides(reasoning="xhigh"))
    assert resolved.reasoning is None
    assert "'xhigh' is not available" in notices[0]


def test_saved_preference_is_restored_as_explicit() -> None:
    resolved, notices = resolve(StubProvider(ON_OFF), ModelPreferences("on"))

    assert (resolved.reasoning, resolved.reasoning_explicit, notices) == ("on", True, [])


def test_unsupported_saved_preference_falls_back_with_notice() -> None:
    resolved, notices = resolve(StubProvider(ON_OFF), ModelPreferences("high"))

    assert (resolved.reasoning, resolved.reasoning_explicit) == ("off", False)
    assert notices == [
        "Saved reasoning 'High' is not available for m (m supports reasoning: Off, On); using Off."
    ]


def test_saved_default_choice_sends_no_parameter() -> None:
    resolved, _ = resolve(StubProvider(LEVELS), ModelPreferences(DEFAULT_CHOICE))

    assert (resolved.reasoning, resolved.reasoning_explicit) == (None, False)


def test_support_lookup_failure_is_reported_not_raised() -> None:
    failure = ProviderError("Could not connect to Ollama.", ErrorKind.CONNECTION)

    resolved, notices = resolve(StubProvider(failure), ModelPreferences("on"))

    assert resolved.reasoning is None
    assert "Could not check reasoning support for m" in notices[0]


def test_nothing_chosen_without_default_lookup_makes_no_request() -> None:
    provider = StubProvider(ON_OFF, limit=100)

    resolved, _ = resolve(provider, include_default=False)

    assert resolved.reasoning is None
    assert (provider.support_calls, provider.limit_calls) == (0, 0)


# ----- max output tokens -----


def test_cli_tokens_over_known_limit_is_an_error() -> None:
    with pytest.raises(SettingsError, match="20000 exceeds the limit of 16384 output tokens"):
        resolve_model_settings(
            OpenAIProvider(), "gpt-4o", ModelPreferences(), Overrides(max_output_tokens=20000),
            strict=True,
        )


def test_saved_tokens_over_limit_fall_back() -> None:
    prefs = ModelPreferences(max_output_tokens=5000, max_output_tokens_set=True)

    resolved, notices = resolve(StubProvider(ON_OFF, limit=4096), prefs)

    assert resolved.max_output_tokens is None
    assert "Saved setting ignored: 5000 exceeds the limit of 4096" in notices[0]


def test_tokens_within_limit_and_unknown_limit() -> None:
    assert resolve(StubProvider(ON_OFF, 4096), overrides=Overrides(max_output_tokens=4096))[0].max_output_tokens == 4096
    assert resolve(StubProvider(ON_OFF, None), overrides=Overrides(max_output_tokens=10**6))[0].max_output_tokens == 10**6


# ----- Preferences: applying and persisting -----


def test_apply_and_change_settings_persist(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "neurobrau" / "settings.json")
    provider = StubProvider(ON_OFF, limit=8192)
    engine = ChatEngine({"stub": provider}, "stub", "m")
    prefs = Preferences(store, UserSettings(), Overrides(reasoning="low", answer_style="brief"))

    assert prefs.apply(engine) == []
    assert (engine.reasoning_effort, engine.answer_style) == ("on", "brief")

    prefs.set_reasoning(engine, "off")
    prefs.set_max_tokens(engine, 2048)
    prefs.set_answer_style(engine, "detailed")

    loaded, _ = SettingsStore(store.path).load()
    assert loaded.for_model("stub", "m") == ModelPreferences("off", 2048, True)
    assert loaded.answer_style == "detailed"
    assert prefs.overrides == Overrides()  # interactive choices replace CLI flags
    options = engine.options()
    assert (options.reasoning_effort, options.reasoning_explicit, options.max_output_tokens) == (
        "off", True, 2048
    )
    assert "example" in options.instructions


def test_choosing_default_reasoning_is_saved_as_default(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    engine = ChatEngine({"stub": StubProvider(LEVELS)}, "stub", "m", reasoning_effort="high")
    prefs = Preferences(store)

    prefs.set_reasoning(engine, None)

    assert (engine.reasoning_effort, engine.reasoning_explicit) == (None, False)
    assert SettingsStore(store.path).load()[0].for_model("stub", "m").reasoning == DEFAULT_CHOICE


def test_labels() -> None:
    assert [label(v) for v in (None, "off", "on", "xhigh", "low")] == [
        "Model default", "Off", "On", "XHigh", "Low"
    ]
