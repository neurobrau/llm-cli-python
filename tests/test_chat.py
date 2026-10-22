"""Tests for the interactive chat loop with scripted input and a fake model."""

from collections.abc import Iterable, Sequence
from unittest.mock import MagicMock

import pytest

from neurobrau_cli import chat
from neurobrau_cli.chat import ChatSettings, parse_command, run_chat
from neurobrau_cli.conversation import Message
from neurobrau_cli.providers.openai_provider import (
    ErrorKind,
    GenerationResult,
    OpenAIProviderError,
    TokenUsage,
)

SETTINGS = ChatSettings(provider="openai", model="gpt-test")


def scripted_input(lines: Iterable[str | BaseException]) -> MagicMock:
    """Return an input() replacement yielding lines, raising exceptions, then EOF."""
    items = iter(lines)

    def fake_input(prompt: str) -> str:
        item = next(items, EOFError())
        if isinstance(item, BaseException):
            raise item
        return item

    return MagicMock(side_effect=fake_input)


class FakeModel:
    """Reply function that records the history it receives."""

    def __init__(self, replies: Iterable[str | BaseException] = ()) -> None:
        self.replies = iter(replies)
        self.calls: list[list[Message]] = []

    def __call__(self, messages: Sequence[Message]) -> GenerationResult:
        self.calls.append(list(messages))
        reply = next(self.replies, f"reply {len(self.calls)}")
        if isinstance(reply, BaseException):
            raise reply
        return GenerationResult(
            text=reply, usage=TokenUsage(input_tokens=4, output_tokens=2, total_tokens=6)
        )


def run(
    lines: Iterable[str | BaseException],
    model: FakeModel | None = None,
    settings: ChatSettings = SETTINGS,
) -> tuple[int, FakeModel]:
    model = model or FakeModel()
    exit_code = run_chat(settings, model, input_fn=scripted_input(lines))
    return exit_code, model


def test_startup_banner_shows_name_version_provider_model_and_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code, model = run([])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "NeuroBräu CLI 0.1.0" in out
    assert "Provider: openai | Model: gpt-test" in out
    assert "/help" in out
    assert model.calls == []


def test_prompts_with_you_prefix() -> None:
    input_fn = scripted_input(["Hi"])

    run_chat(SETTINGS, FakeModel(), input_fn=input_fn)

    input_fn.assert_called_with("YOU > ")


def test_response_is_printed_with_ai_prefix(capsys: pytest.CaptureFixture[str]) -> None:
    run(["Hi"], FakeModel(["Hello there!"]))

    assert "AI > Hello there!\n" in capsys.readouterr().out


def test_multiple_turns_send_chronological_history() -> None:
    _, model = run(["First", "Second", "Third"], FakeModel(["A1", "A2", "A3"]))

    assert model.calls == [
        [Message("user", "First")],
        [
            Message("user", "First"),
            Message("assistant", "A1"),
            Message("user", "Second"),
        ],
        [
            Message("user", "First"),
            Message("assistant", "A1"),
            Message("user", "Second"),
            Message("assistant", "A2"),
            Message("user", "Third"),
        ],
    ]


def test_message_whitespace_is_stripped() -> None:
    _, model = run(["  Hi  "])

    assert model.calls == [[Message("user", "Hi")]]


@pytest.mark.parametrize("line", ["", "   ", "\t"])
def test_empty_input_is_ignored(line: str, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code, model = run([line, line])

    assert exit_code == 0
    assert model.calls == []
    assert "AI >" not in capsys.readouterr().out


@pytest.mark.parametrize("command", ["/help", "\\help", "/HELP"])
def test_help_command_lists_commands(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    _, model = run([command])

    out = capsys.readouterr().out
    assert "/reset" in out
    assert "/exit" in out
    assert model.calls == []


@pytest.mark.parametrize("command", ["/reset", "\\reset"])
def test_reset_command_clears_history(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _, model = run(["Before", command, "After"])

    assert model.calls[1] == [Message("user", "After")]
    assert len(model.calls) == 2
    assert "Conversation history cleared." in capsys.readouterr().out


@pytest.mark.parametrize("command", ["/exit", "\\exit"])
def test_exit_command_stops_the_loop(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    input_fn = scripted_input([command, "never read"])
    model = FakeModel()

    exit_code = run_chat(SETTINGS, model, input_fn=input_fn)

    assert exit_code == 0
    assert input_fn.call_count == 1
    assert model.calls == []
    assert "Goodbye." in capsys.readouterr().out


def test_unknown_command_is_not_sent_to_model(capsys: pytest.CaptureFixture[str]) -> None:
    _, model = run(["/frobnicate"])

    assert model.calls == []
    assert "Unknown command: /frobnicate" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/help", "help"),
        ("\\EXIT", "exit"),
        ("/", ""),
        ("hello", None),
        ("/usr/bin is a directory?", None),
        ("what is 1/2", None),
    ],
)
def test_parse_command(text: str, expected: str | None) -> None:
    assert parse_command(text) == expected


def test_failed_request_preserves_prior_history(capsys: pytest.CaptureFixture[str]) -> None:
    failure = OpenAIProviderError("OpenAI rate limit exceeded.", ErrorKind.RATE_LIMIT)
    model = FakeModel(["A1", failure, "A3"])

    exit_code, _ = run(["First", "Second", "Third"], model)

    assert exit_code == 0
    assert model.calls[2] == [
        Message("user", "First"),
        Message("assistant", "A1"),
        Message("user", "Third"),
    ]
    captured = capsys.readouterr()
    assert "error: OpenAI rate limit exceeded." in captured.err
    assert "AI > A3" in captured.out


def test_ctrl_c_during_request_cancels_it_and_keeps_history(
    capsys: pytest.CaptureFixture[str],
) -> None:
    model = FakeModel(["A1", KeyboardInterrupt(), "A3"])

    exit_code, _ = run(["First", "Second", "Third"], model)

    assert exit_code == 0
    assert model.calls[2] == [
        Message("user", "First"),
        Message("assistant", "A1"),
        Message("user", "Third"),
    ]
    assert "Request cancelled." in capsys.readouterr().err


def test_eof_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code, _ = run([EOFError()])

    assert exit_code == 0
    assert "Goodbye." in capsys.readouterr().out


def test_ctrl_c_at_prompt_exits_without_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code, model = run(["Hi", KeyboardInterrupt(), "never sent"])

    assert exit_code == chat.EXIT_INTERRUPTED
    assert len(model.calls) == 1
    captured = capsys.readouterr()
    assert "Goodbye." in captured.out
    assert "Traceback" not in captured.err


def test_usage_is_printed_after_each_response_when_enabled(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["One", "Two"], settings=ChatSettings("openai", "gpt-test", show_usage=True))

    assert capsys.readouterr().err.count("Token usage: input=4, output=2, total=6\n") == 2


def test_usage_is_hidden_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    run(["One"])

    assert "Token usage" not in capsys.readouterr().err
