"""Local JSON storage for conversations.

One file per conversation: `<conversations dir>/<id>.json`, format described in
`docs/tui-spec.md` ("Conversation file format"). Files are plaintext; they hold
messages and timestamps only, never credentials.
"""

import json
import os
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from neurobrau_cli.conversation import Message

SCHEMA_VERSION = 1
APP_DIR_NAME = "neurobrau"
CONVERSATIONS_DIR_NAME = "conversations"
ROLES = ("user", "assistant")
ACTIVE_FILE_NAME = "active.json"
DIR_MODE = 0o700


class HistoryError(Exception):
    """A user-facing storage error (I/O failure, corrupt or unsupported file)."""


@dataclass(frozen=True)
class StoredConversation:
    """A conversation as written to disk."""

    id: str
    created_at: datetime
    updated_at: datetime
    messages: tuple[Message, ...] = ()
    provider: str | None = None  # optional; absent in files written before Task 006
    model: str | None = None


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_conversation_id() -> str:
    return str(uuid.uuid4())


def default_history_dir(env: Mapping[str, str] | None = None) -> Path:
    """`$XDG_STATE_HOME/neurobrau/conversations`, or `~/.local/state/...`.

    Per the XDG spec, a relative XDG_STATE_HOME is invalid and ignored.
    """
    env = os.environ if env is None else env
    state_home = env.get("XDG_STATE_HOME", "")
    base = Path(state_home) if os.path.isabs(state_home) else Path.home() / ".local" / "state"
    return base / APP_DIR_NAME / CONVERSATIONS_DIR_NAME


# ----- JSON (de)serialization -----


