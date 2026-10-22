"""Tests for JSON conversation storage, using temporary directories."""

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from neurobrau_cli import storage
from neurobrau_cli.conversation import Message
from neurobrau_cli.storage import HistoryError, HistoryStore, StoredConversation

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
T0 = datetime(2026, 10, 21, 12, 0, 0, 123456, tzinfo=UTC)
ID_A = "11111111-1111-4111-8111-111111111111"
ID_B = "22222222-2222-4222-8222-222222222222"


def make_conversation(
    conversation_id: str = ID_A,
    updated_at: datetime = T0,
    messages: tuple[Message, ...] = (Message("user", "Grüß Gott"), Message("assistant", "Hallo!")),
) -> StoredConversation:
    return StoredConversation(conversation_id, T0, updated_at, messages)


@pytest.fixture
def store(tmp_path: Path) -> HistoryStore:
    return HistoryStore(tmp_path / "neurobrau" / "conversations")


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ----- location -----


def test_default_dir_uses_absolute_xdg_state_home(tmp_path: Path) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path)}

    assert storage.default_history_dir(env) == tmp_path / "neurobrau" / "conversations"


@pytest.mark.parametrize("value", ["", "relative/state"])
def test_default_dir_falls_back_to_local_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    expected = tmp_path / ".local" / "state" / "neurobrau" / "conversations"
    assert storage.default_history_dir({"XDG_STATE_HOME": value}) == expected
    assert storage.default_history_dir({}) == expected


# ----- format -----


def test_save_writes_documented_json_format(store: HistoryStore) -> None:
    store.save(make_conversation())

    raw = store.path_for(ID_A).read_text(encoding="utf-8")
    assert "Grüß Gott" in raw  # stored as UTF-8, not \\u escapes
    assert json.loads(raw) == {
        "schema_version": 1,
        "id": ID_A,
        "created_at": "2026-10-21T12:00:00.123Z",
        "updated_at": "2026-10-21T12:00:00.123Z",
        "messages": [
            {"role": "user", "content": "Grüß Gott"},
            {"role": "assistant", "content": "Hallo!"},
        ],
    }


def test_save_and_load_round_trip(store: HistoryStore) -> None:
    original = make_conversation()
    store.save(original)

    loaded = store.load(store.path_for(ID_A))

    assert loaded.id == original.id
    assert loaded.messages == original.messages
    assert loaded.updated_at == T0.replace(microsecond=123000)


def test_load_accepts_offsets_and_unknown_fields(store: HistoryStore) -> None:
    store.ensure_directory()
    path = store.path_for(ID_A)
    path.write_text(json.dumps({
        "schema_version": 1,
        "id": ID_A,
        "created_at": "2026-10-21T14:00:00+02:00",
        "updated_at": "2026-10-21T14:00:00+02:00",
        "messages": [{"role": "user", "content": "Hi", "extra": True}],
        "future_field": "ignored",
    }))

    loaded = store.load(path)

    assert loaded.created_at == datetime(2026, 10, 21, 12, tzinfo=UTC)
    assert loaded.messages == (Message("user", "Hi"),)


