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


# ----- provider and model selection -----


def test_provider_and_model_are_saved(store: HistoryStore) -> None:
    session = ChatSession.restore(store, provider="ollama", model="qwen3.5:4b")
    session.send("Hi", echo)

    data = json.loads(store.path_for(session.id).read_text())

    assert (data["provider"], data["model"]) == ("ollama", "qwen3.5:4b")


def test_switch_starts_new_conversation_and_keeps_old_file(store: HistoryStore) -> None:
    session = ChatSession.restore(store, Clock(), provider="openai", model="gpt-x")
    session.send("Secret context", echo)
    old_id = session.id

    assert session.switch("ollama", "qwen3.5:4b") is None

    assert session.id != old_id
    assert session.messages == ()
    assert len(saved_messages(store, ChatSession(store, store.load(store.path_for(old_id))))) == 2
    assert ChatSession.restore(store).provider == "ollama"  # the new selection is "active"


def test_switch_never_forwards_history(store: HistoryStore) -> None:
    session = ChatSession.restore(store, provider="openai", model="gpt-x")
    session.send("Secret context", echo)
    seen: list[list[Message]] = []

    session.switch("ollama", "qwen3.5:4b")
    session.send("New topic", lambda messages: (seen.append(list(messages)), echo(messages))[1])

    assert seen == [[Message("user", "New topic")]]


def test_switch_to_same_selection_changes_nothing(store: HistoryStore) -> None:
    session = ChatSession.restore(store, provider="openai", model="gpt-x")
    session.send("Keep", echo)

    session.switch("openai", "gpt-x")

    assert len(session.messages) == 2
    assert len(store.list_paths()) == 1


def test_switch_of_empty_conversation_reuses_it(store: HistoryStore) -> None:
    session = ChatSession.restore(store, provider="openai", model="gpt-x")
    original_id = session.id

    session.switch("openai", "gpt-y")

    assert session.id == original_id
    assert len(store.list_paths()) == 1


def test_restore_filters_by_provider_and_model(store: HistoryStore) -> None:
    clock = Clock()
    openai = ChatSession.restore(store, clock, provider="openai", model="gpt-x")
    openai.send("OpenAI chat", echo)
    ollama = ChatSession.restore(store, clock, provider="ollama", model="qwen3.5:4b")
    ollama.send("Ollama chat", echo)

    assert ChatSession.restore(store, provider="openai").messages[0].content == "OpenAI chat"
    assert ChatSession.restore(store, provider="ollama").messages[0].content == "Ollama chat"
    assert ChatSession.restore(store).provider == "ollama"  # most recent overall
    other_model = ChatSession.restore(store, provider="ollama", model="llama3.2:3b")
    assert other_model.messages == ()
    assert other_model.model == "llama3.2:3b"


def test_restore_by_provider_adopts_saved_model(store: HistoryStore) -> None:
    ChatSession.restore(store, provider="ollama", model="llama3.2:3b").send("Hi", echo)

    session = ChatSession.restore(store, provider="ollama")

    assert session.model == "llama3.2:3b"


def test_legacy_file_without_metadata_counts_as_openai(store: HistoryStore) -> None:
    ChatSession.restore(store).send("Old chat", echo)  # no provider/model, like Task 005

    assert ChatSession.restore(store, provider="openai", model="gpt-6-luna").restored
    assert not ChatSession.restore(store, provider="ollama").restored
    legacy = ChatSession.restore(store)
    assert (legacy.provider, legacy.model) == ("openai", None)


# ----- Task 007b: per-selection conversations restored on switch -----


