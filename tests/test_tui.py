"""Tests for the curses UI using a fake screen and a fake model."""

import curses
from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest

from neurobrau_cli.chat import EXIT_INTERRUPTED, ChatSettings
from neurobrau_cli.conversation import Message
from neurobrau_cli.engine import ChatEngine
from neurobrau_cli.providers.base import Availability, ModelInfo, ProviderError
from neurobrau_cli.providers.openai_provider import (
    ErrorKind,
    GenerationResult,
    OpenAIProviderError,
    TokenUsage,
)
from neurobrau_cli.session import ChatSession
from neurobrau_cli.storage import HistoryStore
from neurobrau_cli.ui import tui
from neurobrau_cli.ui.tui import Entry, Styles, TuiApp

from fakes import FakeScreen

SETTINGS = ChatSettings(provider="openai", model="gpt-test")
STYLES = Styles(border=1, text=2, user=3, ai=4, info=5, error=6, waiting=7)


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
            text=reply, usage=TokenUsage(input_tokens=11, output_tokens=22, total_tokens=33)
        )


def keys_for(*messages: str) -> list[str]:
    """Key presses that type each message and press Enter."""
    keys: list[str] = []
    for message in messages:
        keys.extend(message)
        keys.append("\n")
    return keys


def make_app(
    keys: Iterable[str | int] = (),
    model: FakeModel | None = None,
    rows: int = 25,
    cols: int = 80,
) -> tuple[TuiApp, FakeScreen, FakeModel]:
    screen = FakeScreen(rows, cols, keys)
    model = model or FakeModel()
    return TuiApp(screen, SETTINGS, model, STYLES), screen, model


# ----- startup and layout -----


def test_initial_screen_shows_regions_and_status() -> None:
    app, screen, _ = make_app()
    app.render()

    lines = screen.lines()
    assert len(lines) == 25 and all(len(line) == 80 for line in lines)
    assert lines[0].startswith("+-[ NeuroBräu CLI 0.1.0 ]")
    assert "[ Status ]" in lines[19]
    assert "Provider: openai  Model: gpt-test  Tokens: -" in lines[20]
    assert "[READY]" in lines[20]
    assert "[ Input ]" in lines[21]
    assert lines[22].startswith("| YOU > ")
    assert lines[23] == "+" + "-" * 78 + "+"
    assert "/help" in lines[24]
    assert screen.cursor == (22, 8)


def test_colors_follow_the_spec() -> None:
    app, screen, _ = make_app()
    app.state.entries = [Entry("user", "question"), Entry("ai", "answer"), Entry("error", "boom")]
    app.render()

    assert screen.attr_at("+-[") == STYLES.border
    assert screen.attr_at("YOU > question") == STYLES.user
    assert screen.attr_at("question") == STYLES.text
    assert screen.attr_at("AI > ") == STYLES.ai
    assert screen.attr_at("answer") == STYLES.text
    assert screen.attr_at("boom") == STYLES.error


@pytest.mark.parametrize(("rows", "cols"), [(25, 80), (12, 40), (50, 200), (30, 41)])
def test_layout_adapts_to_terminal_size(rows: int, cols: int) -> None:
    app, screen, _ = make_app(rows=rows, cols=cols)
    app.state.entries = [Entry("ai", "word " * 200)]
    app.render()  # FakeScreen asserts nothing is drawn out of bounds

    layout = tui.compute_layout(rows, cols)
    assert layout is not None
    assert layout.conversation_height == rows - 7
    assert screen.lines()[layout.input_row].startswith("| YOU > ")


@pytest.mark.parametrize(("rows", "cols"), [(11, 80), (25, 39), (3, 10), (1, 1)])
def test_too_small_terminal_shows_message(rows: int, cols: int) -> None:
    app, screen, _ = make_app(rows=rows, cols=cols)
    app.render()

    assert tui.compute_layout(rows, cols) is None
    assert screen.lines()[0].startswith("Terminal too small."[: cols - 1])


def test_resize_redraws_with_new_size() -> None:
    app, screen, _ = make_app(keys=[curses.KEY_RESIZE])
    screen.rows, screen.cols = 30, 100
    app.handle_key(curses.KEY_RESIZE)
    screen.erase()
    app.render()

    assert len(screen.lines()) == 30
    assert screen.lines()[29 - 1] == "+" + "-" * 98 + "+"


