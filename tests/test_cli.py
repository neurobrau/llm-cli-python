"""Tests for the command-line interface."""

import curses
import json
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from neurobrau_cli import __version__

from neurobrau_cli import cli
from neurobrau_cli.cli import main
from neurobrau_cli.providers.openai_provider import (
    ErrorKind,
    GenerationResult,
    OpenAIProviderError,
    TokenUsage,
)
from neurobrau_cli.ui.tui import Styles

from neurobrau_cli.providers.ollama_provider import OllamaProviderError

from fakes import CLOUD_TAG, QWEN_TAG, FakeOllama, FakeScreen


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
    assert capsys.readouterr().out.strip() == "neurobrau 0.2.0"


def test_installed_metadata_matches_the_single_version_source() -> None:
    import importlib.metadata

    assert __version__ == "0.2.0"
    assert importlib.metadata.version("neurobrau-llm-cli") == __version__


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
    assert f"NeuroBräu CLI {__version__}" in out
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
    engine = fake_tui.run_tui.call_args.kwargs["engine"]
    assert session.persistent
    assert (settings.provider, settings.model, settings.show_usage) == (
        "openai", "gpt-custom", True
    )
    assert reply_fn == engine.reply
    assert (engine.provider, engine.model) == ("openai", "gpt-custom")


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
    assert f"NeuroBräu CLI {__version__}" in capsys.readouterr().out


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


# ----- Ollama and provider selection -----


@pytest.fixture
def fake_ollama(monkeypatch: pytest.MonkeyPatch) -> FakeOllama:
    """Replace Ollama's HTTP transport; no network access."""
    fake = FakeOllama()
    monkeypatch.setattr(cli.ollama_provider, "urllib_transport", fake)
    return fake


