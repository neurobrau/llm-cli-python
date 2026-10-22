"""Minimal DOS-style fullscreen chat built on the standard-library `curses` module.

Layout, colors and key bindings are specified in `docs/tui-spec.md`.
The pure parts (layout, wrapping, key handling) are testable with a fake screen.
"""

import curses
import locale
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal, TypeVar

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
from neurobrau_cli.engine import ChatEngine
from neurobrau_cli.providers import provider_label
from neurobrau_cli.providers.base import ProviderError, TokenUsage
from neurobrau_cli.session import ChatSession
from neurobrau_cli.ui.menu import Menu, MenuItem

T = TypeVar("T")

MIN_COLS = 40
MIN_ROWS = 12
CHROME_ROWS = 7  # top border, 2 separators, status, input, bottom border, help line
PANEL_WIDTH = 22  # settings panel: separator, space, 18 content columns, space, border
PANEL_MIN_COLS = 64  # narrower terminals hide the panel
MENU_MIN_WIDTH = 30

USER_LABEL = "YOU > "
AI_LABEL = "AI > "
HELP_LINE = " /help /reset /exit | F2 model F3 provider | PgUp/PgDn scroll | Ctrl+C quit"
HELP_TEXT = """Commands:
  /help     show this help
  /reset    start a new conversation (the previous one stays saved)
  /cleanup  delete all saved conversations (asks for confirmation)
  /exit     exit the application (the conversation stays saved)
Backslash aliases also work: \\help, \\reset, \\cleanup, \\exit.
Keys: Enter send, Backspace/Delete edit, Left/Right/Home/End move,
Ctrl+U clear input, PgUp/PgDn/Up/Down scroll, Ctrl+D (empty input) or Ctrl+C quit.
F2 choose model, F3 choose provider (both start a new conversation).
F4 reasoning, F5 max tokens, F6 answer style are display-only for now."""
DISPLAY_ONLY_KEYS = {
    curses.KEY_F4: "F4 REASONING is display-only for now; set it with --reasoning-effort.",
    curses.KEY_F5: "F5 MAX TOKENS is display-only for now; set it with --max-output-tokens.",
    curses.KEY_F6: "F6 ANSWER STYLE is display-only for now.",
}

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
    selected: int = 0  # highlighted menu item


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
    def panel_x(self) -> int | None:
        """Column of the separator left of the settings panel, or None if hidden."""
        return self.cols - PANEL_WIDTH if self.cols >= PANEL_MIN_COLS else None

    @property
    def conversation_limit(self) -> int:
        """Exclusive right edge for conversation text."""
        return self.cols - 2 if self.panel_x is None else self.panel_x - 1

    @property
    def conversation_width(self) -> int:
        return self.conversation_limit - 2

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
    busy: str | None = None  # e.g. "LOADING MODELS" while a blocking discovery call runs
    menu: Menu | None = None


