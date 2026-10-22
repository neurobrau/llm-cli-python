"""Tests for the persistent conversation manager."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from neurobrau_cli.conversation import Message
from neurobrau_cli.providers.openai_provider import (
    ErrorKind,
    GenerationResult,
    OpenAIProviderError,
)
from neurobrau_cli.session import ChatSession
from neurobrau_cli.storage import HistoryStore


class Clock:
    """Deterministic clock advancing one second per call."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def echo(messages: Sequence[Message]) -> GenerationResult:
    return GenerationResult(text=f"echo: {messages[-1].content}")


def fail(messages: Sequence[Message]) -> GenerationResult:
    raise OpenAIProviderError("boom", ErrorKind.API)


@pytest.fixture
def store(tmp_path: Path) -> HistoryStore:
    return HistoryStore(tmp_path / "conversations")


def saved_messages(store: HistoryStore, session: ChatSession) -> list[dict[str, str]]:
    return json.loads(store.path_for(session.id).read_text())["messages"]


def test_restore_without_files_starts_new_unsaved_session(store: HistoryStore) -> None:
    session = ChatSession.restore(store)

    assert session.messages == ()
    assert not session.restored
    assert store.list_paths() == []


def test_send_saves_immediately(store: HistoryStore) -> None:
    session = ChatSession.restore(store)

    result, warning = session.send("Hi", echo)

    assert result.text == "echo: Hi"
    assert warning is None
    assert saved_messages(store, session) == [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "echo: Hi"},
    ]


def test_conversation_survives_restart(store: HistoryStore) -> None:
    first = ChatSession.restore(store)
    first.send("One", echo)
    first.send("Two", echo)

    second = ChatSession.restore(store)

    assert second.restored
    assert second.id == first.id
    assert second.messages == first.messages
    assert len(second.messages) == 4


def test_timestamps_track_creation_and_updates(store: HistoryStore) -> None:
    clock = Clock()
    session = ChatSession.restore(store, clock)
    session.send("One", echo)
    session.send("Two", echo)

    data = json.loads(store.path_for(session.id).read_text())

    assert data["created_at"] == "2026-10-21T12:00:01.000Z"
    assert data["updated_at"] == "2026-10-21T12:00:03.000Z"


def test_failed_request_does_not_change_saved_history(store: HistoryStore) -> None:
    session = ChatSession.restore(store)
    session.send("One", echo)
    before = store.path_for(session.id).read_bytes()

    with pytest.raises(OpenAIProviderError):
        session.send("Two", fail)

    assert store.path_for(session.id).read_bytes() == before
    assert len(session.messages) == 2


def test_reset_keeps_previous_file_and_restores_new_empty_session(store: HistoryStore) -> None:
    session = ChatSession.restore(store, Clock())
    session.send("Old topic", echo)
    old_id = session.id

    assert session.reset() is None

    assert session.id != old_id
    assert session.messages == ()
    assert len(saved_messages(store, ChatSession(store, store.load(store.path_for(old_id))))) == 2
    restored = ChatSession.restore(store)
    assert restored.id == session.id
    assert restored.messages == ()


def test_reset_of_empty_conversation_creates_no_file(store: HistoryStore) -> None:
    session = ChatSession.restore(store)

    session.reset()

    assert store.list_paths() == []


def test_cleanup_deletes_all_and_starts_new_session(store: HistoryStore) -> None:
    session = ChatSession.restore(store, Clock())
    session.send("A", echo)
    session.reset()
    session.send("B", echo)
    old_id = session.id

    assert session.saved_count() == 2
    assert session.cleanup() == 2

    assert store.list_paths() == []
    assert session.messages == ()
    assert session.id != old_id


def test_save_failure_is_reported_and_keeps_memory(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    session = ChatSession.restore(HistoryStore(blocker / "conversations"))

    result, warning = session.send("Hi", echo)

    assert result.text == "echo: Hi"
    assert warning is not None and "Could not save" in warning
    assert len(session.messages) == 2


def test_restore_reports_corrupt_files(store: HistoryStore) -> None:
    store.ensure_directory()
    (store.directory / "broken.json").write_text("{")

    session = ChatSession.restore(store)

    assert len(session.startup_warnings) == 1
    assert "broken.json" in session.startup_warnings[0]


def test_session_without_store_never_touches_disk(tmp_path: Path) -> None:
    session = ChatSession.restore(None)
    session.send("Hi", echo)
    session.reset()

    assert not session.persistent
    assert session.saved_count() == 0
    assert session.cleanup() == 0
    assert list(tmp_path.iterdir()) == []
