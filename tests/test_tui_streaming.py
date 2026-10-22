"""Tests for live streaming in the curses UI: worker thread, queue, status bar, cancellation."""

import curses
import queue
import re
import threading
import time
from pathlib import Path

import pytest

from neurobrau_cli.chat import EXIT_INTERRUPTED, ChatSettings
from neurobrau_cli.conversation import Message
from neurobrau_cli.providers.base import (
    ErrorKind,
    GenerationResult,
    ProviderError,
    ReasoningDelta,
    StreamCompleted,
    StreamFailed,
    StreamIncomplete,
    TextDelta,
    TokenUsage,
)
from neurobrau_cli.session import ChatSession
from neurobrau_cli.storage import HistoryStore
from neurobrau_cli.ui import status as rt
from neurobrau_cli.ui import tui
from neurobrau_cli.ui.tui import Styles, TuiApp
from neurobrau_cli.ui.worker import QUEUE_SIZE, StreamWorker, WorkerFinished

from fakes import Do, FakeScreen, GatedStream, Wait

SETTINGS = ChatSettings("ollama", "qwen3.5:4b")
STYLES = Styles(border=1, text=2, user=3, ai=4, info=5, error=6, waiting=7, selected=8)
OLLAMA_METRICS = {"total_seconds": 2.5, "load_seconds": 0.5, "eval_seconds": 1.0}


def completed(text: str, usage: TokenUsage | None = TokenUsage(7, 3, 10), **metadata: object) -> StreamCompleted:
    return StreamCompleted(GenerationResult(text, usage), metadata)


def typed(text: str) -> list[str]:
    return [*text, "\n"]


def make_app(
    keys: list[object],
    stream: GatedStream,
    store: HistoryStore | None = None,
    rows: int = 25,
    cols: int = 80,
) -> tuple[TuiApp, FakeScreen]:
    screen = FakeScreen(rows, cols, keys)
    session = ChatSession.restore(store, provider="ollama", model="qwen3.5:4b")
    app = TuiApp(screen, SETTINGS, lambda messages: None, STYLES, session, stream_fn=stream)
    return app, screen


def idle(app: TuiApp) -> Wait:
    return Wait(lambda: app.request is None)


def status_line(frame: list[str]) -> str:
    return frame[20][2:78].rstrip()


def conversation(frame: list[str], limit: int = 57) -> str:
    return "\n".join(line[2:limit].rstrip() for line in frame[1:19])


def run_script(stream: GatedStream, build, **kwargs) -> tuple[TuiApp, FakeScreen, int]:
    """`build(app)` returns the key script; it can refer to the app for Wait steps."""
    app, screen = make_app([], stream, **kwargs)
    screen.keys = build(app)  # FakeScreen copies its key list, so assign the script here
    exit_code = app.run()
    return app, screen, exit_code


# ----- progressive rendering and completion -----


def test_text_appears_progressively_and_completes() -> None:
    stream = GatedStream([TextDelta("Neural "), TextDelta("networks "), TextDelta("learn."),
                          completed("Neural networks learn.", **OLLAMA_METRICS)])
    frames: dict[str, list[str]] = {}

    def build(app: TuiApp) -> list[object]:
        return [*typed("Explain"), Wait(lambda: app.request is not None),
                Do(lambda: frames.setdefault("connecting", app.screen.snapshots[-1])),
                Do(lambda: stream.release(1)), Wait(lambda: app.request.text == "Neural "),
                Do(lambda: frames.setdefault("first", app.screen.snapshots[-1])),
                Do(lambda: stream.release(1)), Wait(lambda: app.request.text == "Neural networks "),
                Do(lambda: frames.setdefault("second", app.screen.snapshots[-1])),
                Do(lambda: stream.release(2)), idle(app)]

    app, screen, exit_code = run_script(stream, build)

    assert exit_code == 0
    assert "YOU > Explain" in conversation(frames["connecting"])
    assert status_line(frames["connecting"]).startswith("CONNECTING | ")
    assert "AI > Neural" in conversation(frames["first"]) and "networks" not in conversation(frames["first"])
    assert status_line(frames["first"]).startswith("GENERATING | ")
    assert "AI > Neural networks" in conversation(frames["second"])
    final = screen.snapshots[-1]
    assert "AI > Neural networks learn." in conversation(final)
    assert status_line(final) == "DONE | 2.50s total | load 0.50s | generation 1.00s | 3.00 tok/s"
    assert app.session.messages == (Message("user", "Explain"),
                                    Message("assistant", "Neural networks learn."))
    assert [line[60:78].rstrip() for line in final[11:14]] == ["TOKENS", " in 7 out 3", " total 10"]
    assert stream.closed == 1


