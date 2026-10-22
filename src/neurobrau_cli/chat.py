"""Interactive terminal chat built on top of a reply function."""

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TextIO

from neurobrau_cli import __version__
from neurobrau_cli.formatting import format_usage
from neurobrau_cli.providers.openai_provider import OpenAIProviderError
from neurobrau_cli.session import ChatSession, ReplyFn, exchange
from neurobrau_cli.storage import HistoryError

__all__ = ["ChatSettings", "ReplyFn", "exchange", "parse_command", "run_chat"]

InputFn = Callable[[str], str]

APP_NAME = "NeuroBräu CLI"
USER_PROMPT = "YOU > "
AI_PREFIX = "AI > "
COMMAND_PREFIXES = ("/", "\\")
HELP_TEXT = """Commands:
  /help     show this help
  /reset    start a new conversation (the previous one stays saved)
  /cleanup  delete all saved conversations (asks for confirmation)
  /exit     exit the chat (the conversation stays saved)
Backslash aliases also work: \\help, \\reset, \\cleanup, \\exit.
Press Ctrl+D (EOF) or Ctrl+C at the prompt to exit."""
CLEANUP_CONFIRMATION = "yes"

EXIT_OK = 0
EXIT_INTERRUPTED = 130  # conventional exit code for Ctrl+C (128 + SIGINT)


@dataclass(frozen=True)
class ChatSettings:
    """Display and output options for an interactive session."""

    provider: str
    model: str
    show_usage: bool = False


def parse_command(text: str) -> str | None:
    """Return the command name if `text` is a command such as `/help`, else None.

    Only a single word starting with `/` or `\\` is a command, so messages like
    "/usr/bin is what?" are still sent to the model.
    """
    if not text.startswith(COMMAND_PREFIXES) or any(char.isspace() for char in text):
        return None
    return text[1:].lower()


def print_banner(settings: ChatSettings, stdout: TextIO) -> None:
    """Print the startup banner."""
    print(f"{APP_NAME} {__version__}", file=stdout)
    print(f"Provider: {settings.provider} | Model: {settings.model}", file=stdout)
    print("Type /help for commands, /exit to quit.", file=stdout)
    print(file=stdout)


def restored_notice(session: ChatSession) -> str | None:
    """Startup notice for a restored conversation, shared by plain chat and TUI."""
    if not session.restored:
        return None
    updated = session.updated_at.astimezone().strftime("%Y-%m-%d %H:%M")
    return (
        f"Restored conversation from {updated} ({len(session.messages)} messages). "
        "Type /reset to start a new one."
    )


def reset_notice(session: ChatSession) -> str:
    if session.persistent:
        return "Started a new conversation. The previous one stays saved."
    return "Conversation history cleared."


def cleanup_prompt(session: ChatSession, count: int) -> str:
    directory = session.store.directory if session.store else ""
    return (
        f"Delete {count} saved conversation(s) in {directory}? This cannot be undone. "
        f"Type '{CLEANUP_CONFIRMATION}' to confirm: "
    )


def is_confirmed(answer: str) -> bool:
    return answer.strip().lower() == CLEANUP_CONFIRMATION


def prepare_cleanup(session: ChatSession) -> tuple[int, str | None]:
    """Return (number of saved files, message to show instead of a prompt)."""
    if not session.persistent:
        return 0, "Conversation history is disabled for this session (--no-history)."
    try:
        count = session.saved_count()
    except HistoryError as error:
        return 0, str(error)
    return count, None if count else "No saved conversations."


def finish_cleanup(session: ChatSession) -> tuple[str, bool]:
    """Delete saved conversations. Return (message, success)."""
    try:
        deleted = session.cleanup()
    except HistoryError as error:
        return str(error), False
    return f"Deleted {deleted} saved conversation(s). Started a new conversation.", True


def handle_command(
    command: str,
    session: ChatSession,
    input_fn: InputFn,
    stdout: TextIO,
    stderr: TextIO,
) -> bool:
    """Execute a chat command. Return False when the chat should end."""
    match command:
        case "exit":
            print("Goodbye.", file=stdout)
            return False
        case "help":
            print(HELP_TEXT, file=stdout)
        case "reset":
            warning = session.reset()
            print(reset_notice(session), file=stdout)
            if warning:
                print(f"warning: {warning}", file=stderr)
        case "cleanup":
            run_cleanup(session, input_fn, stdout, stderr)
        case _:
            print(f"Unknown command: /{command}. Type /help for commands.", file=stdout)
    return True


def run_cleanup(session: ChatSession, input_fn: InputFn, stdout: TextIO, stderr: TextIO) -> None:
    """Ask for explicit confirmation, then delete saved conversations."""
    count, message = prepare_cleanup(session)
    if message:
        print(message, file=stdout)
        return
    try:
        answer = input_fn(cleanup_prompt(session, count))
    except (EOFError, KeyboardInterrupt):
        answer = ""
        print(file=stdout)
    if not is_confirmed(answer):
        print("Cleanup cancelled. Nothing was deleted.", file=stdout)
        return
    message, success = finish_cleanup(session)
    print(message if success else f"error: {message}", file=stdout if success else stderr)


def send_message(
    text: str,
    session: ChatSession,
    reply_fn: ReplyFn,
    settings: ChatSettings,
    stdout: TextIO,
    stderr: TextIO,
) -> None:
    """Send a user message and print the reply, or report the failure."""
    try:
        result, save_warning = session.send(text, reply_fn)
    except OpenAIProviderError as error:
        print(f"error: {error}", file=stderr)
        return
    except KeyboardInterrupt:
        print("\nRequest cancelled.", file=stderr)
        return

    print(f"{AI_PREFIX}{result.text}", file=stdout)
    if settings.show_usage:
        print(format_usage(result.usage), file=stderr)
    if save_warning:
        print(f"warning: {save_warning}", file=stderr)


def run_chat(
    settings: ChatSettings,
    reply_fn: ReplyFn,
    input_fn: InputFn = input,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    session: ChatSession | None = None,
) -> int:
    """Run the read-send-print loop until the user exits. Return an exit code."""
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    session = session or ChatSession()
    print_banner(settings, stdout)
    for warning in session.startup_warnings:
        print(f"warning: {warning}", file=stderr)
    notice = restored_notice(session)
    if notice:
        print(notice, file=stdout)
        print(file=stdout)

    while True:
        try:
            text = input_fn(USER_PROMPT).strip()
        except EOFError:
            print("\nGoodbye.", file=stdout)
            return EXIT_OK
        except KeyboardInterrupt:
            print("\nGoodbye.", file=stdout)
            return EXIT_INTERRUPTED

        if not text:
            continue
        command = parse_command(text)
        if command is not None:
            if not handle_command(command, session, input_fn, stdout, stderr):
                return EXIT_OK
            continue
        send_message(text, session, reply_fn, settings, stdout, stderr)


def enable_line_editing() -> None:
    """Enable arrow-key editing and input history for `input()` where available."""
    try:
        import readline  # noqa: F401  (imported for its side effect)
    except ImportError:
        pass