def test_saved_file_never_contains_credentials(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-value")

    store.save(make_conversation())

    data = json.loads(store.path_for(ID_A).read_text())
    assert set(data) == {"schema_version", "id", "created_at", "updated_at", "messages"}
    assert "sk-test-secret-value" not in store.path_for(ID_A).read_text()


# ----- atomic writes -----


def test_save_replaces_file_and_leaves_no_temp_files(store: HistoryStore) -> None:
    store.save(make_conversation(messages=(Message("user", "old"),)))
    store.save(make_conversation(messages=(Message("user", "new"),)))

    assert [path.name for path in store.directory.iterdir()] == [f"{ID_A}.json"]
    assert store.load(store.path_for(ID_A)).messages == (Message("user", "new"),)


@pytest.mark.parametrize("failure", [OSError(28, "No space left on device"), KeyboardInterrupt()])
def test_interrupted_save_keeps_previous_file(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    store.save(make_conversation(messages=(Message("user", "old"),)))

    def failing_replace(source: str, destination: Path) -> None:
        raise failure

    monkeypatch.setattr(storage.os, "replace", failing_replace)
    expected = HistoryError if isinstance(failure, OSError) else KeyboardInterrupt
    with pytest.raises(expected):
        store.save(make_conversation(messages=(Message("user", "new"),)))

    assert [path.name for path in store.directory.iterdir()] == [f"{ID_A}.json"]
    assert store.load(store.path_for(ID_A)).messages == (Message("user", "old"),)


def test_save_error_is_reported_as_history_error(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    store = HistoryStore(blocker / "conversations")

    with pytest.raises(HistoryError, match="Could not save conversation history"):
        store.save(make_conversation())


# ----- permissions -----


@POSIX_ONLY
def test_permissions_are_owner_only_despite_umask(store: HistoryStore) -> None:
    old_umask = os.umask(0o022)
    try:
        store.save(make_conversation())
    finally:
        os.umask(old_umask)

    assert mode_of(store.directory) == 0o700
    assert mode_of(store.directory.parent) == 0o700  # the "neurobrau" directory
    assert mode_of(store.path_for(ID_A)) == 0o600


@POSIX_ONLY
def test_existing_permissive_directory_is_tightened(store: HistoryStore) -> None:
    store.directory.mkdir(parents=True)
    os.chmod(store.directory, 0o755)
    os.chmod(store.directory.parent, 0o755)

    store.save(make_conversation())

    assert mode_of(store.directory) == 0o700
    assert mode_of(store.directory.parent) == 0o700


@POSIX_ONLY
def test_parent_directories_outside_app_dir_are_not_changed(tmp_path: Path) -> None:
    state_home = tmp_path / "state"
    state_home.mkdir()
    os.chmod(state_home, 0o755)
    store = HistoryStore(storage.default_history_dir({"XDG_STATE_HOME": str(state_home)}))

    store.save(make_conversation())

    assert mode_of(state_home) == 0o755


# ----- restoring -----


def test_missing_directory_means_no_history(store: HistoryStore) -> None:
    assert store.list_paths() == []
    assert store.load_latest() == (None, [])
    assert not store.directory.exists()


def test_load_latest_picks_most_recently_updated(store: HistoryStore) -> None:
    store.save(make_conversation(ID_A, updated_at=T0 + timedelta(hours=1)))
    store.save(make_conversation(ID_B, updated_at=T0))

    latest, warnings = store.load_latest()

    assert latest is not None and latest.id == ID_A
    assert warnings == []


CORRUPT_CONTENTS = {
    "truncated": b'{"schema_version": 1, "id": ',
    "not an object": b"[]",
    "wrong version": json.dumps({"schema_version": 99}).encode(),
    "bad id": json.dumps({
        "schema_version": 1, "id": "../../etc/passwd", "created_at": "2026-10-21T12:00:00Z",
        "updated_at": "2026-10-21T12:00:00Z", "messages": [],
    }).encode(),
    "bad role": json.dumps({
        "schema_version": 1, "id": ID_B, "created_at": "2026-10-21T12:00:00Z",
        "updated_at": "2026-10-21T12:00:00Z", "messages": [{"role": "system", "content": "x"}],
    }).encode(),
    "naive timestamp": json.dumps({
        "schema_version": 1, "id": ID_B, "created_at": "2026-10-21T12:00:00",
        "updated_at": "2026-10-21T12:00:00", "messages": [],
    }).encode(),
    "not utf-8": b"\xff\xfe\x00garbage",
}


@pytest.mark.parametrize("content", CORRUPT_CONTENTS.values(), ids=CORRUPT_CONTENTS.keys())
def test_corrupt_file_is_reported_skipped_and_kept(store: HistoryStore, content: bytes) -> None:
    store.save(make_conversation(ID_A))
    corrupt = store.directory / f"{ID_B}.json"
    corrupt.write_bytes(content)

    latest, warnings = store.load_latest()

    assert latest is not None and latest.id == ID_A
    assert len(warnings) == 1
    assert str(corrupt) in warnings[0]
    assert "left unchanged" in warnings[0]
    assert corrupt.read_bytes() == content


def test_unreadable_directory_is_reported(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save(make_conversation())

    def denied(path: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(storage.os, "scandir", denied)
    latest, warnings = store.load_latest()

    assert latest is None
    assert warnings == [
        f"Could not read conversation history at {store.directory}: Permission denied."
    ]


# ----- cleanup -----


def test_delete_all_removes_only_conversation_files(store: HistoryStore) -> None:
    store.save(make_conversation(ID_A))
    store.save(make_conversation(ID_B))
    (store.directory / "notes.txt").write_text("not ours")

    assert store.delete_all() == 2
    assert [path.name for path in store.directory.iterdir()] == ["notes.txt"]


def test_delete_all_with_missing_directory(store: HistoryStore) -> None:
    assert store.delete_all() == 0


def test_load_latest_breaks_timestamp_ties_by_modification_time(store: HistoryStore) -> None:
    store.save(make_conversation(ID_B, updated_at=T0))
    store.save(make_conversation(ID_A, updated_at=T0))
    os.utime(store.path_for(ID_A), ns=(1_000_000_000, 1_000_000_000))
    os.utime(store.path_for(ID_B), ns=(2_000_000_000, 2_000_000_000))

    latest, _ = store.load_latest()

    assert latest is not None and latest.id == ID_B


@POSIX_ONLY
def test_restoring_tightens_existing_directories(store: HistoryStore) -> None:
    store.save(make_conversation())
    os.chmod(store.directory, 0o755)
    os.chmod(store.directory.parent, 0o755)

    store.load_latest()

    assert mode_of(store.directory) == 0o700
    assert mode_of(store.directory.parent) == 0o700


def test_provider_and_model_round_trip(store: HistoryStore) -> None:
    conversation = StoredConversation(ID_A, T0, T0, (), "ollama", "qwen3.5:4b")
    store.save(conversation)

    data = json.loads(store.path_for(ID_A).read_text())
    loaded = store.load(store.path_for(ID_A))

    assert list(data) == [
        "schema_version", "id", "created_at", "updated_at", "provider", "model", "messages"
    ]
    assert (loaded.provider, loaded.model) == ("ollama", "qwen3.5:4b")


@pytest.mark.parametrize("value", [42, "", ["x"]])
def test_invalid_provider_metadata_is_corrupt(store: HistoryStore, value: object) -> None:
    store.ensure_directory()
    data = storage.to_json(make_conversation())
    data["provider"] = value
    store.path_for(ID_A).write_text(json.dumps(data))

    with pytest.raises(HistoryError, match="'provider' must be a non-empty string"):
        store.load(store.path_for(ID_A))


def test_load_latest_with_filter(store: HistoryStore) -> None:
    store.save(StoredConversation(ID_A, T0, T0 + timedelta(hours=1), (), "openai", "m"))
    store.save(StoredConversation(ID_B, T0, T0, (), "ollama", "m"))

    latest, _ = store.load_latest(lambda conversation: conversation.provider == "ollama")

    assert latest is not None and latest.id == ID_B