def test_status_shows_elapsed_time_and_cancel_hint() -> None:
    now = [100.0]
    stream = GatedStream([TextDelta("x"), completed("x")])
    app, screen = make_app([], stream)
    app.clock = lambda: now[0]
    screen.keys = [*typed("Hi"), Wait(lambda: app.request is not None),
                   Do(lambda: now.__setitem__(0, 104.24)),
                   Wait(lambda: "4.2s" in status_line(screen.snapshots[-1])),
                   Do(lambda: stream.release(2)), idle(app)]

    app.run()

    assert any(status_line(f) == "CONNECTING | 4.2s elapsed | Ctrl+C Cancel" for f in screen.snapshots)


def test_thinking_shows_status_but_never_text() -> None:
    stream = GatedStream([ReasoningDelta("Let me think about it"), TextDelta("42"), completed("42")])
    seen: list[list[str]] = []

    def build(app: TuiApp) -> list[object]:
        return [*typed("6*7?"), Do(lambda: stream.release(1)),
                Wait(lambda: app.request is not None and app.request.thinking),
                Do(lambda: seen.append(app.screen.snapshots[-1])),
                Do(lambda: stream.release(2)), idle(app)]

    app, screen, _ = run_script(stream, build)

    assert status_line(seen[0]).startswith("GENERATING | Thinking | ")
    assert all("think" not in "".join(frame).lower().replace("thinking |", "") for frame in screen.snapshots)
    assert app.session.messages[-1] == Message("assistant", "42")


def test_openai_shows_client_observed_latency_only() -> None:
    stream = GatedStream([TextDelta("Hi"), completed("Hi", TokenUsage(2, 1, 3), response_id="r1")],
                         open_gate=True)

    _, screen, _ = run_script(stream, lambda app: [*typed("Hello"), idle(app)])

    line = status_line(screen.snapshots[-1])
    assert re.fullmatch(r"DONE \| \d+\.\d\ds observed \| first text after \d+\.\d\ds", line)
    assert "tok/s" not in line  # no client-side estimate presented as a server metric


def test_done_returns_to_ready_after_next_action(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "conversations")
    stream = GatedStream([TextDelta("A"), completed("A")], open_gate=True)
    frames: list[list[str]] = []

    def build(app: TuiApp) -> list[object]:
        return [*typed("Q"), idle(app), Do(lambda: frames.append(app.screen.snapshots[-1])),
                curses.KEY_UP]

    _, screen, _ = run_script(stream, build, store=store)

    assert status_line(frames[0]).startswith("DONE | ")
    assert status_line(screen.snapshots[-1]) == "READY | Conversation saved"


def test_status_bar_never_repeats_provider_model_or_tokens() -> None:
    stream = GatedStream([TextDelta("Hello"), completed("Hello", **OLLAMA_METRICS)], open_gate=True)

    _, screen, _ = run_script(stream, lambda app: [*typed("Hi"), idle(app)])

    for frame in screen.snapshots:
        line = status_line(frame)
        assert not any(word in line for word in ("Ollama", "ollama", "qwen", "Tokens", "in 7", "total 10"))


# ----- long responses, auto-scroll and manual scroll -----