def test_one_shot_ollama(fake_ollama: FakeOllama, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--provider", "ollama", "--model", "qwen3.5:4b", "--usage", "Hello"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == "local reply 1\n"
    assert captured.err == "Token usage: input=10, output=5, total=15\n"
    assert fake_ollama.chat_payloads[0]["messages"] == [{"role": "user", "content": "Hello"}]
    assert fake_ollama.chat_payloads[0]["think"] is False


def test_one_shot_ollama_defaults_to_first_local_model(fake_ollama: FakeOllama) -> None:
    fake_ollama.models = [CLOUD_TAG, QWEN_TAG]

    main(["--provider", "ollama", "Hello"])

    assert fake_ollama.chat_payloads[0]["model"] == "qwen3.5:4b"


def test_one_shot_ollama_refuses_cloud_model(
    fake_ollama: FakeOllama, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["--provider", "ollama", "--model", "glm-4.7:cloud", "Hello"])

    assert exit_code == 1
    assert "cloud model" in capsys.readouterr().err
    assert fake_ollama.chat_payloads == []


def test_one_shot_ollama_not_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def down(*args: object) -> object:
        raise OllamaProviderError("Could not connect to Ollama.", ErrorKind.CONNECTION)

    monkeypatch.setattr(cli.ollama_provider, "urllib_transport", down)

    assert main(["--provider", "ollama", "Hello"]) == 7
    assert "Could not connect to Ollama." in capsys.readouterr().err


def test_ollama_needs_no_openai_key(
    fake_ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    feed_input(monkeypatch, "Hi")

    assert main(["--provider", "ollama"]) == 0
    out = capsys.readouterr().out
    assert "Provider: ollama | Model: qwen3.5:4b" in out
    assert "AI > local reply 1" in out


def test_interactive_ollama_history_is_saved_with_provider(
    fake_ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch, isolated_state_home: Path
) -> None:
    feed_input(monkeypatch, "Hi")
    main(["--provider", "ollama", "--model", "qwen3.5:4b"])

    data = json.loads(history_files(isolated_state_home)[0].read_text())
    assert (data["provider"], data["model"]) == ("ollama", "qwen3.5:4b")


def test_plain_launch_resumes_last_provider_and_model(
    fake_ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    feed_input(monkeypatch, "My name is Ada.")
    main(["--provider", "ollama"])
    capsys.readouterr()

    feed_input(monkeypatch, "What is my name?")
    assert main([]) == 0  # no OPENAI_API_KEY needed: the last conversation was Ollama

    assert "Provider: ollama | Model: qwen3.5:4b" in capsys.readouterr().out
    assert [m["content"] for m in fake_ollama.chat_payloads[-1]["messages"]] == [
        "My name is Ada.", "local reply 1", "What is my name?"
    ]


def test_explicit_provider_does_not_receive_other_providers_history(
    fake_ollama: FakeOllama,
    fake_chat_backend: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed_input(monkeypatch, "Ollama secret")
    main(["--provider", "ollama"])

    feed_input(monkeypatch, "Hi OpenAI")
    main(["--provider", "openai"])

    history = fake_chat_backend.call_args.args[0]
    assert [m.content for m in history] == ["Hi OpenAI"]


def test_interactive_ollama_without_models_fails_cleanly(
    fake_ollama: FakeOllama, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_ollama.models = [CLOUD_TAG]

    assert main(["--provider", "ollama"]) == 1
    err = capsys.readouterr().err
    assert "No local Ollama models are installed" in err
    assert "Traceback" not in err


def test_tui_receives_engine_for_ollama(fake_ollama: FakeOllama, fake_tui: MagicMock) -> None:
    main(["--provider", "ollama"])

    engine = fake_tui.run_tui.call_args.kwargs["engine"]
    assert (engine.provider, engine.model) == ("ollama", "qwen3.5:4b")
    assert set(engine.providers) == {"openai", "ollama"}


def test_provider_choices_exclude_unsupported(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--provider", "anthropic", "Hi"])

    assert "invalid choice" in capsys.readouterr().err


# ----- Task 007: settings, --answer-style, overrides -----


def write_settings(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config" / "neurobrau" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, **data}))
    return path


def test_help_lists_answer_style(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])

    assert "--answer-style {brief,normal,detailed}" in capsys.readouterr().out


def test_answer_style_one_shot_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock(return_value=GenerationResult(text="Short."))
    monkeypatch.setattr(cli.openai_provider, "generate_text", fake)

    main(["--answer-style", "brief", "Hi"])

    assert "concise" in fake.call_args.kwargs["instructions"]
    assert fake.call_args.kwargs["reasoning_effort"] == "none"


def test_saved_settings_apply_to_one_shot_and_cli_overrides_them(
    fake_generate: MagicMock, tmp_path: Path
) -> None:
    write_settings(tmp_path, {
        "answer_style": "detailed",
        "providers": {"openai": {"models": {"gpt-5": {"reasoning": "low", "max_output_tokens": 900}}}},
    })

    main(["--model", "gpt-5", "Hi"])
    saved = fake_generate.call_args.kwargs
    main(["--model", "gpt-5", "--reasoning-effort", "high", "--max-output-tokens", "50",
          "--answer-style", "normal", "Hi"])
    overridden = fake_generate.call_args.kwargs

    assert (saved["reasoning_effort"], saved["max_output_tokens"]) == ("low", 900)
    assert "example" in saved["instructions"]
    assert (overridden["reasoning_effort"], overridden["max_output_tokens"]) == ("high", 50)
    assert "instructions" not in overridden


def test_cli_flags_are_not_saved(fake_generate: MagicMock, tmp_path: Path) -> None:
    main(["--answer-style", "brief", "--max-output-tokens", "99", "Hi"])

    assert not (tmp_path / "config").exists()


def test_unsupported_reasoning_for_known_model_is_a_usage_error(
    fake_generate: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--model", "gpt-4o", "--reasoning-effort", "high", "Hi"]) == 2

    assert "gpt-4o does not support reasoning" in capsys.readouterr().err
    fake_generate.assert_not_called()


def test_token_limit_over_known_maximum_is_a_usage_error(
    fake_generate: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--model", "gpt-4o", "--max-output-tokens", "20000", "Hi"]) == 2

    assert "exceeds the limit of 16384" in capsys.readouterr().err


def test_unsupported_saved_setting_falls_back_with_visible_note(
    fake_generate: MagicMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_settings(tmp_path, {"providers": {"openai": {"models": {"gpt-6-luna": {"reasoning": "max"}}}}})

    assert main(["Hi"]) == 0

    assert fake_generate.call_args.kwargs["reasoning_effort"] == "none"
    assert "note: Saved reasoning 'Max' is not available for gpt-6-luna" in capsys.readouterr().err


def test_corrupt_settings_do_not_break_one_shot(
    fake_generate: MagicMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_settings(tmp_path, {})
    path.write_text("{oops")

    assert main(["Hi"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "Paris.\n"
    assert "Ignoring invalid settings file" in captured.err
    assert path.read_text() == "{oops"


def test_ollama_one_shot_style_and_saved_thinking(fake_ollama: FakeOllama, tmp_path: Path) -> None:
    write_settings(tmp_path, {
        "answer_style": "brief",
        "providers": {"ollama": {"models": {"qwen3.5:4b": {"reasoning": "on", "max_output_tokens": 300}}}},
    })

    main(["--provider", "ollama", "--model", "qwen3.5:4b", "Hi"])

    payload = fake_ollama.chat_payloads[0]
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1:] == [{"role": "user", "content": "Hi"}]
    assert payload["think"] is True
    assert payload["options"] == {"num_predict": 300}


def test_ollama_one_shot_default_makes_no_show_request(fake_ollama: FakeOllama) -> None:
    main(["--provider", "ollama", "--model", "qwen3.5:4b", "Hi"])

    assert fake_ollama.paths == ["/api/tags", "/api/chat"]


def test_plain_chat_shows_settings_fallback_and_uses_saved_style(
    fake_ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_settings(tmp_path, {
        "answer_style": "detailed",
        "providers": {"ollama": {"models": {"qwen3.5:4b": {"reasoning": "ultra"}}}},
    })
    feed_input(monkeypatch, "Hi")

    assert main(["--provider", "ollama", "--plain"]) == 0

    err = capsys.readouterr().err
    assert "warning: Saved reasoning 'Ultra' is not available for qwen3.5:4b" in err
    payload = fake_ollama.chat_payloads[0]
    assert payload["think"] is False and "example" in payload["messages"][0]["content"]


def test_tui_receives_preferences(fake_chat_backend: MagicMock, fake_tui: MagicMock) -> None:
    main(["--answer-style", "brief"])

    kwargs = fake_tui.run_tui.call_args.kwargs
    assert kwargs["preferences"].answer_style == "brief"
    assert kwargs["engine"].answer_style == "brief"
    assert fake_tui.run_tui.call_args.args[0].answer_style == "brief"


def test_interactive_cli_value_unsupported_is_usage_error(
    fake_chat_backend: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--model", "gpt-4o", "--reasoning-effort", "low"]) == 2

    assert "gpt-4o does not support reasoning" in capsys.readouterr().err


# ----- Task 007b: restoration across restarts -----


def test_restart_restores_per_provider_conversations(
    fake_chat_backend: MagicMock,
    fake_ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    feed_input(monkeypatch, "OpenAI one")
    main([])  # openai (default)
    feed_input(monkeypatch, "Ollama one")
    main(["--provider", "ollama"])

    feed_input(monkeypatch, "Ollama two")
    main([])  # resumes the active selection: Ollama
    assert [m["content"] for m in fake_ollama.chat_payloads[-1]["messages"]] == [
        "Ollama one", "local reply 1", "Ollama two"
    ]

    feed_input(monkeypatch, "OpenAI two")
    main(["--provider", "openai"])
    assert [m.content for m in fake_chat_backend.call_args.args[0]] == [
        "OpenAI one", "Hello!", "OpenAI two"
    ]
    assert "Restored conversation from" in capsys.readouterr().out


def test_tui_switch_is_resumed_after_restart(
    fake_chat_backend: MagicMock,
    fake_ollama: FakeOllama,
    curses_with_fake_screen: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    curses_with_fake_screen["keys"] = [*type_keys("OpenAI chat"), curses.KEY_F3, curses.KEY_DOWN,
                                       "\n", *type_keys("Ollama chat", "/exit")]
    main([])
    curses_with_fake_screen["keys"] = [curses.KEY_F3, curses.KEY_UP, "\n", *type_keys("/exit")]
    main([])  # resumes Ollama, then switches back to OpenAI (restored, not rewritten)
    capsys.readouterr()

    monkeypatch.setattr(cli, "is_fullscreen_terminal", lambda: False)
    feed_input(monkeypatch, "Plain follow-up")
    main([])  # plain chat after restart: OpenAI was active last

    assert [m.content for m in fake_chat_backend.call_args.args[0]] == [
        "OpenAI chat", "Hello!", "Plain follow-up"
    ]


def test_unknown_provider_in_active_file_falls_back(
    fake_chat_backend: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    isolated_state_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    active = isolated_state_home / "neurobrau" / "active.json"
    active.parent.mkdir(parents=True)
    active.write_text('{"schema_version": 1, "provider": "anthropic", "model": "x"}')
    feed_input(monkeypatch, "Hi")

    assert main([]) == 0
    assert "Provider: openai" in capsys.readouterr().out