# ----- input editing and submission -----


def test_typing_and_enter_sends_message_and_shows_reply() -> None:
    app, screen, model = make_app(keys_for("Hello"), FakeModel(["Hi there!"]))

    assert app.run() == 0
    assert model.calls == [[Message("user", "Hello")]]
    text = screen.snapshots[-1]
    assert any("YOU > Hello" in line for line in text)
    assert any("AI > Hi there!" in line for line in text)
    assert app.state.input_text == ""


def test_backspace_delete_and_cursor_movement_edit_input() -> None:
    app, _, _ = make_app()
    for key in [*"helo", curses.KEY_LEFT, "l", curses.KEY_END, "\x7f", "!",
                curses.KEY_HOME, curses.KEY_DC, "H"]:
        app.handle_key(key)

    assert app.state.input_text == "Hell!"
    assert app.state.cursor == 1


def test_ctrl_u_clears_input() -> None:
    app, _, _ = make_app()
    for key in [*"draft", tui.CTRL_U]:
        app.handle_key(key)

    assert (app.state.input_text, app.state.cursor) == ("", 0)


def test_unicode_input_is_supported() -> None:
    app, _, model = make_app(keys_for("Grüß Gott, NeuroBräu"))

    app.run()

    assert model.calls == [[Message("user", "Grüß Gott, NeuroBräu")]]


def test_long_input_scrolls_horizontally_keeping_cursor_visible() -> None:
    app, screen, _ = make_app(cols=40)
    for char in "x" * 100 + "END":
        app.handle_key(char)
    app.render()

    input_line = screen.lines()[app.layout().input_row]
    assert input_line.rstrip(" |").endswith("END")
    assert screen.cursor[1] < 39


def test_empty_input_is_ignored() -> None:
    app, _, model = make_app(["\n", " ", " ", "\n"])

    app.run()

    assert model.calls == []
    assert app.state.entries == []


def test_waiting_indicator_is_shown_during_request() -> None:
    seen: list[list[str]] = []

    class RecordingModel(FakeModel):
        def __call__(self, messages: Sequence[Message]) -> GenerationResult:
            seen.append(screen.snapshots[-1])
            return super().__call__(messages)

    app, screen, _ = make_app(keys_for("Hi"), RecordingModel())
    app.run()

    assert any("[WAITING FOR RESPONSE...]" in line for line in seen[0])
    assert any("YOU > Hi" in line for line in seen[0])
    assert any("[READY]" in line for line in screen.snapshots[-1])


def test_token_usage_is_refreshed_after_response() -> None:
    app, screen, _ = make_app(keys_for("Hi"))

    app.run()

    assert "Tokens: in 11 out 22 total 33" in screen.text()


# ----- conversation history -----


def test_multiple_turns_send_chronological_history() -> None:
    app, _, model = make_app(keys_for("One", "Two"), FakeModel(["A1", "A2"]))

    app.run()

    assert model.calls[1] == [
        Message("user", "One"),
        Message("assistant", "A1"),
        Message("user", "Two"),
    ]


def test_failed_request_shows_error_and_preserves_history() -> None:
    failure = OpenAIProviderError("OpenAI rate limit exceeded.", ErrorKind.RATE_LIMIT)
    app, screen, model = make_app(keys_for("One", "Two", "Three"),
                                  FakeModel(["A1", failure, "A3"]))

    assert app.run() == 0
    assert model.calls[2] == [
        Message("user", "One"),
        Message("assistant", "A1"),
        Message("user", "Three"),
    ]
    assert "Error: OpenAI rate limit exceeded." in screen.text()
    assert screen.attr_at("Error: OpenAI") == STYLES.error


def test_ctrl_c_during_request_cancels_and_keeps_running() -> None:
    app, screen, model = make_app(keys_for("One", "Two"), FakeModel([KeyboardInterrupt(), "A2"]))

    assert app.run() == 0
    assert model.calls[1] == [Message("user", "Two")]
    assert "Request cancelled." in screen.text()


# ----- commands -----