def long_deltas(count: int) -> list[object]:
    words = [TextDelta(f"word{i} ") for i in range(count)]
    return [*words, completed("".join(d.text for d in words))]


def test_long_response_follows_newest_text() -> None:
    stream = GatedStream(long_deltas(300), open_gate=True)

    app, screen, _ = run_script(stream, lambda app: [*typed("Long"), idle(app)])

    view = conversation(screen.snapshots[-1])
    assert "word299" in view and "word0 " not in view
    assert app.state.scroll == 0


def test_manual_scroll_during_streaming_keeps_the_view() -> None:
    stream = GatedStream(long_deltas(300))
    views: list[str] = []

    def build(app: TuiApp) -> list[object]:
        return [*typed("Long"), Do(lambda: stream.release(150)),
                Wait(lambda: app.request is not None and "word149" in app.request.text),
                curses.KEY_PPAGE,
                Wait(lambda: app.state.scroll > 0),
                Do(lambda: views.append(conversation(app.screen.snapshots[-1]))),
                Do(lambda: stream.release(151)), idle(app),
                Do(lambda: views.append(conversation(app.screen.snapshots[-1])))]

    app, _, _ = run_script(stream, build)

    assert views[0] == views[1]  # new text arrived below; the scrolled view did not move
    assert app.state.scroll > 17
    assert app.session.messages[-1].content.endswith("word299 ")


def test_redraws_are_batched() -> None:
    stream = GatedStream(long_deltas(400))

    def build(app: TuiApp) -> list[object]:
        return [*typed("Many"), Wait(lambda: app.request is not None),
                Do(lambda: stream.release(401)), idle(app)]

    app, screen, _ = run_script(stream, build)

    streaming_frames = [f for f in screen.snapshots if "word" in "".join(f)]
    assert len(streaming_frames) < 100  # 400 events, far fewer redraws


# ----- responsiveness and single active request -----


def test_ui_stays_responsive_while_waiting_for_network() -> None:
    stream = GatedStream([TextDelta("ok"), completed("ok")])
    seen: list[tuple[str, str]] = []

    def build(app: TuiApp) -> list[object]:
        return [*typed("Hi"), Wait(lambda: app.request is not None), "a", "b", "c",
                Wait(lambda: app.state.input_text == "abc"),
                Do(lambda: seen.append((app.screen.snapshots[-1][22], status_line(app.screen.snapshots[-1])))),
                Do(lambda: stream.release(2)), idle(app), tui.CTRL_U]

    run_script(stream, build)

    input_row, status = seen[0]
    assert input_row.startswith("| YOU > abc")
    assert status.startswith("CONNECTING | ")


def test_second_request_is_refused_while_streaming() -> None:
    stream = GatedStream([TextDelta("ok"), completed("ok")])

    def build(app: TuiApp) -> list[object]:
        return [*typed("First"), Wait(lambda: app.request is not None), *typed("Second"),
                Wait(lambda: app.state.flash is not None),
                Do(lambda: stream.release(2)), idle(app), tui.CTRL_U]

    app, screen, _ = run_script(stream, build)

    assert stream.calls == [["First"]]
    assert any("WARNING | A response is being generated; wait or press Ctrl+C" in status_line(f)
               for f in screen.snapshots)
    assert app.session.messages == (Message("user", "First"), Message("assistant", "ok"))


@pytest.mark.parametrize("key", [curses.KEY_F2, curses.KEY_F3, curses.KEY_F4, curses.KEY_F5, curses.KEY_F6])
def test_settings_keys_are_blocked_while_streaming(key: int) -> None:
    stream = GatedStream([completed("ok")])

    def build(app: TuiApp) -> list[object]:
        return [*typed("Hi"), Wait(lambda: app.request is not None), key,
                Do(lambda: stream.release(1)), idle(app)]

    app, screen, _ = run_script(stream, build)

    assert app.state.menu is None
    assert any("Settings cannot change while a response is generated" in status_line(f)
               for f in screen.snapshots)