class TuiApp:
    """Event loop: read a key, update state, redraw."""

    def __init__(
        self,
        screen: Any,
        settings: ChatSettings,
        reply_fn: ReplyFn,
        styles: Styles = Styles(),
        session: ChatSession | None = None,
        engine: ChatEngine | None = None,
    ) -> None:
        self.screen = screen
        self.settings = settings
        self.reply_fn = reply_fn
        self.styles = styles
        self.session = session or ChatSession()
        self.engine = engine  # None: provider/model menus are unavailable
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
        if state.menu is not None:
            self.handle_menu_key(key)
        elif key == curses.KEY_F2:
            self.open_model_menu()
        elif key == curses.KEY_F3:
            self.open_provider_menu()
        elif key in DISPLAY_ONLY_KEYS:
            self.add_entry("info", DISPLAY_ONLY_KEYS[key])
            state.scroll = 0
        elif key in ENTER_KEYS:
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
        except ProviderError as error:
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

    # ----- provider and model selection -----

    def open_provider_menu(self) -> None:
        if self.engine is None:
            self.add_entry("error", "Provider selection is not available.")
            return
        items = [
            MenuItem(provider.label, name, current=name == self.engine.provider)
            for name, provider in self.engine.providers.items()
        ]
        self.state.menu = Menu.create("provider", "SELECT PROVIDER", items)

    def open_model_menu(self) -> None:
        if self.engine is None:
            self.add_entry("error", "Model selection is not available.")
            return
        provider = self.engine.current
        models = self.run_busy(f"LOADING {provider.label.upper()} MODELS", provider.list_models)
        if models is None:
            return
        if not models:
            self.add_entry("error", f"No selectable {provider.label} models were found.")
            return
        items = [
            MenuItem(model.id, model.id, model.details, current=model.id == self.engine.model)
            for model in models
        ]
        self.state.menu = Menu.create("model", f"SELECT {provider.label.upper()} MODEL", items)

    def handle_menu_key(self, key: Key) -> None:
        menu = self.state.menu
        assert menu is not None
        # Ctrl+D closes a menu like Esc; pressed again it exits as usual.
        action = "cancel" if key == CTRL_D else menu.handle_key(key, self.menu_visible_items())
        if action == "cancel":
            self.state.menu = None
        elif action == "select":
            self.state.menu = None
            if menu.kind == "provider":
                self.select_provider(menu.selected.value)
            else:
                self.select_model(menu.selected.value)

    def select_provider(self, name: str) -> None:
        assert self.engine is not None
        if name == self.engine.provider:
            return
        provider = self.engine.providers[name]
        availability = self.run_busy(f"CHECKING {provider.label.upper()}", provider.check_availability)
        if availability is None:
            return
        if not availability.available:
            self.add_entry("error", f"{provider.label} is not available: {availability.message}")
            return
        model = self.run_busy(f"CHECKING {provider.label.upper()}", provider.default_model)
        if model is not None:
            self.switch(name, model)

    def select_model(self, model: str) -> None:
        assert self.engine is not None
        if model != self.engine.model:
            self.switch(self.engine.provider, model)

    def switch(self, provider: str, model: str) -> None:
        """Change provider/model and start a new conversation; history is never carried over."""
        assert self.engine is not None
        had_messages = bool(self.session.messages)
        self.engine.select(provider, model)
        warning = self.session.switch(provider, model)
        self.settings = replace(self.settings, provider=provider, model=model)
        notice = f"Now using {provider_label(provider)} / {model}. Started a new conversation"
        if had_messages and self.session.persistent:
            notice += "; the previous one stays saved."
        elif had_messages:
            notice += "; the previous messages are not sent to the new model."
        else:
            notice += "."
        self.clear_panel(notice)
        if warning:
            self.add_entry("error", f"Warning: {warning}")

    def run_busy(self, label: str, action: Callable[[], T]) -> T | None:
        """Run a blocking call with a status indicator. Errors become panel entries."""
        self.state.busy = label
        self.render()
        try:
            return action()
        except ProviderError as error:
            self.add_entry("error", f"Error: {error}")
        except KeyboardInterrupt:
            self.add_entry("error", "Cancelled.")
        finally:
            self.state.busy = None
            self.state.scroll = 0
        return None

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
        total = len(transcript_lines(self.state.entries, layout.conversation_width))
        return max(0, total - layout.conversation_height)

    def menu_visible_items(self) -> int:
        layout = self.layout()
        return max(1, layout.conversation_height - 2) if layout else 1

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
            self.render_settings_panel(layout)
            self.render_status(layout)
            self.render_input(layout)
            self.render_menu(layout)
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
            title += f" - scrolled up {self.state.scroll} (PgDn)"
        panel_x = layout.panel_x
        if panel_x is None:
            top = horizontal_rule(width, title)
        else:
            top = horizontal_rule(panel_x + 1, title) + horizontal_rule(
                width - panel_x, "LLM SETTINGS"
            )[1:]
        self.put(0, 0, top, border, width)
        for row in range(layout.conversation_top, layout.status_separator_row):
            self.put(row, 0, "|", border, width)
            if panel_x is not None:
                self.put(row, panel_x, "|", border, width)
            self.put(row, width - 1, "|", border, width)
        self.put(layout.status_separator_row, 0, horizontal_rule(width, "Status"), border, width)
        if panel_x is not None:
            self.put(layout.status_separator_row, panel_x, "+", border, width)
        for row in (layout.status_row, layout.input_row):
            self.put(row, 0, "|", border, width)
            self.put(row, width - 1, "|", border, width)
        self.put(layout.input_separator_row, 0, horizontal_rule(width, "Input"), border, width)
        self.put(layout.bottom_row, 0, horizontal_rule(width, ""), border, width)
        # The last cell of the screen cannot be written without scrolling.
        self.put(layout.help_row, 0, HELP_LINE, border, width - 1)

    def render_conversation(self, layout: Layout) -> None:
        lines = transcript_lines(self.state.entries, layout.conversation_width)
        start, end = visible_window(len(lines), layout.conversation_height, self.state.scroll)
        limit = layout.conversation_limit
        for offset, line in enumerate(lines[start:end]):
            x = 2
            for segment in line:
                attr = getattr(self.styles, segment.style)
                self.put(layout.conversation_top + offset, x, segment.text, attr, limit)
                x += len(segment.text)

    def render_settings_panel(self, layout: Layout) -> None:
        if layout.panel_x is None:
            return
        x = layout.panel_x + 2
        rows = settings_panel_lines(self.settings, self.state.usage)
        for offset, (text, style) in enumerate(rows[: layout.conversation_height]):
            attr = getattr(self.styles, style)
            self.put(layout.conversation_top + offset, x, text, attr, layout.cols - 2)

    def render_menu(self, layout: Layout) -> None:
        """Draw the open menu as a bordered box centered over the conversation area."""
        menu = self.state.menu
        if menu is None:
            return
        visible = self.menu_visible_items()
        menu.scroll_into_view(visible)
        shown = menu.items[menu.top : menu.top + visible]
        footer = f"{menu.index + 1}/{len(menu.items)} Enter=OK Esc=cancel"
        longest = max(len(menu_line(item, 0)) for item in menu.items)
        wanted = max(MENU_MIN_WIDTH, longest + 4, len(menu.title) + 6, len(footer) + 6)
        width = min(layout.cols - 2, wanted)
        height = len(shown) + 2
        # Center over the conversation area if the box fits there, else the screen.
        area = layout.panel_x if layout.panel_x and width <= layout.panel_x - 2 else layout.cols
        x0 = (area - width) // 2
        y0 = layout.conversation_top + max(0, (layout.conversation_height - height) // 2)
        border = self.styles.border
        self.put(y0, x0, horizontal_rule(width, menu.title), border, x0 + width)
        for offset, item in enumerate(shown):
            row = y0 + 1 + offset
            style = self.styles.selected if menu.top + offset == menu.index else self.styles.text
            self.put(row, x0, "|", border, x0 + width)
            self.put(row, x0 + 1, f" {menu_line(item, width - 4)} ", style, x0 + width - 1)
            self.put(row, x0 + width - 1, "|", border, x0 + width)
        self.put(y0 + height - 1, x0, horizontal_rule(width, footer), border, x0 + width)

    def render_status(self, layout: Layout) -> None:
        if self.state.waiting:
            indicator = "[WAITING FOR RESPONSE...]"
        elif self.state.busy:
            indicator = f"[{self.state.busy}...]"
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
        highlighted = self.state.waiting or self.state.busy or self.state.confirming_cleanup
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


def fit(text: str, width: int) -> str:
    """Clip text to `width`, marking the cut with `~`."""
    return text if len(text) <= width else text[: max(0, width - 1)] + "~"


PANEL_CONTENT_WIDTH = PANEL_WIDTH - 4


def settings_panel_lines(settings: ChatSettings, usage: TokenUsage | None) -> list[tuple[str, str]]:
    """(text, style) rows of the LLM SETTINGS panel."""
    width = PANEL_CONTENT_WIDTH

    def value(text: str) -> tuple[str, str]:
        return fit(f" {text}", width), "text"

    max_tokens = settings.max_output_tokens
    rows = [
        ("F2 MODEL", "border"), value(settings.model),
        ("F3 PROVIDER", "border"), value(provider_label(settings.provider)),
        ("F4 REASONING", "border"), value(settings.reasoning_effort or "default"),
        ("F5 MAX TOKENS", "border"), value(str(max_tokens) if max_tokens else "default"),
        ("F6 ANSWER STYLE", "border"), value("default"),
        ("TOKENS", "border"),
    ]
    if usage is None:
        rows.append(value("-"))
    else:
        counts = [usage.input_tokens, usage.output_tokens, usage.total_tokens]
        shown = ["?" if count is None else str(count) for count in counts]
        rows += [value(f"in {shown[0]} out {shown[1]}"), value(f"total {shown[2]}")]
    return rows


def menu_line(item: MenuItem, width: int) -> str:
    """`* label    note` padded to `width` (0: natural length)."""
    marker = "*" if item.current else " "
    left = f"{marker} {item.label}"
    if width <= 0:
        return f"{left}  {item.note}" if item.note else left
    note = f"  {item.note}" if item.note and len(left) + len(item.note) + 2 <= width else ""
    return fit(left, width - len(note)).ljust(width - len(note)) + note


def init_styles() -> Styles:
    """Create color pairs on a dark background, or monochrome attributes."""
    if not curses.has_colors():
        bold = curses.A_BOLD
        reverse = curses.A_REVERSE
        return Styles(
            border=bold, user=bold, ai=bold, error=bold, waiting=reverse, selected=reverse
        )
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
        selected=curses.color_pair(2) | curses.A_REVERSE,
    )


def run_tui(
    settings: ChatSettings,
    reply_fn: ReplyFn,
    session: ChatSession | None = None,
    engine: ChatEngine | None = None,
) -> int:
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
        try:
            curses.set_escdelay(25)  # Esc closes menus; don't wait the default 1 s
        except (AttributeError, curses.error):
            pass
        return TuiApp(screen, settings, reply_fn, styles, session, engine).run()

    try:
        return curses.wrapper(main)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
