"""Tests for the CLI bootstrap."""

import pytest

from neurobrau_cli.cli import main


def test_help_prints_usage_and_exits_successfully(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "usage: neurobrau" in output
    assert "--version" in output


def test_version_prints_version_and_exits_successfully(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == "neurobrau 0.1.0"


def test_no_arguments_returns_zero() -> None:
    assert main([]) == 0