def format_timestamp(moment: datetime) -> str:
    """RFC 3339 UTC with millisecond precision, e.g. `2026-10-21T12:00:00.000Z`."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"'{field}' must be a string")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"'{field}' is not an RFC 3339 timestamp") from None
    if moment.tzinfo is None:
        raise ValueError(f"'{field}' has no time zone")
    return moment.astimezone(UTC)


def to_json(conversation: StoredConversation) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": conversation.id,
        "created_at": format_timestamp(conversation.created_at),
        "updated_at": format_timestamp(conversation.updated_at),
    }
    if conversation.provider is not None:
        data["provider"] = conversation.provider
    if conversation.model is not None:
        data["model"] = conversation.model
    data["messages"] = [
        {"role": message.role, "content": message.content} for message in conversation.messages
    ]
    return data


def from_json(data: object) -> StoredConversation:
    """Validate and convert parsed JSON. Raises ValueError describing the problem."""
    if not isinstance(data, dict):
        raise ValueError("top level must be a JSON object")
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version {version!r} (expected {SCHEMA_VERSION})")

    conversation_id = data.get("id")
    if not isinstance(conversation_id, str) or not is_valid_id(conversation_id):
        raise ValueError("'id' must be a UUID string")

    raw_messages = data.get("messages")
    if not isinstance(raw_messages, list):
        raise ValueError("'messages' must be a list")
    messages = []
    for index, item in enumerate(raw_messages):
        if not isinstance(item, dict):
            raise ValueError(f"message {index} must be an object")
        role, content = item.get("role"), item.get("content")
        if role not in ROLES:
            raise ValueError(f"message {index} has invalid role {role!r}")
        if not isinstance(content, str):
            raise ValueError(f"message {index} content must be a string")
        messages.append(Message(role, content))

    return StoredConversation(
        id=conversation_id,
        created_at=parse_timestamp(data.get("created_at"), "created_at"),
        updated_at=parse_timestamp(data.get("updated_at"), "updated_at"),
        messages=tuple(messages),
        provider=optional_string(data, "provider"),
        model=optional_string(data, "model"),
    )


def optional_string(data: dict[str, Any], field: str) -> str | None:
    value = data.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{field}' must be a non-empty string")
    return value


def is_valid_id(value: str) -> bool:
    """IDs are canonical lowercase UUIDs; this also keeps file names safe."""
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


# ----- filesystem -----


class HistoryStore:
    """Reads and writes conversation files in one directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path_for(self, conversation_id: str) -> Path:
        return self.directory / f"{conversation_id}.json"

    def list_paths(self) -> list[Path]:
        """Conversation files, sorted by name. A missing directory means none."""
        try:
            with os.scandir(self.directory) as entries:
                names = [entry.name for entry in entries if entry.name.endswith(".json")]
        except FileNotFoundError:
            return []
        except OSError as error:
            raise HistoryError(describe_os_error("read", self.directory, error)) from error
        return [self.directory / name for name in sorted(names)]

    def load(self, path: Path) -> StoredConversation:
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise HistoryError(describe_os_error("read", path, error)) from error
        try:
            # JSONDecodeError and UnicodeDecodeError are both ValueErrors.
            return from_json(json.loads(raw.decode("utf-8")))
        except (ValueError, RecursionError) as error:
            raise HistoryError(
                f"Ignoring unreadable conversation file {path}: {error}. "
                "The file was left unchanged."
            ) from error

    def load_latest(
        self, matches: Callable[[StoredConversation], bool] | None = None
    ) -> tuple[StoredConversation | None, list[str]]:
        """Return the most recently updated valid conversation and any warnings.

        `matches` limits the choice (e.g. to one provider). Unreadable or
        corrupt files are reported, skipped, and never modified.
        """
        warnings: list[str] = []
        self.secure_existing_directories()
        try:
            paths = self.list_paths()
        except HistoryError as error:
            return None, [str(error)]

        # Timestamps have millisecond precision, so ties are broken by the
        # file's modification time in nanoseconds.
        latest: StoredConversation | None = None
        latest_key: tuple[datetime, int] | None = None
        for path in paths:
            try:
                candidate = self.load(path)
                key = (candidate.updated_at, path.stat().st_mtime_ns)
            except HistoryError as error:
                warnings.append(str(error))
                continue
            except OSError as error:
                warnings.append(describe_os_error("read", path, error))
                continue
            if matches is not None and not matches(candidate):
                continue
            if latest_key is None or key > latest_key:
                latest, latest_key = candidate, key
        return latest, warnings

    def save(self, conversation: StoredConversation) -> None:
        """Write atomically: temp file in the same directory, fsync, then rename."""
        path = self.path_for(conversation.id)
        content = json.dumps(to_json(conversation), ensure_ascii=False, indent=2) + "\n"
        try:
            self.ensure_directory()
            write_atomically(path, content)
        except OSError as error:
            raise HistoryError(describe_os_error("save", path, error)) from error

    # ----- active selection -----

    @property
    def active_path(self) -> Path:
        """`active.json` next to the conversations directory (outside /cleanup's reach)."""
        return self.directory.parent / ACTIVE_FILE_NAME

    def load_active(self) -> tuple[tuple[str, str] | None, str | None]:
        """The provider/model in use when the app last ran, and a warning if unreadable."""
        try:
            data = json.loads(self.active_path.read_bytes().decode("utf-8"))
        except FileNotFoundError:
            return None, None
        except (OSError, ValueError) as error:
            return None, f"Ignoring unreadable {self.active_path}: {error}."
        if not isinstance(data, dict):
            return None, f"Ignoring invalid {self.active_path}."
        provider, model = data.get("provider"), data.get("model")
        if data.get("schema_version") != SCHEMA_VERSION or not (
            isinstance(provider, str) and provider and isinstance(model, str) and model
        ):
            return None, f"Ignoring invalid {self.active_path}."
        return (provider, model), None

    def save_active(self, provider: str, model: str) -> None:
        """Record the active selection. Conversation files are not touched."""
        data = {"schema_version": SCHEMA_VERSION, "provider": provider, "model": model}
        try:
            self.ensure_directory()
            write_atomically(self.active_path, json.dumps(data, ensure_ascii=False) + "\n")
        except OSError as error:
            raise HistoryError(describe_os_error("save", self.active_path, error)) from error

    def delete_all(self) -> int:
        """Delete every conversation file. Return how many were deleted."""
        deleted = 0
        for path in self.list_paths():
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise HistoryError(
                    f"Deleted {deleted} file(s), then failed: "
                    + describe_os_error("delete", path, error)
                ) from error
            deleted += 1
        return deleted

    def ensure_directory(self) -> None:
        """Create `neurobrau/conversations` with owner-only permissions."""
        self.directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        # mkdir's mode is reduced by the umask, so set it explicitly.
        for directory in self.owned_directories():
            os.chmod(directory, DIR_MODE)

    def secure_existing_directories(self) -> None:
        """Best effort: tighten directories created by other tools or older versions."""
        for directory in self.owned_directories():
            try:
                if directory.is_dir():
                    os.chmod(directory, DIR_MODE)
            except OSError:
                pass  # reported later if it prevents reading or saving

    def owned_directories(self) -> list[Path]:
        """The conversations directory and the app directory above it, if any.

        Directories further up (e.g. ~/.local/state) are not ours to change.
        """
        if self.directory.parent.name == APP_DIR_NAME:
            return [self.directory.parent, self.directory]
        return [self.directory]


def write_atomically(path: Path, content: str) -> None:
    """Replace `path` so readers see either the old or the new file, never a mix."""
    # mkstemp creates the file with mode 0600.
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:  # includes KeyboardInterrupt: never leave temp files behind
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    fsync_directory(path.parent)


def fsync_directory(directory: Path) -> None:
    """Persist the rename itself; not supported on every platform."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def describe_os_error(action: str, path: Path, error: OSError) -> str:
    reason = error.strerror or type(error).__name__
    return f"Could not {action} conversation history at {path}: {reason}."