def test_messages_stream_one_after_another_with_history() -> None:
    stream = GatedStream([TextDelta("A1"), completed("A1")], [TextDelta("A2"), completed("A2")],
                         open_gate=True)

    app, _, _ = run_script(stream, lambda app: [*typed("Q1"), idle(app), *typed("Q2"), idle(app)])

    assert stream.calls == [["Q1"], ["Q1", "A1", "Q2"]]
    assert [m.content for m in app.session.messages] == ["Q1", "A1", "Q2", "A2"]


# ----- cancellation -----


def test_ctrl_c_cancels_and_app_stays_usable(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "conversations")
    stream = GatedStream([TextDelta("Partial "), TextDelta("LATE"), completed("Partial LATE")],
                         [TextDelta("Fresh"), completed("Fresh")])

    def build(app: TuiApp) -> list[object]:
        return [*typed("First"), Do(lambda: stream.release(1)),
                Wait(lambda: app.request is not None and app.request.text == "Partial "),
                "<CTRL-C>",
                Do(lambda: stream.release(1)),  # the worker gets the next chunk, sees the flag
                Wait(lambda: stream.closed == 1),
                Wait(lambda: app.closing is None),
                *typed("Second"), Do(lambda: stream.release(2)), idle(app)]

    app, screen, exit_code = run_script(stream, build, store=store)

    assert exit_code == 0
    text = "\n".join(e.text for e in app.state.entries)
    assert "Cancelled. The partial answer above was not saved." in text
    assert "LATE" not in "".join("".join(frame) for frame in screen.snapshots)
    assert any(status_line(f) == "CANCELLED | Partial response not saved" for f in screen.snapshots)
    assert [m.content for m in app.session.messages] == ["Second", "Fresh"]  # nothing from the cancelled one
    saved = ChatSession.restore(store)
    assert [m.content for m in saved.messages] == ["Second", "Fresh"]
    assert stream.calls[1] == ["Second"]


def test_cancel_before_any_text() -> None:
    stream = GatedStream([TextDelta("never"), completed("never")])

    def build(app: TuiApp) -> list[object]:
        return [*typed("Hi"), Wait(lambda: app.request is not None), "<CTRL-C>",
                Do(lambda: stream.release(1)), Wait(lambda: stream.closed == 1)]

    app, screen, _ = run_script(stream, build)

    assert app.state.entries[-1].text == "Cancelled."
    assert app.session.messages == ()


def test_new_request_waits_for_cancelled_stream_to_close(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tui, "CLOSE_WAIT", 0.05)
    stream = GatedStream([TextDelta("a"), completed("a")], [TextDelta("b"), completed("b")])

    def build(app: TuiApp) -> list[object]:
        return [*typed("One"), Wait(lambda: app.request is not None), "<CTRL-C>",
                *typed("Two"),  # the cancelled worker is still blocked on the network
                Wait(lambda: app.state.status.state == rt.WARNING),
                Do(lambda: stream.release(1)), Wait(lambda: app.closing is None),
                "\n", Do(lambda: stream.release(2)), idle(app)]

    app, screen, _ = run_script(stream, build)

    assert any("The cancelled response is still closing" in status_line(f) for f in screen.snapshots)
    assert stream.calls == [["One"], ["Two"]]
    assert [m.content for m in app.session.messages] == ["Two", "b"]


def test_exit_during_streaming_cancels_and_closes() -> None:
    stream = GatedStream([TextDelta("a"), completed("a")])

    def build(app: TuiApp) -> list[object]:
        # The worker is blocked "on the network" until the gate opens; open it shortly
        # after /exit, while the app waits for the stream to close.
        return [*typed("Hi"), Wait(lambda: app.request is not None),
                Do(lambda: threading.Timer(0.1, stream.release).start()), *typed("/exit")]

    app, _, exit_code = run_script(stream, build)

    assert exit_code == 0
    assert stream.closed_event.wait(2)
    assert app.session.messages == ()


