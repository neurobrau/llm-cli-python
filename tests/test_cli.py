"""Tests for the command-line interface."""

import json
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from neurobrau_cli import cli
from neurobrau_cli.cli import main
from neurobrau_cli.providers.openai_provider import (
    ErrorKind,
    GenerationResult,
    OpenAIProviderError,
    TokenUsage,
)
from neurobrau_cli.ui.tui import Styles

from fakes import FakeScreen


@pytest.fixture
def fake_generate(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the provider call so no real API requests are made."""
    fake = MagicMock(
        return_value=GenerationResult(
            text="Paris.",
            usage=TokenUsage(input_tokens=10, output_tokens=3, total_tokens=13),
        )
    )
    monkeypatch.setattr(cli.openai_provider, "generate_text", fake)
    return fake


def test_help_prints_usage_and_exits_successfully(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "usage: neurobrau" in output
    assert "--version" in output
    assert "--reasoning-effort" in output
    assert "interactive chat" in output
    assert "--plain" in output
    assert "--no-history" in output
    assert "--transcript-on-exit" in output


def test_version_prints_version_and_exits_successfully(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == "neurobrau 0.1.0"


@pytest.fixture
def fake_chat_backend(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Fake client creation and replies for interactive mode."""
    monkeypatch.setattr(cli.openai_provider, "create_client", MagicMock(name="client"))
    fake_reply = MagicMock(return_value=GenerationResult(text="Hello!", usage=None))
    monkeypatch.setattr(cli.openai_provider, "generate_reply", fake_reply)
    return fake_reply


def feed_input(monkeypatch: pytest.MonkeyPatch, *lines: str) -> None:
    """Replace input() with scripted lines followed by EOF."""
    items = iter(lines)

    def fake_input(prompt: str) -> str:
        try:
            return next(items)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)


