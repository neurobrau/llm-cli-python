"""Keyboard-driven selection menu state (no drawing; see tui.py for rendering)."""

import curses
from dataclasses import dataclass, field
from typing import Literal

ESCAPE = "\x1b"
MenuAction = Literal["select", "cancel"] | None


@dataclass(frozen=True)
class MenuItem:
    label: str
    value: str
    note: str = ""
    current: bool = False  # marked with `*`


@dataclass
class Menu:
    """A list with a highlighted item and a scroll offset."""

    kind: str  # what selecting means, e.g. "model" or "provider"
    title: str
    items: list[MenuItem] = field(default_factory=list)
    index: int = 0  # highlighted item
    top: int = 0  # first visible item

    @classmethod
    def create(cls, kind: str, title: str, items: list[MenuItem]) -> "Menu":
        """Open with the current item highlighted."""
        index = next((i for i, item in enumerate(items) if item.current), 0)
        return cls(kind, title, items, index, index)

    @property
    def selected(self) -> MenuItem:
        return self.items[self.index]

    def move_to(self, index: int, visible: int) -> None:
        """Highlight `index` (clamped) and scroll so it is visible."""
        self.index = max(0, min(len(self.items) - 1, index))
        self.scroll_into_view(visible)

    def scroll_into_view(self, visible: int) -> None:
        visible = max(1, visible)
        if self.index < self.top:
            self.top = self.index
        elif self.index >= self.top + visible:
            self.top = self.index - visible + 1
        self.top = max(0, min(self.top, max(0, len(self.items) - visible)))

    def handle_key(self, key: str | int, visible: int) -> MenuAction:
        """Apply a key. `visible` is how many items fit on screen."""
        if key in ("\n", "\r", curses.KEY_ENTER):
            return "select"
        if key == ESCAPE:
            return "cancel"
        moves = {
            curses.KEY_UP: self.index - 1,
            curses.KEY_DOWN: self.index + 1,
            curses.KEY_PPAGE: self.index - max(1, visible - 1),
            curses.KEY_NPAGE: self.index + max(1, visible - 1),
            curses.KEY_HOME: 0,
            curses.KEY_END: len(self.items) - 1,
        }
        if key in moves:
            self.move_to(moves[key], visible)
        return None