def test_ctrl_c_when_idle_still_exits() -> None:
    stream = GatedStream()

    _, _, exit_code = run_script(stream, lambda app: ["<CTRL-C>"])

    assert exit_code == EXIT_INTERRUPTED


# ----- errors and incomplete answers -----


@pytest.mark.parametrize(
    ("terminal", "status", "entry"),
    [
        (StreamFailed(ProviderError("Request to Ollama timed out.", ErrorKind.TIMEOUT), "Par"),
         "ERROR | Request to Ollama timed out.",
         "Error: Request to Ollama timed out. The partial answer above was not saved."),
        (StreamFailed(ProviderError("Could not connect to Ollama.", ErrorKind.CONNECTION)),
         "ERROR | Could not connect to Ollama.", "Error: Could not connect to Ollama."),
        (StreamIncomplete("the max output token limit was reached", "Par"),
         "WARNING | Response incomplete: the max output token limit was reached",
         "Incomplete: the max output token limit was reached. The partial answer above was not saved."),
    ],
)
def test_failures_are_reported_and_not_saved(
    tmp_path: Path, terminal: object, status: str, entry: str
) -> None:
    store = HistoryStore(tmp_path / "conversations")
    deltas = [TextDelta("Par")] if getattr(terminal, "partial_text", "") else []
    stream = GatedStream([*deltas, terminal], open_gate=True)

    app, screen, _ = run_script(stream, lambda app: [*typed("Hi"), idle(app)], store=store)

    assert status_line(screen.snapshots[-1]) == status
    assert app.state.entries[-1].text == entry
    assert app.session.messages == ()
    assert store.list_paths() == []


def test_unexpected_exception_in_stream_becomes_error() -> None:
    stream = GatedStream([TextDelta("a"), RuntimeError("boom")], open_gate=True)

    app, screen, _ = run_script(stream, lambda app: [*typed("Hi"), idle(app)])

    assert status_line(screen.snapshots[-1]).startswith("ERROR | Unexpected error while streaming: boom")
    assert app.session.messages == ()
    assert stream.closed == 1


# ----- layout -----


def test_streaming_at_small_size_and_resize() -> None:
    stream = GatedStream(long_deltas(50))
    frames: list[list[str]] = []

    def build(app: TuiApp) -> list[object]:
        return [*typed("Hi"), Do(lambda: stream.release(20)),
                Wait(lambda: app.request is not None and "word19" in app.request.text),
                Do(lambda: setattr(app.screen, "rows", 12)), Do(lambda: setattr(app.screen, "cols", 40)),
                curses.KEY_RESIZE, Do(lambda: app.screen.erase()),
                Do(lambda: stream.release(31)), idle(app),
                Do(lambda: frames.append(app.screen.snapshots[-1]))]

    app, screen, _ = run_script(stream, build)  # FakeScreen fails on any out-of-bounds drawing

    assert len(frames[0]) == 12
    assert "DONE" in frames[0][7]


# ----- worker unit tests -----


def test_worker_closes_stream_when_cancelled_and_queue_is_bounded() -> None:
    events: queue.Queue = queue.Queue(QUEUE_SIZE)
    stream = GatedStream(long_deltas(200), open_gate=True)
    worker = StreamWorker(1, stream, [Message("user", "Hi")], events)
    worker.start()

    deadline = time.monotonic() + 2
    while events.qsize() < QUEUE_SIZE and time.monotonic() < deadline:
        time.sleep(0.01)
    assert events.qsize() == QUEUE_SIZE  # the worker waits instead of buffering everything
    assert worker.is_alive()

    worker.cancel()
    assert worker.join(2)
    assert stream.closed == 1
    assert stream.yielded < 200


def test_worker_reports_completion_and_finish() -> None:
    events: queue.Queue = queue.Queue(QUEUE_SIZE)
    stream = GatedStream([TextDelta("a"), completed("a")], open_gate=True)
    worker = StreamWorker(7, stream, [Message("user", "Hi")], events)
    worker.start()
    assert worker.join(2)

    items = [events.get_nowait() for _ in range(events.qsize())]

    assert [request_id for request_id, _ in items] == [7, 7, 7]
    assert isinstance(items[1][1], StreamCompleted)
    assert isinstance(items[2][1], WorkerFinished)


