"""Provider-independent conversation history kept in process memory."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

Role = Literal["user", "assistant"]


@dataclass(frozen=True)
class Message:
    """A single text message in a conversation."""

    role: Role
    content: str


class Conversation:
    """Chronological history of committed user/assistant messages."""

    def __init__(self, messages: Iterable[Message] = ()) -> None:
        self._messages: list[Message] = list(messages)

    @property
    def messages(self) -> tuple[Message, ...]:
        """Committed messages, oldest first. Returned as an immutable snapshot."""
        return tuple(self._messages)

    def with_user_message(self, content: str) -> list[Message]:
        """Return the history plus a new user message, without committing it."""
        return [*self._messages, Message("user", content)]

    def commit_exchange(self, user_content: str, assistant_content: str) -> None:
        """Append a completed user/assistant exchange to the history."""
        self._messages.append(Message("user", user_content))
        self._messages.append(Message("assistant", assistant_content))

    def reset(self) -> None:
        """Remove all messages."""
        self._messages.clear()
