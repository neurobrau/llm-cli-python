"""Minimal DOS-style fullscreen chat built on the standard-library `curses` module.

Layout, colors and key bindings are specified in `docs/tui-spec.md`.
The pure parts (layout, wrapping, key handling) are testable with a fake screen.
"""

import curses
import locale
import textwrap
from dataclasses import dataclass, field
from typing import Any, Literal

from neurobrau_cli import __version__
from neurobrau_cli.chat import (
    APP_NAME,
    CLEANUP_CONFIRMATION,
    EXIT_INTERRUPTED,
    EXIT_OK,
    ChatSettings,
    ReplyFn,
    cleanup_prompt,
    finish_cleanup,
    is_confirmed,
    parse_command,
    prepare_cleanup,
    reset_notice,
    restored_notice,
)
from neurobrau_cli.providers.openai_provider import OpenAIProviderError, TokenUsage
from neurobrau_cli.session import ChatSession

MIN_COLS = 40
MIN_ROWS = 12
CHROME_ROWS = 7  # top border, 2 separators, status, input, bottom border, help line

USER_LABEL = "YOU > "
AI_LABEL = "AI > "
HELP_LINE = " /help  /reset  /exit | PgUp/PgDn Up/Down: scroll | Ctrl+C: quit"
HELP_TEXT = """Commands:
  /help     show this help
  /reset    start a new conversation (the previous one stays saved)
  /cleanup  delete all saved conversations (asks for confirmation)
  /exit     exit the application (the conversation stays saved)
Backslash aliases also work: \\help, \\reset, \\cleanup, \\exit.
Keys: Enter send, Backspace/Delete edit, Left/Right/Home/End move,
Ctrl+U clear input, PgUp/PgDn/Up/Down scroll, Ctrl+D (empty input) or Ctrl+C quit."""

CTRL_A = "\x01"
CTRL_D = "\x04"
CTRL_E = "\x05"
CTRL_U = "\x15"
ENTER_KEYS = ("\n", "\r", curses.KEY_ENTER)
BACKSPACE_KEYS = ("\x7f", "\b", curses.KEY_BACKSPACE)

EntryKind = Literal["user", "ai", "info", "error"]
Key = str | int


@dataclass(frozen=True)
class Styles:
    """curses attributes for each visual role. All zero means monochrome."""

    border: int = 0
    text: int = 0
    user: int = 0
    ai: int = 0
    info: int = 0
    error: int = 0
    waiting: int = 0


@dataclass(frozen=True)
class Entry:
    """One item in the conversation panel."""

    kind: EntryKind
    text: str


@dataclass(frozen=True)
class Segment:
    """A run of text drawn with one style."""

    text: str
    style: str  # attribute name in Styles


Line = list[Segment]


@dataclass(frozen=True)
class Layout:
    """Screen geometry derived from the terminal size."""

    rows: int
    cols: int

    @property
    def inner_width(self) -> int:
        return self.cols - 4  # border and one space of padding on each side

    @property
    def conversation_top(self) -> int:
        return 1

    @property
    def conversation_height(self) -> int:
        return self.rows - CHROME_ROWS

    @property
    def status_separator_row(self) -> int:
        return self.conversation_top + self.conversation_height

    @property
    def status_row(self) -> int:
        return self.status_separator_row + 1

    @property
    def input_separator_row(self) -> int:
        return self.status_row + 1

    @property
    def input_row(self) -> int:
        return self.input_separator_row + 1

    @property
    def bottom_row(self) -> int:
        return self.input_row + 1

    @property
    def help_row(self) -> int:
        return self.rows - 1


def compute_layout(rows: int, cols: int) -> Layout | None:
    """Return the layout for a terminal size, or None if it is too small."""
    if rows < MIN_ROWS or cols < MIN_COLS:
        return None
    return Layout(rows, cols)


