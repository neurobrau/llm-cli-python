"""Test doubles shared between test modules."""

from collections.abc import Iterable

from neurobrau_cli.ui import tui


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

    def bkgd(self, char: str, attr: int = 0) -> None:
        pass

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