@pytest.mark.parametrize("command", ["/help", "\\help"])
def test_help_command_shows_commands_without_api_call(command: str) -> None:
    app, screen, model = make_app(keys_for(command))

    app.run()

    assert model.calls == []
    assert "/reset" in screen.text() and "PgUp/PgDn" in screen.text()


@pytest.mark.parametrize("command", ["/reset", "\\reset"])
def test_reset_command_clears_history_screen_and_usage(command: str) -> None:
    app, screen, model = make_app(keys_for("Before", command, "After"))

    app.run()

    assert model.calls[1] == [Message("user", "After")]
    assert "Before" not in screen.text()
    assert "Conversation history cleared." in screen.text()


def test_reset_clears_token_usage() -> None:
    app, screen, _ = make_app(keys_for("Hi", "/reset"))

    app.run()

    assert "Tokens: -" in screen.text()


@pytest.mark.parametrize("command", ["/exit", "\\exit"])
def test_exit_command_stops_without_api_call(command: str) -> None:
    app, screen, model = make_app([*keys_for(command), *keys_for("never sent")])

    assert app.run() == 0
    assert model.calls == []
    assert screen.keys == keys_for("never sent")


def test_unknown_command_is_not_sent() -> None:
    app, screen, model = make_app(keys_for("/frobnicate"))

    app.run()

    assert model.calls == []
    assert "Unknown command: /frobnicate" in screen.text()


# ----- exit keys -----


def test_ctrl_d_on_empty_input_exits() -> None:
    app, _, _ = make_app([tui.CTRL_D, *keys_for("never sent")])

    assert app.run() == 0


def test_ctrl_d_with_text_does_not_exit() -> None:
    app, _, _ = make_app()
    app.handle_key("x")
    app.handle_key(tui.CTRL_D)

    assert app.exit_code is None


def test_ctrl_c_at_prompt_exits_with_interrupt_code() -> None:
    app, _, model = make_app(["<CTRL-C>", *keys_for("never sent")])

    assert app.run() == EXIT_INTERRUPTED
    assert model.calls == []


# ----- wrapping and scrolling -----


def test_long_ai_response_is_wrapped_with_hanging_indent() -> None:
    long_text = " ".join(f"word{i}" for i in range(60))
    app, screen, _ = make_app(keys_for("Hi"), FakeModel([long_text]))

    app.run()

    limit = app.layout().conversation_limit  # the settings panel starts after this column
    lines = [line[: limit + 2] for line in screen.lines()[1:19] if "word" in line]
    assert len(lines) > 3
    assert lines[0].startswith("| AI > word0 ")
    assert all(line.startswith("|      word") for line in lines[1:])
    assert all(line.endswith(" |") for line in lines)
    words = " ".join(line[2:-1].replace("AI > ", "").strip() for line in lines).split()
    assert words == long_text.split()


def test_wrap_paragraphs_keeps_line_breaks_and_indentation() -> None:
    text = "Intro\n\n    def very_long_function_name(argument):\nEnd"

    assert tui.wrap_paragraphs(text, 20) == [
        "Intro",
        "",
        "    def very_long_fu",
        "    nction_name(argu",
        "    ment):",
        "End",
    ]


def test_wrap_paragraphs_breaks_words_longer_than_width() -> None:
    assert tui.wrap_paragraphs("x" * 25, 10) == ["x" * 10, "x" * 10, "x" * 5]


def make_scrollable_app() -> tuple[TuiApp, FakeScreen]:
    app, screen, _ = make_app()
    app.state.entries = [Entry("ai", f"line {i}") for i in range(30)]  # 59 lines with gaps
    app.render()
    return app, screen


def test_conversation_follows_newest_messages() -> None:
    _, screen = make_scrollable_app()

    assert "line 29" in screen.lines()[18]
    assert "line 0" not in screen.text()


def test_page_up_and_down_scroll_conversation() -> None:
    app, screen = make_scrollable_app()

    app.handle_key(curses.KEY_PPAGE)
    app.render()
    assert app.state.scroll == 17
    assert "line 29" not in screen.text()
    assert "scrolled up 17" in screen.lines()[0]

    app.handle_key(curses.KEY_NPAGE)
    app.render()
    assert app.state.scroll == 0
    assert "line 29" in screen.text()


