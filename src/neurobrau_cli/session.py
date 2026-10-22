"""Conversation manager shared by the plain chat and the TUI.

Wraps the in-memory `Conversation` with session metadata and optional
persistence. UIs call these methods; they never touch the filesystem directly.
"""

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime

from neurobrau_cli.conversation import Conversation, Message
from neurobrau_cli.providers.base import (
    GenerationResult,
    ProviderError,
    ReasoningDelta,
    StreamCompleted,
    StreamEvent,
    StreamFailed,
    TerminalEvent,
    TextDelta,
)
from neurobrau_cli.storage import (
    HistoryError,
    HistoryStore,
    StoredConversation,
    new_conversation_id,
    utc_now,
)

ReplyFn = Callable[[Sequence[Message]], GenerationResult]
StreamFn = Callable[[Sequence[Message]], Iterator[StreamEvent]]
DeltaHandler = Callable[[TextDelta | ReasoningDelta], None]
Clock = Callable[[], datetime]

# Files written before provider selection existed have no provider/model;
# they were all OpenAI conversations.
LEGACY_PROVIDER = "openai"

Selection = tuple[str | None, str | None]


@dataclass
class Parked:
    """A conversation set aside while another provider/model is in use."""

    id: str
    created_at: datetime
    updated_at: datetime
    conversation: Conversation
    on_disk: bool


def matcher(provider: str | None, model: str | None) -> Callable[[StoredConversation], bool]:
    """Conversations held with `provider`/`model` (None: any). Legacy files are OpenAI, any model."""

    def matches(stored: StoredConversation) -> bool:
        stored_provider = stored.provider or LEGACY_PROVIDER
        return (provider is None or stored_provider == provider) and (
            model is None or stored.model in (None, model)
        )

    return matches


def exchange(conversation: Conversation, text: str, reply_fn: ReplyFn) -> GenerationResult:
    """Send `text` with prior context and commit the exchange only on success.

    Exceptions from `reply_fn` propagate and leave the history unchanged.
    """
    result = reply_fn(conversation.with_user_message(text))
    conversation.commit_exchange(text, result.text)
    return result


def stream_exchange(
    conversation: Conversation, text: str, stream_fn: StreamFn, on_delta: DeltaHandler
) -> TerminalEvent:
    """Stream a reply to `text` with prior context; commit only a completed answer.

    Deltas go to `on_delta` as they arrive. The assembled answer is committed
    with the user message only on StreamCompleted; incomplete or failed
    replies, and KeyboardInterrupt, leave the history unchanged. The stream
    is always closed, which releases its network connection.
    """
    events = stream_fn(conversation.with_user_message(text))
    try:
        for event in events:
            if isinstance(event, (TextDelta, ReasoningDelta)):
                on_delta(event)
                continue
            if isinstance(event, StreamCompleted):
                conversation.commit_exchange(text, event.result.text)
            return event
        return StreamFailed(ProviderError("The response stream ended without a result."))
    finally:
        close = getattr(events, "close", None)
        if close is not None:
            close()


