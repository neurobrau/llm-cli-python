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

# Files written before provider selection existed have no provider/model;
# they were all OpenAI conversations.
LEGACY_PROVIDER = "openai"


def exchange(conversation: Conversation, text: str, reply_fn: ReplyFn) -> GenerationResult:
    """Send `text` with prior context and commit the exchange only on success.

    Exceptions from `reply_fn` propagate and leave the history unchanged.
    """
    result = reply_fn(conversation.with_user_message(text))
    conversation.commit_exchange(text, result.text)
    return result


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
        if stored is None:
            self._start_new()
        else:
            self.id = stored.id
            self.created_at = stored.created_at
            self.updated_at = stored.updated_at
            self.conversation = Conversation(stored.messages)
            self.provider = stored.provider or LEGACY_PROVIDER
            self.model = stored.model

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

        def matches(stored: StoredConversation) -> bool:
            stored_provider = stored.provider or LEGACY_PROVIDER
            return (provider is None or stored_provider == provider) and (
                model is None or stored.model in (None, model)
            )

        latest, warnings = store.load_latest(matches)
        session = cls(store, latest, clock, provider, model)
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

        A non-empty conversation is kept on disk and a new one is started. The
        (possibly empty) conversation is saved so the new selection is the one
        restored on the next launch.
        """
        if (provider, model) == (self.provider, self.model):
            return None
        if self.messages:
            self._start_new()
        self.provider, self.model = provider, model
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
        return StoredConversation(
            self.id, self.created_at, self.updated_at, self.messages, self.provider, self.model
        )

    def _start_new(self) -> None:
        self.id = new_conversation_id()
        self.created_at = self.updated_at = self.clock()
        self.conversation = Conversation()
