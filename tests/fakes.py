"""Test doubles shared between test modules."""

import curses
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from neurobrau_cli.ui import tui


@dataclass
class Wait:
    """Script step: report "no key yet" until `predicate()` is true (the UI keeps looping)."""

    predicate: Callable[[], bool]


@dataclass
class Do:
    """Script step: run `action()` (e.g. release stream events), then continue."""

    action: Callable[[], object]


STALL_SECONDS = 10.0


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

    def timeout(self, delay: int) -> None:
        self.delay = delay  # -1: blocking reads; otherwise polling while a request runs

    def refresh(self) -> None:
        self.snapshots.append(self.lines())

    def get_wch(self) -> str | int:
        if not hasattr(self, "started"):
            self.started = time.monotonic()
        if time.monotonic() - self.started > STALL_SECONDS:
            raise AssertionError(f"fake screen script stalled; remaining keys: {self.keys[:5]}")
        while self.keys and isinstance(self.keys[0], Do):
            self.keys.pop(0).action()
        if self.keys and isinstance(self.keys[0], Wait):
            if not self.keys[0].predicate():
                time.sleep(0.002)
                raise curses.error("no input")  # like a get_wch timeout
            self.keys.pop(0)
            return self.get_wch()
        if not self.keys:
            if getattr(self, "delay", -1) >= 0:  # polling while a request runs
                time.sleep(0.002)
                raise curses.error("no input")
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


QWEN_TAG = {
    "name": "qwen3.5:4b",
    "model": "qwen3.5:4b",
    "details": {"parameter_size": "4.2B", "quantization_level": "Q4_K_M"},
    "capabilities": ["completion", "vision", "tools", "thinking"],
}
CLOUD_TAG = {
    "name": "glm-4.7:cloud",
    "model": "glm-4.7:cloud",
    "remote_model": "glm-4.7",
    "remote_host": "https://ollama.com:443",
    "capabilities": ["completion", "tools", "thinking"],
}


class FakeOllama:
    """Stands in for the Ollama HTTP API; records every request."""

    def __init__(
        self,
        models: list[dict] | None = None,
        replies: Iterable[object] = (),
        show: dict[str, list[str]] | None = None,
    ) -> None:
        self.models = [QWEN_TAG, CLOUD_TAG] if models is None else models
        self.replies = iter(replies)
        self.show = show or {}
        self.requests: list[tuple[str, str, dict | None, float]] = []
        self.streaming = FakeOllamaStream(self)

    @property
    def paths(self) -> list[str]:
        return [path for _, path, _, _ in self.requests]

    @property
    def chat_payloads(self) -> list[dict]:
        return [payload for _, path, payload, _ in self.requests if path == "/api/chat"]

    def __call__(self, method: str, url: str, payload: dict | None, timeout: float) -> object:
        path = "/" + url.split("/", 3)[3]
        self.requests.append((method, path, payload, timeout))
        if path == "/api/tags":
            return {"models": self.models}
        if path == "/api/show":
            return {"capabilities": self.show.get(payload["model"], ["completion"])}
        if path == "/api/chat":
            reply = next(self.replies, None)
            if isinstance(reply, BaseException):
                raise reply
            if reply is None:
                count = len(self.chat_payloads)
                reply = {
                    "message": {"role": "assistant", "content": f"local reply {count}"},
                    "prompt_eval_count": 10,
                    "eval_count": 5,
                }
            return reply
        raise AssertionError(f"unexpected request {method} {path}")


def ndjson(*objects: object) -> bytes:
    import json

    # Raw UTF-8 like Ollama sends, so multi-byte characters can be split across chunks.
    return b"".join(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n" for obj in objects)


def ollama_chunks(*texts: str, done: dict | None = None, thinking: tuple[str, ...] = ()) -> bytes:
    """An /api/chat NDJSON stream: thinking lines, content lines, then the done line."""
    lines: list[dict] = [{"message": {"role": "assistant", "content": "", "thinking": t}}
                         for t in thinking]
    lines += [{"message": {"role": "assistant", "content": t}} for t in texts]
    final = {"message": {"role": "assistant", "content": ""}, "done": True,
             "done_reason": "stop", "prompt_eval_count": 7, "eval_count": 3,
             "total_duration": 2_500_000_000, "load_duration": 500_000_000,
             "eval_duration": 1_000_000_000}
    lines.append({**final, **(done or {})})
    return ndjson(*lines)


class FakeOllamaStream:
    """Stands in for the streaming transport: yields scripted byte chunks."""

    def __init__(self, fake: "FakeOllama") -> None:
        self.fake = fake
        self.bodies: list[object] = []  # bytes, a list of byte chunks, or an exception
        self.opened = 0
        self.closed = 0

    def __call__(self, method: str, url: str, payload: dict | None, timeout: float):
        path = "/" + url.split("/", 3)[3]
        self.fake.requests.append((method, path, payload, timeout))
        body = self.bodies.pop(0) if self.bodies else ollama_chunks(
            "local ", "stream ", f"{len(self.fake.chat_payloads)}"
        )
        if isinstance(body, BaseException):
            raise body
        return self._chunks([body] if isinstance(body, bytes) else body)

    def _chunks(self, chunks: list):
        self.opened += 1
        try:
            for chunk in chunks:
                if isinstance(chunk, BaseException):
                    raise chunk
                yield chunk
        finally:
            self.closed += 1


class GatedStream:
    """stream_fn whose events are released one by one by the test (deterministic threads)."""

    def __init__(self, *scripts: list[object], open_gate: bool = False) -> None:
        self.scripts = list(scripts)
        self.gate = threading.Semaphore(10_000 if open_gate else 0)
        self.calls: list[list[str]] = []
        self.yielded = 0
        self.closed = 0
        self.closed_event = threading.Event()

    def __call__(self, messages):
        self.calls.append([m.content for m in messages])
        return self._events(self.scripts.pop(0))

    def _events(self, script: list[object]):
        try:
            for event in script:
                if not self.gate.acquire(timeout=STALL_SECONDS):
                    raise AssertionError("stream gate was never released")
                if isinstance(event, BaseException):
                    raise event
                self.yielded += 1
                yield event
        finally:
            self.closed += 1
            self.closed_event.set()

    def release(self, count: int = 1) -> None:
        for _ in range(count):
            self.gate.release()
