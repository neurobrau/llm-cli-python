"""Conversation manager shared by the plain chat and the TUI.

Wraps the in-memory `Conversation` with session metadata and optional
persistence. UIs call these methods; they never touch the filesystem directly.
"""

from collections.abc import Callable, Sequence
from datetime import datetime

from neurobrau_cli.conversation import Conversation, Message
from neurobrau_cli.providers.openai_provider import GenerationResult
from neurobrau_cli.storage import (
    HistoryError,
    HistoryStore,
    StoredConversation,
    new_conversation_id,
    utc_now,
)

ReplyFn = Callable[[Sequence[Message]], GenerationResult]
Clock = Callable[[], datetime]


def exchange(conversation: Conversation, text: str, reply_fn: ReplyFn) -> GenerationResult:
    """Send `text` with prior context and commit the exchange only on success.

    Exceptions from `reply_fn` propagate and leave the history unchanged.
    """
    result = reply_fn(conversation.with_user_message(text))
    conversation.commit_exchange(text, result.text)
    return result


class ChatSession:
    """The active conversation, saved after every successful exchange."""

    def __init__(
        self,
        store: HistoryStore | None = None,
        stored: StoredConversation | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.store = store
        self.clock = clock
        self.startup_warnings: list[str] = []
        self.restored = stored is not None and bool(stored.messages)
        if stored is None:
            self._start_new()
        else:
            self.id = stored.id
            self.created_at = stored.created_at
            self.updated_at = stored.updated_at
            self.conversation = Conversation(stored.messages)

    @classmethod
    def restore(cls, store: HistoryStore | None, clock: Clock = utc_now) -> "ChatSession":
        """Continue the most recently updated saved conversation, or start a new one."""
        if store is None:
            return cls(clock=clock)
        latest, warnings = store.load_latest()
        session = cls(store, latest, clock)
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

    def reset(self) -> str | None:
        """Start a new empty conversation; the previous one stays on disk.

        The empty conversation is saved right away so it is the one restored on
        the next launch. Return a save warning, if any.
        """
        if not self.messages:
            return None  # already empty: nothing to preserve, reuse this session
        self._start_new()
        return self.save()

    def saved_count(self) -> int:
        """Number of conversation files on disk. Raises HistoryError."""
        return len(self.store.list_paths()) if self.store else 0

    def cleanup(self) -> int:
        """Delete all saved conversations and start a new unsaved one. Raises HistoryError."""
        if self.store is None:
            return 0
        deleted = self.store.delete_all()
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
        return None

    def to_stored(self) -> StoredConversation:
        return StoredConversation(self.id, self.created_at, self.updated_at, self.messages)

    def _start_new(self) -> None:
        self.id = new_conversation_id()
        self.created_at = self.updated_at = self.clock()
        self.conversation = Conversation()