def test_worker_never_touches_curses(monkeypatch: pytest.MonkeyPatch) -> None:
    ui_thread = threading.current_thread()
    calls: list[str] = []

    class GuardedScreen(FakeScreen):
        def addnstr(self, *args: object) -> None:
            if threading.current_thread() is not ui_thread:
                calls.append(threading.current_thread().name)
            super().addnstr(*args)

    stream = GatedStream(long_deltas(30), open_gate=True)
    screen = GuardedScreen(25, 80)
    app = TuiApp(screen, SETTINGS, lambda m: None, STYLES, ChatSession(), stream_fn=stream)
    screen.keys = [*typed("Hi"), idle(app)]

    app.run()

    assert calls == []


# ----- status formatting -----


def test_done_status_formats_only_available_metrics() -> None:
    usage = TokenUsage(10, 50, 60)

    assert rt.done_status({"total_seconds": 7.17, "load_seconds": 0.2, "eval_seconds": 3.133},
                          usage, 9.9, 1.0).text() == (
        "DONE | 7.17s total | load 0.20s | generation 3.13s | 15.96 tok/s"
    )
    assert rt.done_status({"total_seconds": 7.0}, usage, 9.9, None).text() == "DONE | 7.00s total"
    assert rt.done_status({"total_seconds": 7.0, "eval_seconds": 2.0}, None, 1, None).text() == (
        "DONE | 7.00s total | generation 2.00s"  # no output token count: no tok/s
    )
    assert rt.done_status({}, usage, 3.21, 0.82).text() == "DONE | 3.21s observed | first text after 0.82s"
    assert rt.done_status({}, None, 3.21, None).text() == "DONE | 3.21s observed"


def test_active_status_format() -> None:
    assert rt.active_status(rt.GENERATING, 4.24).text() == "GENERATING | 4.2s elapsed | Ctrl+C Cancel"
    assert rt.active_status(rt.GENERATING, 1, "Thinking").text() == (
        "GENERATING | Thinking | 1.0s elapsed | Ctrl+C Cancel"
    )


def test_state_word_colors() -> None:
    stream = GatedStream([StreamFailed(ProviderError("boom"))], open_gate=True)

    _, screen, _ = run_script(stream, lambda app: [*typed("Hi"), idle(app)])

    assert screen.attr_at("ERROR | boom") == STYLES.error


def test_sigint_during_stream_cancels_at_a_safe_point() -> None:
    import os
    import signal as signal_module

    stream = GatedStream([TextDelta("Partial"), TextDelta("more"), completed("Partial more")])

    def build(app: TuiApp) -> list[object]:
        return [*typed("Hi"), Do(lambda: stream.release(1)),
                Wait(lambda: app.request is not None and app.request.text == "Partial"),
                Do(lambda: os.kill(os.getpid(), signal_module.SIGINT)),  # a real Ctrl+C signal
                Wait(lambda: app.request is None),
                Do(lambda: stream.release(1)), Wait(lambda: stream.closed == 1)]

    previous = signal_module.getsignal(signal_module.SIGINT)
    app, screen, exit_code = run_script(stream, build)

    assert exit_code == 0  # cancelled, not exited
    assert app.state.entries[-1].text == "Cancelled. The partial answer above was not saved."
    assert app.session.messages == ()
    assert signal_module.getsignal(signal_module.SIGINT) is previous  # handler restored


def test_sigint_when_idle_still_exits() -> None:
    import os
    import signal as signal_module

    stream = GatedStream()

    def build(app: TuiApp) -> list[object]:
        return [Do(lambda: os.kill(os.getpid(), signal_module.SIGINT)), "x"]

    _, _, exit_code = run_script(stream, build)

    assert exit_code == EXIT_INTERRUPTED
