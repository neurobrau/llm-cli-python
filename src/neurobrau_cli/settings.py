"""User preferences stored in `$XDG_CONFIG_HOME/neurobrau/settings.json`.

Nonsecret only: answer style and per-model reasoning/max-token choices.
Separate from conversation history (XDG state dir) and never contains
credentials. Format documented in `docs/tui-spec.md` ("Settings file").
"""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from neurobrau_cli.storage import write_atomically

SCHEMA_VERSION = 1
APP_DIR_NAME = "neurobrau"
FILE_NAME = "settings.json"
DIR_MODE = 0o700

ANSWER_STYLES = ("brief", "normal", "detailed")
DEFAULT_ANSWER_STYLE = "normal"
TOKEN_PRESETS = (256, 512, 1024, 2048, 4096)
DEFAULT_CHOICE = "default"  # stored reasoning value: send no reasoning parameter

# System/developer instructions per answer style. "normal" adds none, so the
# model answers in its standard conversational way.
STYLE_INSTRUCTIONS: dict[str, str | None] = {
    "brief": (
        "Answer briefly and concisely. Use a few sentences or a short list. "
        "Leave out background and caveats unless the user asks for them."
    ),
    "normal": None,
    "detailed": (
        "Give a comprehensive, well-structured explanation. Cover the relevant "
        "background and the reasoning behind the answer, and include concrete "
        "examples when they help."
    ),
}


class SettingsError(Exception):
    """A user-facing settings problem (invalid file, unsupported value, I/O)."""


@dataclass(frozen=True)
class ModelPreferences:
    """Per-model choices. None means "not set": built-in defaults apply."""

    reasoning: str | None = None  # provider value, or DEFAULT_CHOICE
    max_output_tokens: int | None = None
    max_output_tokens_set: bool = False  # distinguishes "Default" from "not set"


@dataclass(frozen=True)
class UserSettings:
    answer_style: str = DEFAULT_ANSWER_STYLE
    models: Mapping[tuple[str, str], ModelPreferences] = field(default_factory=dict)

    def for_model(self, provider: str, model: str) -> ModelPreferences:
        return self.models.get((provider, model), ModelPreferences())

    def with_model(self, provider: str, model: str, **changes: Any) -> "UserSettings":
        current = self.for_model(provider, model)
        models = {**self.models, (provider, model): replace(current, **changes)}
        return replace(self, models=models)


def default_settings_path(env: Mapping[str, str] | None = None) -> Path:
    """`$XDG_CONFIG_HOME/neurobrau/settings.json`, or `~/.config/...` (relative ignored)."""
    env = os.environ if env is None else env
    config_home = env.get("XDG_CONFIG_HOME", "")
    base = Path(config_home) if os.path.isabs(config_home) else Path.home() / ".config"
    return base / APP_DIR_NAME / FILE_NAME


# ----- JSON format -----


def to_json(settings: UserSettings) -> dict[str, Any]:
    providers: dict[str, Any] = {}
    for (provider, model), prefs in sorted(settings.models.items()):
        entry: dict[str, Any] = {}
        if prefs.reasoning is not None:
            entry["reasoning"] = prefs.reasoning
        if prefs.max_output_tokens_set:
            entry["max_output_tokens"] = prefs.max_output_tokens
        if entry:
            providers.setdefault(provider, {"models": {}})["models"][model] = entry
    return {
        "schema_version": SCHEMA_VERSION,
        "answer_style": settings.answer_style,
        "providers": providers,
    }


def from_json(data: object) -> tuple[UserSettings, list[str]]:
    """Parse settings. Invalid individual values are skipped with a warning.

    Raises ValueError if the file as a whole is unusable.
    """
    if not isinstance(data, dict):
        raise ValueError("top level must be a JSON object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported schema_version {data.get('schema_version')!r} (expected {SCHEMA_VERSION})"
        )
    warnings: list[str] = []
    style = data.get("answer_style", DEFAULT_ANSWER_STYLE)
    if style not in ANSWER_STYLES:
        warnings.append(f"Ignoring unknown answer_style {style!r}; using {DEFAULT_ANSWER_STYLE}.")
        style = DEFAULT_ANSWER_STYLE

    models: dict[tuple[str, str], ModelPreferences] = {}
    providers = data.get("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("'providers' must be an object")
    for provider, provider_data in providers.items():
        model_map = provider_data.get("models") if isinstance(provider_data, dict) else None
        if not isinstance(model_map, dict):
            warnings.append(f"Ignoring settings for provider {provider!r}: 'models' must be an object.")
            continue
        for model, entry in model_map.items():
            prefs, problem = parse_model_entry(entry)
            if problem:
                warnings.append(f"Ignoring settings for {provider}/{model}: {problem}.")
            else:
                models[(provider, model)] = prefs
    return UserSettings(style, models), warnings


def parse_model_entry(entry: object) -> tuple[ModelPreferences, str | None]:
    if not isinstance(entry, dict):
        return ModelPreferences(), "entry must be an object"
    reasoning = entry.get("reasoning")
    if reasoning is not None and (not isinstance(reasoning, str) or not reasoning):
        return ModelPreferences(), "'reasoning' must be a non-empty string"
    tokens_set = "max_output_tokens" in entry
    tokens = entry.get("max_output_tokens")
    if tokens is not None and (isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0):
        return ModelPreferences(), "'max_output_tokens' must be a positive integer or null"
    return ModelPreferences(reasoning, tokens, tokens_set), None


# ----- file -----


@dataclass
class SettingsStore:
    """Loads and saves settings.json. A file that cannot be parsed is never overwritten."""

    path: Path
    writable: bool = True

    def load(self) -> tuple[UserSettings, list[str]]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return UserSettings(), []
        except OSError as error:
            self.writable = False
            return UserSettings(), [
                f"Could not read settings at {self.path}: {error.strerror or error}. "
                "Using defaults; changes will not be saved."
            ]
        try:
            settings, warnings = from_json(json.loads(raw.decode("utf-8")))
        except (ValueError, RecursionError) as error:
            self.writable = False
            return UserSettings(), [
                f"Ignoring invalid settings file {self.path}: {error}. Using defaults; "
                "changes will not be saved until the file is fixed or removed."
            ]
        return settings, warnings

    def save(self, settings: UserSettings) -> str | None:
        """Write atomically with owner-only permissions. Return a warning instead of raising."""
        if not self.writable:
            return f"Settings not saved: {self.path} could not be read at startup."
        content = json.dumps(to_json(settings), ensure_ascii=False, indent=2) + "\n"
        try:
            self.path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
            if self.path.parent.name == APP_DIR_NAME:
                os.chmod(self.path.parent, DIR_MODE)
            write_atomically(self.path, content)
        except OSError as error:
            return f"Could not save settings at {self.path}: {error.strerror or error}."
        return None