class Recorder:
    """Reply function that records what each request contained."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.seen: list[list[str]] = []

    def __call__(self, messages: Sequence[Message]) -> GenerationResult:
        self.seen.append([m.content for m in messages])
        return GenerationResult(text=f"{self.label} reply {len(self.seen)}")


def test_openai_ollama_openai_restores_each_conversation(store: HistoryStore) -> None:
    openai, ollama = Recorder("openai"), Recorder("ollama")
    session = ChatSession.restore(store, Clock(), provider="openai", model="gpt-x")
    session.send("OpenAI topic", openai)

    session.switch("ollama", "qwen3.5:4b")
    assert session.messages == ()
    session.send("Ollama topic", ollama)

    session.switch("openai", "gpt-x")
    assert session.restored
    assert [m.content for m in session.messages] == ["OpenAI topic", "openai reply 1"]
    session.send("Back again", openai)

    session.switch("ollama", "qwen3.5:4b")
    assert [m.content for m in session.messages] == ["Ollama topic", "ollama reply 1"]

    assert openai.seen == [["OpenAI topic"], ["OpenAI topic", "openai reply 1", "Back again"]]
    assert ollama.seen == [["Ollama topic"]]  # never sees OpenAI history
    assert len(store.list_paths()) == 2


def test_two_models_of_one_provider_stay_separate(store: HistoryStore) -> None:
    session = ChatSession.restore(store, Clock(), provider="ollama", model="qwen3.5:4b")
    session.send("Qwen chat", echo)
    session.switch("ollama", "llama3.2:3b")
    session.send("Llama chat", echo)

    session.switch("ollama", "qwen3.5:4b")

    assert [m.content for m in session.messages] == ["Qwen chat", "echo: Qwen chat"]
    session.switch("ollama", "llama3.2:3b")
    assert [m.content for m in session.messages] == ["Llama chat", "echo: Llama chat"]


def test_switching_restores_from_disk_in_a_new_process(store: HistoryStore) -> None:
    clock = Clock()
    first = ChatSession.restore(store, clock, provider="openai", model="gpt-x")
    first.send("Saved OpenAI chat", echo)
    first.switch("ollama", "qwen3.5:4b")
    first.send("Saved Ollama chat", echo)

    second = ChatSession.restore(store, clock)  # restart: resumes the active selection
    assert (second.provider, second.model) == ("ollama", "qwen3.5:4b")
    assert second.messages[0].content == "Saved Ollama chat"

    second.switch("openai", "gpt-x")
    assert second.restored
    assert second.messages[0].content == "Saved OpenAI chat"


def test_restart_resumes_selection_restored_by_switching(store: HistoryStore) -> None:
    clock = Clock()
    session = ChatSession.restore(store, clock, provider="openai", model="gpt-x")
    session.send("OpenAI chat", echo)
    session.switch("ollama", "qwen3.5:4b")
    session.send("Ollama chat", echo)
    session.switch("openai", "gpt-x")  # restored, nothing new sent

    restarted = ChatSession.restore(store, clock)

    assert (restarted.provider, restarted.model) == ("openai", "gpt-x")
    assert restarted.messages[0].content == "OpenAI chat"


def test_restoring_does_not_rewrite_conversation_files(store: HistoryStore) -> None:
    session = ChatSession.restore(store, Clock(), provider="openai", model="gpt-x")
    session.send("Keep me", echo)
    openai_file = store.path_for(session.id)
    before = openai_file.read_bytes()
    session.switch("ollama", "qwen3.5:4b")
    restarted = ChatSession.restore(store, Clock())
    mtime = openai_file.stat().st_mtime_ns

    restarted.switch("openai", "gpt-x")

    assert openai_file.read_bytes() == before
    assert openai_file.stat().st_mtime_ns == mtime


def test_reset_then_switch_then_back_keeps_reset_state(store: HistoryStore) -> None:
    clock = Clock()
    session = ChatSession.restore(store, clock, provider="openai", model="gpt-x")
    session.send("Archived topic", echo)
    archived_id = session.id
    session.reset()
    fresh_id = session.id

    session.switch("ollama", "qwen3.5:4b")
    session.send("Ollama chat", echo)
    session.switch("openai", "gpt-x")

    assert session.id == fresh_id
    assert session.messages == ()  # the post-reset conversation, not the archived one
    archived = store.load(store.path_for(archived_id))
    assert archived.messages[0].content == "Archived topic"
    assert len(store.list_paths()) == 3


def test_switch_without_history_restores_within_the_run() -> None:
    session = ChatSession(provider="openai", model="gpt-x")
    session.send("In memory", echo)
    session.switch("ollama", "qwen3.5:4b")
    session.send("Other", echo)

    session.switch("openai", "gpt-x")

    assert session.messages[0].content == "In memory"
    assert not session.persistent


def test_active_selection_file(store: HistoryStore) -> None:
    session = ChatSession.restore(store, provider="openai", model="gpt-x")
    session.send("Hi", echo)
    session.switch("ollama", "qwen3.5:4b")

    data = json.loads(store.active_path.read_text())

    assert data == {"schema_version": 1, "provider": "ollama", "model": "qwen3.5:4b"}
    assert store.active_path.parent == store.directory.parent  # not touched by /cleanup


@pytest.mark.parametrize("content", ["{", '{"schema_version": 1, "provider": ""}', "[]"])
def test_invalid_active_file_is_reported_and_ignored(store: HistoryStore, content: str) -> None:
    session = ChatSession.restore(store, Clock(), provider="openai", model="gpt-x")
    session.send("Latest", echo)
    store.active_path.write_text(content)

    restarted = ChatSession.restore(store)

    assert restarted.messages[0].content == "Latest"  # falls back to the newest conversation
    assert any("active.json" in warning for warning in restarted.startup_warnings)
    assert store.active_path.read_text() == content


def test_cleanup_then_switch_back_starts_empty(store: HistoryStore) -> None:
    session = ChatSession.restore(store, provider="openai", model="gpt-x")
    session.send("Gone soon", echo)
    session.switch("ollama", "qwen3.5:4b")
    session.cleanup()

    session.switch("openai", "gpt-x")

    assert session.messages == ()  # deleted conversations do not come back from memory
    remaining = [store.load(path) for path in store.list_paths()]
    assert [(c.provider, c.messages) for c in remaining] == [("openai", ())]  # the new empty one