class ChatSession:
    """The active conversation, saved after every successful exchange.

    A conversation belongs to one provider and model; switching either starts
    a new conversation so history is never sent to a different model.
    """

    def __init__(
        self,
        store: HistoryStore | None = None,
        stored: StoredConversation | None = None,
        clock: Clock = utc_now,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.startup_warnings: list[str] = []
        self.restored = stored is not None and bool(stored.messages)
        self.provider = provider
        self.model = model
        self._parked: dict[Selection, Parked] = {}
        self._active_saved: Selection | None = None  # last selection written to active.json
        if stored is None:
            self._start_new()
        else:
            self._adopt(stored)

    @classmethod
    def restore(
        cls,
        store: HistoryStore | None,
        clock: Clock = utc_now,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> "ChatSession":
        """Continue the most recently updated saved conversation, or start a new one.

        With `provider` and/or `model`, only conversations held with them are
        candidates. Legacy files (no metadata) count as OpenAI with any model.
        """
        if store is None:
            return cls(clock=clock, provider=provider, model=model)

        warnings: list[str] = []
        if provider is None and model is None:
            # Continue with whatever was active last time (see active.json).
            active, warning = store.load_active()
            if warning:
                warnings.append(warning)
            if active:
                provider, model = active
        latest, load_warnings = store.load_latest(matcher(provider, model))
        session = cls(store, latest, clock, provider, model)
        warnings.extend(load_warnings)
        session.provider = provider or session.provider
        session.model = model or session.model
        session.startup_warnings = warnings
        return session

    @property
    def persistent(self) -> bool:
        return self.store is not None

    @property
    def messages(self) -> tuple[Message, ...]:
        return self.conversation.messages

    def send(self, text: str, reply_fn: ReplyFn) -> tuple[GenerationResult, str | None]:
        """Exchange a message and save it. Return the result and a save warning, if any.

        Provider errors and KeyboardInterrupt propagate without changing history.
        A failed save keeps the exchange in memory and is reported, not raised.
        """
        result = exchange(self.conversation, text, reply_fn)
        return result, self.save()

    def send_stream(
        self, text: str, stream_fn: StreamFn, on_delta: DeltaHandler
    ) -> tuple[TerminalEvent, str | None]:
        """Streaming `send`: returns the terminal event and a save warning, if any.

        Only a completed answer is committed and saved.
        """
        terminal = stream_exchange(self.conversation, text, stream_fn, on_delta)
        warning = self.save() if isinstance(terminal, StreamCompleted) else None
        return terminal, warning

    def complete_exchange(self, text: str, answer: str) -> str | None:
        """Commit a finished user/assistant pair (e.g. assembled from a stream) and save it."""
        self.conversation.commit_exchange(text, answer)
        return self.save()

    def reset(self) -> str | None:
        """Start a new empty conversation; the previous one stays on disk.

        The empty conversation is saved right away so it is the one restored on
        the next launch. Return a save warning, if any.
        """
        if not self.messages:
            return None  # already empty: nothing to preserve, reuse this session
        self._start_new()
        return self.save()

    def switch(self, provider: str, model: str) -> str | None:
        """Use another provider or model. Return a save warning, if any.

        Each provider/model keeps its own conversation: the current one is set
        aside (it is already saved), and the target's most recent conversation
        comes back, from this run or from disk. History never moves between
        selections. Without a previous conversation, a new empty one is started
        and saved so the selection is restored on the next launch. Restoring
        does not rewrite the restored file; `active.json` records the selection.
        """
        if (provider, model) == (self.provider, self.model):
            return None
        if self.messages:
            self._parked[(self.provider, self.model)] = Parked(
                self.id, self.created_at, self.updated_at, self.conversation, self._on_disk
            )
        parked = self._parked.pop((provider, model), None)
        stored = None
        if parked is None and self.store is not None:
            stored, _ = self.store.load_latest(matcher(provider, model))
        if parked is not None:
            self.id, self.created_at, self.updated_at = parked.id, parked.created_at, parked.updated_at
            self.conversation, self._on_disk = parked.conversation, parked.on_disk
        elif stored is not None:
            self._adopt(stored)
        else:
            # An empty conversation that is not on disk yet can be reused;
            # anything else (e.g. the empty one /reset saved) stays with its selection.
            if self.messages or self._on_disk:
                self._start_new()
            self.restored = False
            self.provider, self.model = provider, model
            return self.save()
        self.restored = bool(self.messages)
        self.provider, self.model = provider, model
        return self.save_active()

    def saved_count(self) -> int:
        """Number of conversation files on disk. Raises HistoryError."""
        return len(self.store.list_paths()) if self.store else 0

    def cleanup(self) -> int:
        """Delete all saved conversations and start a new unsaved one. Raises HistoryError."""
        if self.store is None:
            return 0
        deleted = self.store.delete_all()
        self._parked.clear()  # conversations set aside in this run are deleted too
        self._start_new()
        return deleted

    def save(self) -> str | None:
        """Write the conversation to disk. Return a warning instead of raising."""
        if self.store is None:
            return None
        self.updated_at = self.clock()
        try:
            self.store.save(self.to_stored())
        except HistoryError as error:
            return str(error)
        self._on_disk = True
        return self.save_active()

    def save_active(self) -> str | None:
        """Record the current provider/model as active (only when it changed)."""
        selection = (self.provider, self.model)
        if self.store is None or None in selection or selection == self._active_saved:
            return None
        try:
            self.store.save_active(self.provider, self.model)  # type: ignore[arg-type]
        except HistoryError as error:
            return str(error)
        self._active_saved = selection
        return None

    def to_stored(self) -> StoredConversation:
        return StoredConversation(
            self.id, self.created_at, self.updated_at, self.messages, self.provider, self.model
        )

    def _start_new(self) -> None:
        self.id = new_conversation_id()
        self.created_at = self.updated_at = self.clock()
        self.conversation = Conversation()
        self._on_disk = False

    def _adopt(self, stored: StoredConversation) -> None:
        self.id = stored.id
        self.created_at = stored.created_at
        self.updated_at = stored.updated_at
        self.conversation = Conversation(stored.messages)
        self.provider = stored.provider or LEGACY_PROVIDER
        self.model = stored.model or self.model
        self._on_disk = True
