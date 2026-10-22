"""Background thread that reads a response stream and hands events to the UI thread.

The worker never touches curses. It puts `(request_id, event)` pairs on a
bounded queue that the UI drains between redraws. Cancellation is a flag:
the worker checks it after every event and then closes the stream (which
closes the HTTP connection). A read that is blocked waiting for the network
cannot be interrupted from another thread; it ends when the next data
arrives or the provider's read timeout expires.
"""

import queue
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from neurobrau_cli.conversation import Message
from neurobrau_cli.providers.base import (
    ProviderError,
    StreamCompleted,
    StreamEvent,
    StreamFailed,
    StreamIncomplete,
)
from neurobrau_cli.session import StreamFn

QUEUE_SIZE = 64  # backpressure: a slow UI makes the worker wait instead of buffering everything
PUT_TIMEOUT = 0.1  # seconds; how often a waiting worker re-checks cancellation


@dataclass(frozen=True)
class WorkerFinished:
    """The worker has closed its stream and is about to exit."""


QueueItem = tuple[int, StreamEvent | WorkerFinished]


class StreamWorker:
    def __init__(
        self,
        request_id: int,
        stream_fn: StreamFn,
        messages: Sequence[Message],
        events: "queue.Queue[QueueItem]",
    ) -> None:
        self.request_id = request_id
        self.stream_fn = stream_fn
        self.messages = list(messages)
        self.events = events
        self.cancelled = threading.Event()
        self.thread = threading.Thread(target=self.run, name=f"stream-{request_id}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def cancel(self) -> None:
        self.cancelled.set()

    def is_alive(self) -> bool:
        return self.thread.is_alive()

    def join(self, timeout: float) -> bool:
        """Wait at most `timeout` seconds; True if the worker has finished."""
        self.thread.join(timeout)
        return not self.thread.is_alive()

    def run(self) -> None:
        stream: Iterator[StreamEvent] | None = None
        try:
            stream = self.stream_fn(self.messages)
            for event in stream:
                if self.cancelled.is_set():
                    break
                self.post(event)
                if isinstance(event, (StreamCompleted, StreamIncomplete, StreamFailed)):
                    break
            else:
                self.post(StreamFailed(ProviderError("The response stream ended without a result.")))
        except Exception as error:  # never let a provider bug kill the thread silently
            self.post(StreamFailed(ProviderError(f"Unexpected error while streaming: {error}")))
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                try:
                    close()  # releases the network connection
                except Exception:
                    pass
            self.post(WorkerFinished(), force=True)

    def post(self, item: StreamEvent | WorkerFinished, force: bool = False) -> None:
        """Queue an item, waiting while the queue is full unless cancelled."""
        while True:
            if self.cancelled.is_set() and not force:
                return
            try:
                self.events.put((self.request_id, item), timeout=PUT_TIMEOUT)
                return
            except queue.Full:
                if force and self.cancelled.is_set():
                    return  # nobody is listening any more
