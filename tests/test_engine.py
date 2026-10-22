"""Tests for routing replies to the selected provider."""

from collections.abc import Sequence

import pytest

from neurobrau_cli.conversation import Message
from neurobrau_cli.engine import ChatEngine
from neurobrau_cli.providers.base import (
    Availability,
    GenerationOptions,
    GenerationResult,
    ModelInfo,
)


class RecordingProvider:
    def __init__(self, name: str) -> None:
        self.name = self.label = name
        self.calls: list[tuple[list[Message], GenerationOptions]] = []

    def generate(self, messages: Sequence[Message], options: GenerationOptions) -> GenerationResult:
        self.calls.append((list(messages), options))
        return GenerationResult(f"{self.name}:{options.model}")

    def list_models(self, refresh: bool = False) -> list[ModelInfo]:
        return []

    def check_availability(self) -> Availability:
        return Availability(True)

    def default_model(self) -> str:
        return "default"


def test_reply_uses_selected_provider_model_and_options() -> None:
    a, b = RecordingProvider("a"), RecordingProvider("b")
    engine = ChatEngine({"a": a, "b": b}, "a", "m1", "low", 50)

    assert engine.reply([Message("user", "Hi")]).text == "a:m1"
    assert a.calls[0][1] == GenerationOptions("m1", "low", 50)
    assert b.calls == []

    engine.select("b", "m2")
    assert engine.reply([Message("user", "Hi")]).text == "b:m2"


def test_select_unknown_provider_fails() -> None:
    engine = ChatEngine({"a": RecordingProvider("a")}, "a", "m")

    with pytest.raises(KeyError):
        engine.select("anthropic", "x")
