"""Release 0.3.0: packaging entry points, interruption handling, Model default reasoning, F1."""

import curses
import importlib.metadata
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from neurobrau_cli import __version__, cli
from neurobrau_cli.chat import ChatSettings
from neurobrau_cli.cli import main
from neurobrau_cli.providers.base import GenerationResult, StreamCompleted
from neurobrau_cli.session import ChatSession
from neurobrau_cli.ui.tui import HELP_TEXT, Styles, TuiApp

from fakes import FakeOllama, FakeScreen, GatedStream, Wait


# ----- packaging -----


def test_version_is_0_3_0_and_installed_metadata_matches() -> None:
    assert __version__ == "0.3.0"
    assert importlib.metadata.version("neurobrau-llm-cli") == __version__


def test_console_script_entry_point() -> None:
    scripts = importlib.metadata.entry_points(group="console_scripts")

    [entry] = [ep for ep in scripts if ep.name == "neurobrau"]
    assert entry.value == "neurobrau_cli.cli:main"
    assert entry.load() is main


def test_python_dash_m_runs_the_cli() -> None:
    result = subprocess.run([sys.executable, "-m", "neurobrau_cli", "--version"],
                            capture_output=True, text=True, timeout=30)

    assert result.returncode == 0
    assert result.stdout.strip() == f"neurobrau {__version__}"


# ----- consistent interruption and pipe handling -----


def test_ctrl_c_during_blocking_one_shot_has_no_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.openai_provider, "generate_text", MagicMock(side_effect=KeyboardInterrupt))

    assert main(["Hi"]) == 130
    assert capsys.readouterr().err.strip() == "neurobrau: interrupted"


def test_ctrl_c_during_startup_discovery_has_no_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(*args: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.ollama_provider, "urllib_transport", interrupted)

    assert main(["--provider", "ollama"]) == 130
    assert "interrupted" in capsys.readouterr().err


def test_broken_pipe_exits_quietly() -> None:
    script = (
        "import sys; from unittest.mock import MagicMock; from neurobrau_cli import cli; "
        "from neurobrau_cli.providers.base import GenerationResult; "
        "cli.openai_provider.generate_text = MagicMock("
        "return_value=GenerationResult('line\\n' * 200000)); "
        "raise SystemExit(cli.main(['Hi']))"
    )
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "XDG_CONFIG_HOME": "/nonexistent/c",
           "XDG_STATE_HOME": "/nonexistent/s"}
    result = subprocess.run(
        f"{sys.executable} -c \"{script}\" | head -n 1",
        shell=True, capture_output=True, text=True, timeout=60, env=env,
    )

    assert result.stdout == "line\n"
    assert "Traceback" not in result.stderr
    assert "BrokenPipeError" not in result.stderr


# ----- explicit Model default reasoning -----


def test_cli_model_default_sends_no_effort(fake_generate: MagicMock) -> None:
    main(["--reasoning-effort", "model-default", "Hi"])

    assert fake_generate.call_args.kwargs["reasoning_effort"] is None


def test_without_choice_neurobrau_default_is_still_none(fake_generate: MagicMock) -> None:
    main(["Hi"])

    assert fake_generate.call_args.kwargs["reasoning_effort"] == "none"


def test_model_default_on_on_off_ollama_model_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeOllama(show={"qwen3.5:4b": ["completion", "thinking"]})
    monkeypatch.setattr(cli.ollama_provider, "urllib_transport", fake)

    assert main(["--provider", "ollama", "--reasoning-effort", "model-default", "Hi"]) == 2
    assert "has no model-default reasoning choice" in capsys.readouterr().err


def test_saved_default_choice_still_loads(fake_generate: MagicMock, tmp_path) -> None:
    import json

    path = tmp_path / "config" / "neurobrau" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "providers": {
        "openai": {"models": {"gpt-6-luna": {"reasoning": "default"}}}}}))

    main(["Hi"])

    assert fake_generate.call_args.kwargs["reasoning_effort"] is None
    assert json.loads(path.read_text())["providers"]["openai"]["models"]["gpt-6-luna"] == {
        "reasoning": "default"  # unchanged on disk
    }


@pytest.fixture
def fake_generate(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock(return_value=GenerationResult(text="ok"))
    monkeypatch.setattr(cli.openai_provider, "generate_text", fake)
    return fake


# ----- F1 help -----


def test_f1_shows_help_idle_and_while_streaming() -> None:
    stream = GatedStream([StreamCompleted(GenerationResult("ok"))])
    screen = FakeScreen(25, 80)
    app = TuiApp(screen, ChatSettings("ollama", "m"), lambda m: None, Styles(), ChatSession(),
                 stream_fn=stream)
    screen.keys = [curses.KEY_F1, *"Hi", "\n", Wait(lambda: app.request is not None),
                   curses.KEY_F1, Wait(lambda: sum(e.text == HELP_TEXT for e in app.state.entries) == 2),
                   lambda_release(stream), Wait(lambda: app.request is None)]

    app.run()

    assert sum(entry.text == HELP_TEXT for entry in app.state.entries) == 2
    assert "F1 /help" in screen.snapshots[-1][24]


def lambda_release(stream: GatedStream):
    from fakes import Do

    return Do(lambda: stream.release(1))
