"""Tests for the command-line interface."""

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


def test_version_prints_version_and_exits_successfully(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == "neurobrau 0.1.0"


def test_no_arguments_returns_zero() -> None:
    assert main([]) == 0


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
        ["--usage"],
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