def test_arrow_keys_scroll_by_one_line_and_clamp() -> None:
    app, screen = make_scrollable_app()

    app.handle_key(curses.KEY_DOWN)
    assert app.state.scroll == 0
    for _ in range(100):
        app.handle_key(curses.KEY_UP)
    app.render()

    assert app.state.scroll == 59 - 18
    assert "line 0" in screen.lines()[1]


def test_new_message_scrolls_back_to_bottom() -> None:
    app, screen, _ = make_app(keys_for("Latest"), FakeModel(["Newest reply"]))
    app.state.entries = [Entry("ai", f"line {i}") for i in range(30)]
    app.render()
    app.handle_key(curses.KEY_PPAGE)

    app.run()

    assert app.state.scroll == 0
    assert "Newest reply" in screen.text()


# ----- run_tui terminal handling -----


def test_run_tui_uses_curses_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def fake_wrapper(main: object) -> int:
        calls.append(main)
        return 0

    monkeypatch.setattr(tui.curses, "wrapper", fake_wrapper)

    assert tui.run_tui(SETTINGS, FakeModel()) == 0
    assert len(calls) == 1


def test_run_tui_handles_interrupt_outside_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted_wrapper(main: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(tui.curses, "wrapper", interrupted_wrapper)

    assert tui.run_tui(SETTINGS, FakeModel()) == EXIT_INTERRUPTED


# ----- persistent history -----


def make_persistent_app(
    store: HistoryStore, keys: Iterable[str | int] = (), model: FakeModel | None = None
) -> tuple[TuiApp, FakeScreen, FakeModel]:
    screen = FakeScreen(25, 80, keys)
    model = model or FakeModel()
    session = ChatSession.restore(store)
    return TuiApp(screen, SETTINGS, model, STYLES, session), screen, model


@pytest.fixture
def store(tmp_path: Path) -> HistoryStore:
    return HistoryStore(tmp_path / "conversations")


def test_restored_conversation_is_displayed_and_continued(store: HistoryStore) -> None:
    first, _, _ = make_persistent_app(store, keys_for("My name is Ada."), FakeModel(["Hi Ada."]))
    first.run()

    app, screen, model = make_persistent_app(store, keys_for("What is my name?"))
    app.render()
    text = screen.text()
    assert "YOU > My name is Ada." in text
    assert "AI > Hi Ada." in text
    assert "Restored conversation from" in text

    app.run()
    assert model.calls[0][:2] == [
        Message("user", "My name is Ada."),
        Message("assistant", "Hi Ada."),
    ]


def test_exchange_is_saved_before_exit(store: HistoryStore) -> None:
    app, _, _ = make_persistent_app(store, [*keys_for("Saved?"), "<CTRL-C>"])

    assert app.run() == EXIT_INTERRUPTED
    assert ChatSession.restore(store).messages[0] == Message("user", "Saved?")


def test_failed_request_is_not_saved(store: HistoryStore) -> None:
    failure = OpenAIProviderError("boom", ErrorKind.API)
    app, _, _ = make_persistent_app(store, keys_for("Lost"), FakeModel([failure]))

    app.run()

    assert store.list_paths() == []


def test_reset_preserves_previous_session_on_disk(store: HistoryStore) -> None:
    app, screen, _ = make_persistent_app(store, keys_for("Old", "/reset"))

    app.run()

    assert len(store.list_paths()) == 2
    assert app.state.entries == [
        Entry("info", "Started a new conversation. The previous one stays saved.")
    ]
    assert "Old" not in screen.text()
    assert ChatSession.restore(store).messages == ()


@pytest.mark.parametrize("command", ["/cleanup", "\\cleanup"])
def test_cleanup_requires_confirmation(store: HistoryStore, command: str) -> None:
    make_persistent_app(store, keys_for("Keep?"))[0].run()

    app, screen, model = make_persistent_app(store, keys_for(command))
    app.run()
    seen = screen.snapshots[-1]

    assert any("Delete 1 saved conversation(s)" in line for line in seen)
    assert any("[CONFIRM: TYPE YES]" in line for line in seen)
    assert screen.attr_at("[CONFIRM") == STYLES.waiting
    assert len(store.list_paths()) == 1  # not deleted yet: Ctrl+D ended the script
    assert model.calls == []


def test_cleanup_confirmed_deletes_and_clears_screen(store: HistoryStore) -> None:
    make_persistent_app(store, keys_for("Delete me"))[0].run()

    app, screen, model = make_persistent_app(store, keys_for("/cleanup", "yes"))
    app.run()

    assert store.list_paths() == []
    assert "Deleted 1 saved conversation(s)" in screen.text()
    assert "Delete me" not in screen.text()
    assert "[READY]" in screen.text()
    assert model.calls == []


@pytest.mark.parametrize("answer", ["no", "/exit", "y"])
def test_cleanup_other_answer_cancels(store: HistoryStore, answer: str) -> None:
    make_persistent_app(store, keys_for("Keep me"))[0].run()

    app, screen, model = make_persistent_app(store, keys_for("/cleanup", answer))
    app.run()

    assert len(store.list_paths()) == 1
    assert "Cleanup cancelled. Nothing was deleted." in screen.text()
    assert app.exit_code == 0  # ended by the script's Ctrl+D, not by the "/exit" answer
    assert model.calls == []


def test_cleanup_with_history_disabled() -> None:
    app, screen, _ = make_app(keys_for("/cleanup"))

    app.run()

    assert "history is disabled" in screen.text()


def test_startup_warnings_are_shown(store: HistoryStore) -> None:
    store.ensure_directory()
    (store.directory / "broken.json").write_text("{")

    app, screen, _ = make_persistent_app(store)
    app.render()

    assert "Warning: Ignoring unreadable conversation file" in screen.text()
    assert screen.attr_at("Warning:") == STYLES.error


def test_save_failure_is_shown_after_reply(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    app, screen, _ = make_persistent_app(HistoryStore(blocker / "c"), keys_for("Hi"))

    app.run()

    assert "AI > reply 1" in screen.text()
    assert "Warning: Could not save" in screen.text()


# ----- LLM SETTINGS panel, F2/F3 menus, provider switching -----


class FakeProvider:
    """Provider double with scripted models and availability."""

    def __init__(
        self,
        name: str,
        label: str,
        models: list[str] | Exception = (),
        available: bool = True,
        on_list: object = None,
    ) -> None:
        self.name, self.label = name, label
        self.models = models
        self.available = available
        self.on_list = on_list
        self.calls: list[list[Message]] = []
        self.list_calls = 0

    def generate(self, messages: Sequence[Message], options: object) -> GenerationResult:
        self.calls.append(list(messages))
        return GenerationResult(f"{self.label} says hi", TokenUsage(1, 2, 3))

    def list_models(self, refresh: bool = False) -> list[ModelInfo]:
        self.list_calls += 1
        if callable(self.on_list):
            self.on_list()
        if isinstance(self.models, Exception):
            raise self.models
        return [ModelInfo(model_id, details="1B") for model_id in self.models]

    def check_availability(self) -> Availability:
        return Availability(self.available, "" if self.available else f"{self.label} is down")

    def default_model(self) -> str:
        return self.models[0]


def make_engine_app(
    keys: Iterable[str | int] = (),
    openai_models: list[str] | Exception = ("gpt-a", "gpt-b"),
    ollama: FakeProvider | None = None,
    store: HistoryStore | None = None,
    cols: int = 80,
) -> tuple[TuiApp, FakeScreen, ChatEngine]:
    providers = {
        "openai": FakeProvider("openai", "OpenAI", list(openai_models)
                               if not isinstance(openai_models, Exception) else openai_models),
        "ollama": ollama or FakeProvider("ollama", "Ollama", ["qwen3.5:4b", "llama3.2:3b"]),
    }
    engine = ChatEngine(providers, "openai", "gpt-a", "none", None)
    settings = ChatSettings("openai", "gpt-a", reasoning_effort="none")
    session = ChatSession.restore(store, provider="openai", model="gpt-a")
    screen = FakeScreen(25, cols, keys)
    app = TuiApp(screen, settings, engine.reply, STYLES, session, engine)
    return app, screen, engine


def menu_frame(screen: FakeScreen) -> list[str]:
    """The last drawn frame that showed a menu."""
    return next(frame for frame in reversed(screen.snapshots) if "+-[ SELECT" in "".join(frame))


def panel_text(screen: FakeScreen) -> list[str]:
    return [line[60:78].rstrip() for line in screen.lines()[1:19]]


def test_settings_panel_shows_selection_and_usage() -> None:
    app, screen, _ = make_engine_app(keys_for("Hi"))

    app.run()

    assert screen.lines()[0].endswith("+-[ LLM SETTINGS ]---+")
    assert panel_text(screen)[:13] == [
        "F2 MODEL", " gpt-a",
        "F3 PROVIDER", " OpenAI",
        "F4 REASONING", " none",
        "F5 MAX TOKENS", " default",
        "F6 ANSWER STYLE", " default",
        "TOKENS", " in 1 out 2", " total 3",
    ]
    assert screen.attr_at("F2 MODEL") == STYLES.border
    assert screen.lines()[19][58] == "+"  # panel separator joins the status border


def test_settings_panel_clips_long_model_names() -> None:
    app, screen, _ = make_app()
    app.settings = ChatSettings("openai", "gpt-6-luna-2026-10-01-preview")
    app.render()

    assert " gpt-6-luna-2026-~" in panel_text(screen)  # 18 columns, cut marked with ~


def test_settings_panel_is_hidden_on_narrow_terminals() -> None:
    app, screen, _ = make_engine_app(cols=63)
    app.render()

    assert "LLM SETTINGS" not in screen.text()
    assert app.layout().conversation_width == 63 - 4


def test_f3_menu_lists_providers_with_current_highlighted() -> None:
    app, screen, _ = make_engine_app([curses.KEY_F3])

    app.run()

    menu_rows = [line for line in menu_frame(screen) if "OpenAI" in line or "Ollama" in line]
    assert any("[ SELECT PROVIDER ]" in line for line in menu_frame(screen))
    assert any("| * OpenAI" in line for line in menu_rows)
    assert any("|   Ollama" in line for line in menu_rows)
    app.handle_key(curses.KEY_F3)
    app.render()
    assert screen.attr_at("* OpenAI") == STYLES.selected


def test_switching_provider_starts_new_conversation_without_history() -> None:
    keys = [*keys_for("Secret"), curses.KEY_F3, curses.KEY_DOWN, "\n", *keys_for("Hello")]
    app, screen, engine = make_engine_app(keys)

    app.run()

    openai, ollama = engine.providers["openai"], engine.providers["ollama"]
    assert openai.calls == [[Message("user", "Secret")]]
    assert ollama.calls == [[Message("user", "Hello")]]  # nothing forwarded
    assert (engine.provider, engine.model) == ("ollama", "qwen3.5:4b")
    assert (app.settings.provider, app.settings.model) == ("ollama", "qwen3.5:4b")
    assert (app.session.provider, app.session.model) == ("ollama", "qwen3.5:4b")
    text = screen.text()
    assert "Secret" not in text
    assert " Ollama" in panel_text(screen)
    assert "Provider: ollama  Model: qwen3.5:4b" in text


def test_switch_keeps_previous_conversation_on_disk(store: HistoryStore) -> None:
    keys = [*keys_for("Secret"), curses.KEY_F3, curses.KEY_DOWN, "\n"]
    app, _, _ = make_engine_app(keys, store=store)

    app.run()

    assert len(store.list_paths()) == 2
    assert ChatSession.restore(store, provider="openai").messages[0] == Message("user", "Secret")
    assert ChatSession.restore(store).provider == "ollama"
    assert app.state.entries[0].text.endswith("the previous one stays saved.")


def test_unavailable_provider_is_not_selected() -> None:
    down = FakeProvider("ollama", "Ollama", ["qwen3.5:4b"], available=False)
    app, screen, engine = make_engine_app([curses.KEY_F3, curses.KEY_DOWN, "\n"], ollama=down)

    app.run()

    assert engine.provider == "openai"
    assert "Ollama is not available: Ollama is down" in screen.text()


def test_selecting_current_provider_changes_nothing() -> None:
    app, _, engine = make_engine_app([*keys_for("Keep"), curses.KEY_F3, "\n"])

    app.run()

    assert engine.provider == "openai"
    assert len(app.session.messages) == 2


def test_f2_lists_models_and_switches_model() -> None:
    keys = [*keys_for("Old"), curses.KEY_F2, curses.KEY_END, "\n", *keys_for("New")]
    app, screen, engine = make_engine_app(keys)

    app.run()

    assert engine.model == "gpt-b"
    assert engine.providers["openai"].calls[-1] == [Message("user", "New")]
    assert " gpt-b" in panel_text(screen)


def test_f2_menu_marks_current_model_and_shows_details() -> None:
    app, screen, _ = make_engine_app([curses.KEY_F2])

    app.run()

    menu = "\n".join(menu_frame(screen))
    assert "[ SELECT OPENAI MODEL ]" in menu
    assert "* gpt-a" in menu and "1B" in menu
    assert "1/2 Enter=OK Esc=cancel" in menu


def test_escape_closes_menu_without_changes() -> None:
    app, screen, engine = make_engine_app([curses.KEY_F2, curses.KEY_DOWN, "\x1b"])

    app.run()

    assert engine.model == "gpt-a"
    assert app.state.menu is None
    assert "SELECT" not in screen.text()


def test_menu_keys_do_not_reach_input_line() -> None:
    app, _, engine = make_engine_app([curses.KEY_F2, "x", "y", "\x1b"])

    app.run()

    assert app.state.input_text == ""
    assert engine.providers["openai"].calls == []


def test_long_menu_scrolls_and_counts() -> None:
    models = [f"model-{i:02d}" for i in range(40)]
    app, screen, engine = make_engine_app([curses.KEY_F2, curses.KEY_NPAGE, curses.KEY_NPAGE],
                                          openai_models=models)
    app.run()

    menu = "\n".join(menu_frame(screen))
    visible = app.menu_visible_items()
    assert visible == 16
    assert f"{2 * (visible - 1) + 1}/40" in menu
    assert "model-30" in menu and "model-00" not in menu


def test_model_discovery_failure_is_reported() -> None:
    failure = ProviderError("Could not connect to OpenAI.", ErrorKind.CONNECTION)
    app, screen, engine = make_engine_app([curses.KEY_F2], openai_models=failure)

    app.run()

    assert app.state.menu is None
    assert "Error: Could not connect to OpenAI." in screen.text()
    assert engine.model == "gpt-a"


def test_empty_model_list_is_reported() -> None:
    app, screen, _ = make_engine_app([curses.KEY_F2], openai_models=[])

    app.run()

    assert "No selectable OpenAI models were found." in screen.text()


def test_busy_indicator_during_discovery() -> None:
    seen: list[tuple[list[str], int]] = []

    def record() -> None:
        seen.append((screen.snapshots[-1], screen.attr_at("[LOADING")))

    ollama = FakeProvider("ollama", "Ollama", ["q"], on_list=record)
    app, screen, engine = make_engine_app([curses.KEY_F3, curses.KEY_DOWN, "\n", curses.KEY_F2],
                                          ollama=ollama)
    app.run()

    snapshot, attr = seen[0]
    assert any("[LOADING OLLAMA MODELS...]" in line for line in snapshot)
    assert attr == STYLES.waiting
    assert any("[READY]" in line for line in screen.snapshots[-1])


def test_ctrl_d_closes_menu_then_exits() -> None:
    app, _, _ = make_engine_app([curses.KEY_F2, tui.CTRL_D])

    assert app.run() == 0
    assert app.state.menu is None


def test_ctrl_c_during_discovery_cancels() -> None:
    def interrupt() -> None:
        raise KeyboardInterrupt

    ollama = FakeProvider("ollama", "Ollama", ["q"], on_list=interrupt)
    app, screen, engine = make_engine_app([curses.KEY_F3, curses.KEY_DOWN, "\n"], ollama=ollama)
    ollama.check_availability = lambda: ollama.list_models() and Availability(True)

    assert app.run() == 0
    assert "Cancelled." in screen.text()
    assert engine.provider == "openai"


@pytest.mark.parametrize("key", [curses.KEY_F4, curses.KEY_F5, curses.KEY_F6])
def test_display_only_keys_explain_themselves(key: int) -> None:
    app, screen, engine = make_engine_app([key])

    app.run()

    assert "display-only" in screen.text()


@pytest.mark.parametrize("key", [curses.KEY_F2, curses.KEY_F3])
def test_menus_without_engine_are_unavailable(key: int) -> None:
    app, screen, _ = make_app([key])

    app.run()

    assert "not available" in screen.text()
    assert app.state.menu is None
