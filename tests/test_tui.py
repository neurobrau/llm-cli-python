"""Tests for the curses UI using a fake screen and a fake model."""

import curses
from collections.abc import Iterable, Sequence

import pytest

from neurobrau_cli.chat import EXIT_INTERRUPTED, ChatSettings
from neurobrau_cli.conversation import Message
from neurobrau_cli.providers.openai_provider import (
    ErrorKind,
    GenerationResult,
    OpenAIProviderError,
    TokenUsage,
)
from neurobrau_cli.ui import tui
from neurobrau_cli.ui.tui import Entry, Styles, TuiApp

SETTINGS = ChatSettings(provider="openai", model="gpt-test")
STYLES = Styles(border=1, text=2, user=3, ai=4, info=5, error=6, waiting=7)


class FakeScreen:
    """Records drawing into a character grid; fails on writes a real terminal rejects."""

    def __init__(self, rows: int = 25, cols: int = 80, keys: Iterable[str | int] = ()) -> None:
        self.rows, self.cols = rows, cols
        self.keys = list(keys)
        self.cursor = (0, 0)
        self.snapshots: list[list[str]] = []
        self.erase()

    def getmaxyx(self) -> tuple[int, int]:
        return self.rows, self.cols

    def erase(self) -> None:
        self.chars = [[" "] * self.cols for _ in range(self.rows)]
        self.attrs = [[0] * self.cols for _ in range(self.rows)]

    def addnstr(self, y: int, x: int, text: str, n: int, attr: int = 0) -> None:
        text = text[:n]
        assert 0 <= y < self.rows and 0 <= x, (y, x)
        assert x + len(text) <= self.cols, f"line {y} overflows: {text!r}"
        assert (y, x + len(text)) != (self.rows, self.cols), "wrote the last cell"
        if y == self.rows - 1:
            assert x + len(text) < self.cols, "wrote the bottom-right cell"
        for offset, char in enumerate(text):
            self.chars[y][x + offset] = char
            self.attrs[y][x + offset] = attr

    def move(self, y: int, x: int) -> None:
        assert 0 <= y < self.rows and 0 <= x < self.cols, (y, x)
        self.cursor = (y, x)

    def refresh(self) -> None:
        self.snapshots.append(self.lines())

    def get_wch(self) -> str | int:
        if not self.keys:
            return tui.CTRL_D  # end of script: exit like Ctrl+D on empty input
        key = self.keys.pop(0)
        if key == "<CTRL-C>":
            raise KeyboardInterrupt
        return key

    def lines(self) -> list[str]:
        return ["".join(row) for row in self.chars]

    def text(self) -> str:
        return "\n".join(self.lines())

    def attr_at(self, needle: str) -> int:
        for y, line in enumerate(self.lines()):
            x = line.find(needle)
            if x >= 0:
                return self.attrs[y][x]
        raise AssertionError(f"{needle!r} not on screen")


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

    lines = [line for line in screen.lines()[1:19] if "word" in line]
    assert len(lines) > 3
    assert lines[0].startswith("| AI > word0 ")
    assert all(line.startswith("|      word") for line in lines[1:])
    assert all(line.endswith("|") for line in lines)
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