def test_no_arguments_starts_interactive_chat(
    fake_chat_backend: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    feed_input(monkeypatch)

    assert main([]) == 0
    out = capsys.readouterr().out
    assert "NeuroBräu CLI 0.1.0" in out
    assert "Provider: openai | Model: gpt-6-luna" in out
    fake_chat_backend.assert_not_called()


def test_interactive_mode_uses_model_options(
    fake_chat_backend: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    feed_input(monkeypatch, "Hi", "/exit")

    exit_code = main(
        ["--model", "gpt-custom", "--reasoning-effort", "low", "--max-output-tokens", "32"]
    )

    assert exit_code == 0
    assert "Model: gpt-custom" in capsys.readouterr().out
    fake_chat_backend.assert_called_once()
    args, kwargs = fake_chat_backend.call_args
    assert [(m.role, m.content) for m in args[0]] == [("user", "Hi")]
    assert kwargs["model"] == "gpt-custom"
    assert kwargs["reasoning_effort"] == "low"
    assert kwargs["max_output_tokens"] == 32
    assert kwargs["client"] is cli.openai_provider.create_client.return_value


def test_interactive_mode_with_usage_prints_usage(
    fake_chat_backend: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    feed_input(monkeypatch, "Hi")

    main(["--usage"])

    assert "Token usage: not reported" in capsys.readouterr().err


def test_interactive_mode_without_api_key_exits_before_chat(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_input = MagicMock()
    monkeypatch.setattr("builtins.input", fake_input)

    exit_code = main([])

    assert exit_code == 3
    assert "OPENAI_API_KEY is not set" in capsys.readouterr().err
    fake_input.assert_not_called()


def test_prompt_uses_default_options(
    fake_generate: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["Capital of France?"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == "Paris.\n"
    assert captured.err == ""
    fake_generate.assert_called_once_with(
        prompt="Capital of France?",
        model="gpt-6-luna",
        reasoning_effort="none",
        max_output_tokens=None,
    )


def test_prompt_passes_custom_options(fake_generate: MagicMock) -> None:
    main(
        [
            "--provider", "openai",
            "--model", "gpt-custom",
            "--reasoning-effort", "low",
            "--max-output-tokens", "64",
            "Hi",
        ]
    )

    fake_generate.assert_called_once_with(
        prompt="Hi", model="gpt-custom", reasoning_effort="low", max_output_tokens=64
    )


def test_usage_flag_prints_token_counts_to_stderr(
    fake_generate: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["--usage", "Hi"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == "Paris.\n"
    assert captured.err == "Token usage: input=10, output=3, total=13\n"


def test_usage_flag_when_usage_not_reported(
    fake_generate: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_generate.return_value = GenerationResult(text="Paris.", usage=None)

    main(["--usage", "Hi"])

    assert capsys.readouterr().err == "Token usage: not reported\n"


@pytest.mark.parametrize(
    ("kind", "exit_code"),
    [
        (ErrorKind.API, 1),
        (ErrorKind.MISSING_API_KEY, 3),
        (ErrorKind.AUTHENTICATION, 4),
        (ErrorKind.RATE_LIMIT, 5),
        (ErrorKind.TIMEOUT, 6),
        (ErrorKind.CONNECTION, 7),
    ],
)
def test_provider_errors_map_to_exit_codes(
    fake_generate: MagicMock,
    capsys: pytest.CaptureFixture[str],
    kind: ErrorKind,
    exit_code: int,
) -> None:
    fake_generate.side_effect = OpenAIProviderError("Something went wrong.", kind)

    assert main(["Hi"]) == exit_code
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "neurobrau: error: Something went wrong.\n"


def test_missing_api_key_fails_gracefully(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["Hi"])

    assert exit_code == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "OPENAI_API_KEY is not set" in captured.err


@pytest.mark.parametrize(
    "argv",
    [
        ["--provider", "anthropic", "Hi"],
        ["--reasoning-effort", "extreme", "Hi"],
        ["--max-output-tokens", "0", "Hi"],
        ["--max-output-tokens", "abc", "Hi"],
        ["   "],
        ["first", "second"],
    ],
)
def test_invalid_arguments_exit_with_usage_error(
    fake_generate: MagicMock, argv: list[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(argv)

    assert exc_info.value.code == 2
    fake_generate.assert_not_called()


@pytest.fixture
def fake_tui(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Pretend we run in a real terminal and capture TUI launches."""
    module = MagicMock()
    module.run_tui.return_value = 0
    monkeypatch.setattr(cli, "is_fullscreen_terminal", lambda: True)
    monkeypatch.setattr(cli, "load_tui", lambda: module)
    return module


def test_interactive_terminal_launches_tui(
    fake_chat_backend: MagicMock, fake_tui: MagicMock
) -> None:
    assert main(["--model", "gpt-custom", "--usage"]) == 0

    fake_tui.run_tui.assert_called_once()
    settings, reply_fn, session = fake_tui.run_tui.call_args.args
    assert session.persistent
    assert settings == cli.chat.ChatSettings("openai", "gpt-custom", show_usage=True)
    assert reply_fn.keywords["model"] == "gpt-custom"


def test_plain_flag_uses_line_chat_in_terminal(
    fake_chat_backend: MagicMock,
    fake_tui: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    feed_input(monkeypatch, "/exit")

    assert main(["--plain"]) == 0
    fake_tui.run_tui.assert_not_called()
    assert "Goodbye." in capsys.readouterr().out


def test_one_shot_prompt_never_launches_tui(
    fake_generate: MagicMock, fake_tui: MagicMock
) -> None:
    assert main(["Hi"]) == 0

    fake_tui.run_tui.assert_not_called()
    fake_generate.assert_called_once()


def test_falls_back_to_plain_chat_when_curses_is_unavailable(
    fake_chat_backend: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "is_fullscreen_terminal", lambda: True)
    monkeypatch.setattr(cli, "load_tui", lambda: None)
    feed_input(monkeypatch)

    assert main([]) == 0
    assert "NeuroBräu CLI 0.1.0" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty", "term", "expected"),
    [
        (True, True, "xterm-256color", True),
        (False, True, "xterm-256color", False),
        (True, False, "xterm-256color", False),
        (True, True, "dumb", False),
        (True, True, None, False),
    ],
)
def test_is_fullscreen_terminal(
    monkeypatch: pytest.MonkeyPatch,
    stdin_tty: bool,
    stdout_tty: bool,
    term: str | None,
    expected: bool,
) -> None:
    monkeypatch.setattr(cli.sys, "stdin", MagicMock(isatty=lambda: stdin_tty))
    monkeypatch.setattr(cli.sys, "stdout", MagicMock(isatty=lambda: stdout_tty))
    if term is None:
        monkeypatch.delenv("TERM", raising=False)
    else:
        monkeypatch.setenv("TERM", term)

    assert cli.is_fullscreen_terminal() is expected


def test_load_tui_returns_curses_module() -> None:
    module = cli.load_tui()

    assert module is not None
    assert callable(module.run_tui)


# ----- persistent history -----


def history_files(state_home: Path) -> list[Path]:
    directory = state_home / "neurobrau" / "conversations"
    return sorted(directory.glob("*.json")) if directory.exists() else []


def test_history_persists_across_runs(
    fake_chat_backend: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    isolated_state_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    feed_input(monkeypatch, "Remember this", "/exit")
    main([])
    assert len(history_files(isolated_state_home)) == 1

    feed_input(monkeypatch, "And this")
    main([])

    history = fake_chat_backend.call_args.args[0]
    assert [m.content for m in history] == ["Remember this", "Hello!", "And this"]
    assert "Restored conversation from" in capsys.readouterr().out


def test_no_history_flag_neither_loads_nor_saves(
    fake_chat_backend: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    isolated_state_home: Path,
) -> None:
    feed_input(monkeypatch, "Saved")
    main([])

    feed_input(monkeypatch, "Not saved")
    main(["--no-history"])

    assert [m.content for m in fake_chat_backend.call_args.args[0]] == ["Not saved"]
    restored = json.loads(history_files(isolated_state_home)[0].read_text())
    assert [m["content"] for m in restored["messages"]] == ["Saved", "Hello!"]


def test_one_shot_prompt_does_not_touch_history(
    fake_generate: MagicMock, isolated_state_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["Hi"]) == 0

    assert capsys.readouterr().out == "Paris.\n"
    assert not isolated_state_home.exists()


def test_corrupt_history_does_not_crash_cli(
    fake_chat_backend: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    isolated_state_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory = isolated_state_home / "neurobrau" / "conversations"
    directory.mkdir(parents=True)
    (directory / "bad.json").write_text("{")
    feed_input(monkeypatch, "Hi")

    assert main([]) == 0
    err = capsys.readouterr().err
    assert "warning: Ignoring unreadable conversation file" in err
    assert "Traceback" not in err
    assert (directory / "bad.json").read_text() == "{"


@pytest.fixture
def curses_with_fake_screen(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> dict[str, object]:
    """Run the real TUI on a FakeScreen; record stdout written while "curses" is active."""
    from neurobrau_cli.ui import tui

    record: dict[str, object] = {"keys": []}

    def fake_wrapper(main_fn: Callable[[FakeScreen], int]) -> int:
        record["stdout_before"] = capsys.readouterr().out
        try:
            return main_fn(FakeScreen(25, 80, record["keys"]))  # type: ignore[arg-type]
        finally:
            # The real wrapper calls endwin() here, restoring the terminal.
            record["stdout_during_curses"] = capsys.readouterr().out

    monkeypatch.setattr(tui.curses, "wrapper", fake_wrapper)
    monkeypatch.setattr(tui, "init_styles", Styles)
    monkeypatch.setattr(cli, "is_fullscreen_terminal", lambda: True)
    return record


def type_keys(*messages: str) -> list[str]:
    return [key for message in messages for key in [*message, "\n"]]


def test_transcript_is_printed_after_curses_exits(
    fake_chat_backend: MagicMock,
    curses_with_fake_screen: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    curses_with_fake_screen["keys"] = type_keys("First question", "/exit")

    assert main(["--transcript-on-exit"]) == 0

    assert curses_with_fake_screen["stdout_during_curses"] == ""
    assert capsys.readouterr().out == "YOU > First question\n\nAI > Hello!\n"


def test_transcript_includes_restored_messages(
    fake_chat_backend: MagicMock,
    curses_with_fake_screen: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    curses_with_fake_screen["keys"] = type_keys("One", "/exit")
    main([])
    capsys.readouterr()
    curses_with_fake_screen["keys"] = type_keys("Two", "/exit")

    main(["--transcript-on-exit"])

    assert capsys.readouterr().out == (
        "YOU > One\n\nAI > Hello!\n\nYOU > Two\n\nAI > Hello!\n"
    )


def test_transcript_printed_after_ctrl_c(
    fake_chat_backend: MagicMock,
    curses_with_fake_screen: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    curses_with_fake_screen["keys"] = [*type_keys("Hi"), "<CTRL-C>"]

    assert main(["--transcript-on-exit"]) == 130
    assert capsys.readouterr().out == "YOU > Hi\n\nAI > Hello!\n"


def test_no_transcript_without_flag_or_messages(
    fake_chat_backend: MagicMock,
    curses_with_fake_screen: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    curses_with_fake_screen["keys"] = type_keys("Hi", "/exit")
    main(["--no-history"])
    assert capsys.readouterr().out == ""

    curses_with_fake_screen["keys"] = type_keys("/exit")
    main(["--no-history", "--transcript-on-exit"])
    assert capsys.readouterr().out == ""