def wrap_paragraphs(text: str, width: int) -> list[str]:
    """Wrap text to `width`, keeping explicit line breaks and leading indentation."""
    lines: list[str] = []
    for paragraph in text.expandtabs(4).splitlines() or [""]:
        stripped = paragraph.lstrip(" ")
        indent = " " * min(len(paragraph) - len(stripped), width // 2)
        wrapped = textwrap.wrap(
            stripped,
            width,
            initial_indent=indent,
            subsequent_indent=indent,
            break_on_hyphens=False,
        )
        lines.extend(wrapped or [""])
    return lines


def entry_lines(entry: Entry, width: int) -> list[Line]:
    """Render one entry as styled lines; labels hang over wrapped text."""
    match entry.kind:
        case "user":
            label, label_style, text_style = USER_LABEL, "user", "text"
        case "ai":
            label, label_style, text_style = AI_LABEL, "ai", "text"
        case "error":
            label, label_style, text_style = "", "error", "error"
        case _:
            label, label_style, text_style = "", "info", "info"

    body = wrap_paragraphs(entry.text, max(1, width - len(label)))
    lines: list[Line] = []
    for index, text in enumerate(body):
        prefix = label if index == 0 else " " * len(label)
        line = [Segment(prefix, label_style)] if prefix else []
        lines.append([*line, Segment(text, text_style)])
    return lines


def transcript_lines(entries: list[Entry], width: int) -> list[Line]:
    """Render all entries, separated by blank lines."""
    lines: list[Line] = []
    for index, entry in enumerate(entries):
        if index:
            lines.append([])
        lines.extend(entry_lines(entry, width))
    return lines


def visible_window(total: int, height: int, scroll: int) -> tuple[int, int]:
    """Return the [start, end) line range shown when scrolled `scroll` lines up."""
    end = total - scroll
    return max(0, end - height), end


def format_status_usage(usage: TokenUsage | None) -> str:
    """Compact token usage for the status panel."""
    if usage is None:
        return "Tokens: -"
    counts = [usage.input_tokens, usage.output_tokens, usage.total_tokens]
    shown = ["?" if count is None else str(count) for count in counts]
    return f"Tokens: in {shown[0]} out {shown[1]} total {shown[2]}"


@dataclass
class TuiState:
    """Everything the screen shows, independent of curses."""

    entries: list[Entry] = field(default_factory=list)
    input_text: str = ""
    cursor: int = 0
    scroll: int = 0  # lines scrolled up from the newest message
    usage: TokenUsage | None = None
    waiting: bool = False
    confirming_cleanup: bool = False  # next submitted line answers the cleanup prompt


class TuiApp:
    """Event loop: read a key, update state, redraw."""

    def __init__(
        self,
        screen: Any,
        settings: ChatSettings,
        reply_fn: ReplyFn,
        styles: Styles = Styles(),
        session: ChatSession | None = None,
    ) -> None:
        self.screen = screen
        self.settings = settings
        self.reply_fn = reply_fn
        self.styles = styles
        self.session = session or ChatSession()
        self.state = TuiState()
        self.exit_code: int | None = None
        self.show_restored_session()

    def show_restored_session(self) -> None:
        """Show restored messages, then startup notices, at the bottom of the panel."""
        for message in self.session.messages:
            self.add_entry("user" if message.role == "user" else "ai", message.content)
        for warning in self.session.startup_warnings:
            self.add_entry("error", f"Warning: {warning}")
        notice = restored_notice(self.session)
        if notice:
            self.add_entry("info", notice)

    # ----- main loop -----

    def run(self) -> int:
        """Run until the user exits. Return the process exit code."""
        while self.exit_code is None:
            self.render()
            try:
                key = self.screen.get_wch()
            except KeyboardInterrupt:
                return EXIT_INTERRUPTED
            except curses.error:
                continue  # no input available (e.g. interrupted by a signal)
            self.handle_key(key)
        return self.exit_code

    def handle_key(self, key: Key) -> None:
        """Apply one key press to the state."""
        state = self.state
        if key in ENTER_KEYS:
            self.submit()
        elif key in BACKSPACE_KEYS:
            if state.cursor > 0:
                state.input_text = (
                    state.input_text[: state.cursor - 1] + state.input_text[state.cursor :]
                )
                state.cursor -= 1
        elif key == curses.KEY_DC:
            state.input_text = (
                state.input_text[: state.cursor] + state.input_text[state.cursor + 1 :]
            )
        elif key == curses.KEY_LEFT:
            state.cursor = max(0, state.cursor - 1)
        elif key == curses.KEY_RIGHT:
            state.cursor = min(len(state.input_text), state.cursor + 1)
        elif key in (curses.KEY_HOME, CTRL_A):
            state.cursor = 0
        elif key in (curses.KEY_END, CTRL_E):
            state.cursor = len(state.input_text)
        elif key == CTRL_U:
            state.input_text, state.cursor = "", 0
        elif key == CTRL_D:
            if not state.input_text:
                self.exit_code = EXIT_OK
        elif key == curses.KEY_UP:
            self.scroll_by(1)
        elif key == curses.KEY_DOWN:
            self.scroll_by(-1)
        elif key == curses.KEY_PPAGE:
            self.scroll_by(self.page_size())
        elif key == curses.KEY_NPAGE:
            self.scroll_by(-self.page_size())
        elif isinstance(key, str) and key.isprintable():
            state.input_text = (
                state.input_text[: state.cursor] + key + state.input_text[state.cursor :]
            )
            state.cursor += len(key)
        # Other keys (including KEY_RESIZE) only trigger a redraw.

    # ----- actions -----

    def submit(self) -> None:
        """Handle the current input line as a command or a message."""
        text = self.state.input_text.strip()
        self.state.input_text, self.state.cursor = "", 0
        if not text:
            return

        if self.state.confirming_cleanup:
            self.confirm_cleanup(text)
        elif (command := parse_command(text)) is not None:
            self.run_command(command)
        else:
            self.send(text)
        self.state.scroll = 0

    def run_command(self, command: str) -> None:
        match command:
            case "exit":
                self.exit_code = EXIT_OK
            case "help":
                self.add_entry("info", HELP_TEXT)
            case "reset":
                warning = self.session.reset()
                self.clear_panel(reset_notice(self.session))
                if warning:
                    self.add_entry("error", f"Warning: {warning}")
            case "cleanup":
                count, message = prepare_cleanup(self.session)
                if message:
                    self.add_entry("info", message)
                else:
                    self.add_entry("info", cleanup_prompt(self.session, count))
                    self.state.confirming_cleanup = True
            case _:
                self.add_entry("error", f"Unknown command: /{command}. Type /help for commands.")

    def send(self, text: str) -> None:
        """Send a message; show a waiting indicator while the request blocks."""
        self.add_entry("user", text)
        self.state.waiting = True
        self.state.scroll = 0
        self.render()
        try:
            result, save_warning = self.session.send(text, self.reply_fn)
        except OpenAIProviderError as error:
            self.add_entry("error", f"Error: {error}")
        except KeyboardInterrupt:
            self.add_entry("error", "Request cancelled.")
        else:
            self.add_entry("ai", result.text)
            if result.usage is not None:
                self.state.usage = result.usage
            if save_warning:
                self.add_entry("error", f"Warning: {save_warning}")
        finally:
            self.state.waiting = False

    def confirm_cleanup(self, answer: str) -> None:
        """Delete saved conversations only if the user typed the confirmation word."""
        self.state.confirming_cleanup = False
        if not is_confirmed(answer):
            self.add_entry("info", "Cleanup cancelled. Nothing was deleted.")
            return
        message, success = finish_cleanup(self.session)
        if success:
            self.clear_panel(message)
        else:
            self.add_entry("error", f"Error: {message}")

    def clear_panel(self, notice: str) -> None:
        self.state.entries.clear()
        self.state.usage = None
        self.add_entry("info", notice)

    def add_entry(self, kind: EntryKind, text: str) -> None:
        self.state.entries.append(Entry(kind, text))

    def scroll_by(self, delta: int) -> None:
        self.state.scroll = max(0, min(self.max_scroll(), self.state.scroll + delta))

    def page_size(self) -> int:
        layout = self.layout()
        return max(1, layout.conversation_height - 1) if layout else 1

    def max_scroll(self) -> int:
        layout = self.layout()
        if layout is None:
            return 0
        total = len(transcript_lines(self.state.entries, layout.inner_width))
        return max(0, total - layout.conversation_height)

    def layout(self) -> Layout | None:
        rows, cols = self.screen.getmaxyx()
        return compute_layout(rows, cols)

    # ----- rendering -----

    def render(self) -> None:
        self.screen.erase()
        layout = self.layout()
        if layout is None:
            self.render_too_small()
        else:
            self.state.scroll = min(self.state.scroll, self.max_scroll())
            self.render_frame(layout)
            self.render_conversation(layout)
            self.render_status(layout)
            self.render_input(layout)
        self.screen.refresh()

    def render_too_small(self) -> None:
        rows, cols = self.screen.getmaxyx()
        messages = [
            "Terminal too small.",
            f"Need {MIN_COLS}x{MIN_ROWS}, have {cols}x{rows}.",
            "Resize or press Ctrl+C.",
        ]
        for row, message in enumerate(messages[:rows]):
            # Stop one column short so the bottom-right cell is never written.
            self.put(row, 0, message, self.styles.error, cols - 1)

    def render_frame(self, layout: Layout) -> None:
        width = layout.cols
        border = self.styles.border
        title = f"{APP_NAME} {__version__}"
        if self.state.scroll:
            title += f" - scrolled up {self.state.scroll} (PgDn for newer)"
        self.put(0, 0, horizontal_rule(width, title), border, width)
        for row in range(layout.conversation_top, layout.status_separator_row):
            self.put(row, 0, "|", border, width)
            self.put(row, width - 1, "|", border, width)
        self.put(layout.status_separator_row, 0, horizontal_rule(width, "Status"), border, width)
        for row in (layout.status_row, layout.input_row):
            self.put(row, 0, "|", border, width)
            self.put(row, width - 1, "|", border, width)
        self.put(layout.input_separator_row, 0, horizontal_rule(width, "Input"), border, width)
        self.put(layout.bottom_row, 0, horizontal_rule(width, ""), border, width)
        # The last cell of the screen cannot be written without scrolling.
        self.put(layout.help_row, 0, HELP_LINE, border, width - 1)

    def render_conversation(self, layout: Layout) -> None:
        lines = transcript_lines(self.state.entries, layout.inner_width)
        start, end = visible_window(len(lines), layout.conversation_height, self.state.scroll)
        for offset, line in enumerate(lines[start:end]):
            x = 2
            for segment in line:
                attr = getattr(self.styles, segment.style)
                self.put(layout.conversation_top + offset, x, segment.text, attr, layout.cols - 2)
                x += len(segment.text)

    def render_status(self, layout: Layout) -> None:
        if self.state.waiting:
            indicator = "[WAITING FOR RESPONSE...]"
        elif self.state.confirming_cleanup:
            indicator = f"[CONFIRM: TYPE {CLEANUP_CONFIRMATION.upper()}]"
        else:
            indicator = "[READY]"
        details = (
            f"Provider: {self.settings.provider}  Model: {self.settings.model}  "
            f"{format_status_usage(self.state.usage)}"
        )
        width = layout.inner_width
        indicator_x = 2 + max(0, width - len(indicator))
        self.put(layout.status_row, 2, details, self.styles.text, indicator_x - 1)
        highlighted = self.state.waiting or self.state.confirming_cleanup
        style = self.styles.waiting if highlighted else self.styles.text
        self.put(layout.status_row, indicator_x, indicator, style, layout.cols - 2)

    def render_input(self, layout: Layout) -> None:
        state = self.state
        # Scroll horizontally so the cursor (which may sit after the last
        # character) always stays inside the field.
        field_width = max(1, layout.inner_width - len(USER_LABEL))
        start = max(0, state.cursor - field_width + 1)
        visible = state.input_text[start : start + field_width]
        x = 2 + len(USER_LABEL)
        self.put(layout.input_row, 2, USER_LABEL, self.styles.user, layout.cols - 2)
        self.put(layout.input_row, x, visible, self.styles.text, layout.cols - 2)
        try:
            self.screen.move(layout.input_row, x + state.cursor - start)
        except curses.error:
            pass

    def put(self, row: int, col: int, text: str, attr: int, limit: int) -> None:
        """Draw text clipped so it never passes column `limit` (exclusive)."""
        available = limit - col
        if available <= 0 or not text:
            return
        try:
            self.screen.addnstr(row, col, text, available, attr)
        except curses.error:
            pass  # e.g. wide characters overflowing the line; never crash rendering


def horizontal_rule(width: int, title: str) -> str:
    """A `+-[ title ]----+` border line of exactly `width` characters."""
    label = f"[ {title} ]" if title else ""
    inner = ("-" + label).ljust(width - 2, "-")[: width - 2]
    return f"+{inner}+"


def init_styles() -> Styles:
    """Create color pairs on a dark background, or monochrome attributes."""
    if not curses.has_colors():
        bold = curses.A_BOLD
        return Styles(border=bold, user=bold, ai=bold, error=bold, waiting=curses.A_REVERSE)
    pairs = {
        1: curses.COLOR_GREEN,
        2: curses.COLOR_WHITE,
        3: curses.COLOR_BLUE,
        4: curses.COLOR_RED,
        5: curses.COLOR_YELLOW,
    }
    for number, foreground in pairs.items():
        curses.init_pair(number, foreground, curses.COLOR_BLACK)
    bold = curses.A_BOLD
    return Styles(
        border=curses.color_pair(1) | bold,
        text=curses.color_pair(2),
        user=curses.color_pair(3) | bold,
        ai=curses.color_pair(1) | bold,
        info=curses.color_pair(1),
        error=curses.color_pair(4) | bold,
        waiting=curses.color_pair(5) | bold,
    )


def run_tui(settings: ChatSettings, reply_fn: ReplyFn, session: ChatSession | None = None) -> int:
    """Run the fullscreen chat. `curses.wrapper` restores the terminal on any exit."""
    try:
        locale.setlocale(locale.LC_ALL, "")  # needed for non-ASCII input and output
    except locale.Error:
        pass

    def main(screen: Any) -> int:
        styles = init_styles()
        screen.bkgd(" ", styles.text)
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        return TuiApp(screen, settings, reply_fn, styles, session).run()

    try:
        return curses.wrapper(main)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
