"""Effective generation settings: CLI flags > saved preferences > built-in defaults.

Every value is checked against what the current model is known to support.
An unsupported CLI flag is an error at startup; an unsupported saved value
falls back to the default with a visible notice.
"""

from dataclasses import dataclass, field

from neurobrau_cli.engine import ChatEngine
from neurobrau_cli.providers.base import Provider, ProviderError, ReasoningSupport
from neurobrau_cli.settings import (
    DEFAULT_CHOICE,
    ModelPreferences,
    SettingsError,
    SettingsStore,
    UserSettings,
)

# OpenAI-style effort names accepted by --reasoning-effort.
EFFORT_ALIASES = {"none": "off", "off": "none"}


@dataclass
class Overrides:
    """Values given explicitly on the command line (None: not given)."""

    reasoning: str | None = None
    max_output_tokens: int | None = None
    answer_style: str | None = None


@dataclass(frozen=True)
class ModelSettings:
    reasoning: str | None
    reasoning_explicit: bool
    max_output_tokens: int | None


def label(value: str | None) -> str:
    """Display name for a reasoning value."""
    if value is None:
        return "Default"
    return {"xhigh": "XHigh", "on": "On", "off": "Off"}.get(value, value.capitalize())


def map_reasoning(support: ReasoningSupport, value: str) -> str | None:
    """Translate a requested value into one the model accepts, or None if impossible.

    `none`/`off` are the same choice; an effort name selects `on` on models
    that only switch thinking on or off.
    """
    if value in support.values:
        return value
    alias = EFFORT_ALIASES.get(value)
    if alias in support.values:
        return alias
    if value not in ("none", "off") and "on" in support.values:
        return "on"
    return None


def describe_support(model: str, support: ReasoningSupport) -> str:
    if not support.values:
        return f"{model} does not support reasoning"
    return f"{model} supports reasoning: {', '.join(label(v) for v in support.values)}"


def resolve_model_settings(
    provider: Provider,
    model: str,
    prefs: ModelPreferences,
    overrides: Overrides,
    strict: bool = False,
    include_default: bool = True,
) -> tuple[ModelSettings, list[str]]:
    """Work out reasoning and max tokens for one provider/model.

    `strict` raises SettingsError for unsupported CLI values instead of
    falling back. `include_default=False` skips the support lookup when
    nothing was chosen (the provider then applies its own default policy).
    """
    notices: list[str] = []
    support_cache: list[ReasoningSupport] = []

    def support() -> ReasoningSupport:
        if not support_cache:
            try:
                support_cache.append(provider.reasoning_support(model))
            except ProviderError as error:
                notices.append(f"Could not check reasoning support for {model}: {error}")
                support_cache.append(ReasoningSupport(known=False))
        return support_cache[0]

    reasoning, explicit = None, False
    decided = False
    if overrides.reasoning is not None:
        current = support()
        mapped = map_reasoning(current, overrides.reasoning) if current.known else overrides.reasoning
        if current.known and not current.values and overrides.reasoning in ("none", "off"):
            decided = True  # "no reasoning" on a model without reasoning: send nothing
        elif mapped is not None:
            reasoning, explicit, decided = mapped, True, True
        else:
            message = f"{describe_support(model, current)}; '{overrides.reasoning}' is not available"
            if strict:
                raise SettingsError(message + ".")
            notices.append(message + "; using the default.")
    if not decided and prefs.reasoning is not None:
        current = support()
        if prefs.reasoning == DEFAULT_CHOICE and current.allows_default:
            decided = True  # the user chose "Default": send no reasoning parameter
        elif current.known and prefs.reasoning in current.values:
            reasoning, explicit, decided = prefs.reasoning, True, True
        else:
            notices.append(
                f"Saved reasoning '{label(prefs.reasoning)}' is not available for {model} "
                f"({describe_support(model, current)}); using {label(current.default)}."
            )
    if not decided and include_default:
        reasoning = support().default

    max_tokens = resolve_max_tokens(provider, model, prefs, overrides, strict, notices)
    return ModelSettings(reasoning, explicit, max_tokens), notices


def output_limit(provider: Provider, model: str) -> int | None:
    try:
        return provider.max_output_limit(model)
    except ProviderError:
        return None  # e.g. model not installed; the request itself reports that


def check_max_tokens(value: int, limit: int | None, model: str) -> str | None:
    """Problem description, or None if `value` is acceptable."""
    if value <= 0:
        return f"Max output tokens must be a positive whole number, not {value}."
    if limit is not None and value > limit:
        return f"{value} exceeds the limit of {limit} output tokens for {model}."
    return None


def resolve_max_tokens(
    provider: Provider,
    model: str,
    prefs: ModelPreferences,
    overrides: Overrides,
    strict: bool,
    notices: list[str],
) -> int | None:
    if overrides.max_output_tokens is not None:
        problem = check_max_tokens(overrides.max_output_tokens, output_limit(provider, model), model)
        if problem is None:
            return overrides.max_output_tokens
        if strict:
            raise SettingsError(problem)
        notices.append(problem + " Using the default.")
    if prefs.max_output_tokens_set and prefs.max_output_tokens is not None:
        problem = check_max_tokens(prefs.max_output_tokens, output_limit(provider, model), model)
        if problem is None:
            return prefs.max_output_tokens
        notices.append(f"Saved setting ignored: {problem} Using the default.")
    return None


@dataclass
class Preferences:
    """Saved settings plus CLI overrides; applies them to an engine and persists changes."""

    store: SettingsStore | None = None
    settings: UserSettings = field(default_factory=UserSettings)
    overrides: Overrides = field(default_factory=Overrides)

    @property
    def answer_style(self) -> str:
        return self.overrides.answer_style or self.settings.answer_style

    def apply(self, engine: ChatEngine, strict: bool = False) -> list[str]:
        """Set the engine's settings for its current provider/model. Return notices."""
        resolved, notices = resolve_model_settings(
            engine.current,
            engine.model,
            self.settings.for_model(engine.provider, engine.model),
            self.overrides,
            strict,
        )
        engine.reasoning_effort = resolved.reasoning
        engine.reasoning_explicit = resolved.reasoning_explicit
        engine.max_output_tokens = resolved.max_output_tokens
        engine.answer_style = self.answer_style
        return notices

    def set_reasoning(self, engine: ChatEngine, value: str | None) -> str | None:
        """None selects "Default" (no parameter). Applies to the next request."""
        engine.reasoning_effort = value
        engine.reasoning_explicit = value is not None
        self.overrides.reasoning = None  # an interactive choice replaces the CLI flag
        return self.save_model(engine, reasoning=value or DEFAULT_CHOICE)

    def set_max_tokens(self, engine: ChatEngine, value: int | None) -> str | None:
        engine.max_output_tokens = value
        self.overrides.max_output_tokens = None
        return self.save_model(engine, max_output_tokens=value, max_output_tokens_set=True)

    def set_answer_style(self, engine: ChatEngine, style: str) -> str | None:
        engine.answer_style = style
        self.overrides.answer_style = None
        self.settings = UserSettings(style, self.settings.models)
        return self.save()

    def save_model(self, engine: ChatEngine, **changes: object) -> str | None:
        self.settings = self.settings.with_model(engine.provider, engine.model, **changes)
        return self.save()

    def save(self) -> str | None:
        return self.store.save(self.settings) if self.store else None
