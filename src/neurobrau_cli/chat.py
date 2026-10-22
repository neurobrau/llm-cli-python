"""Interactive terminal chat built on top of a reply function."""

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TextIO

from neurobrau_cli import __version__
from neurobrau_cli.conversation import Conversation, Message
from neurobrau_cli.formatting import format_usage
from neurobrau_cli.providers.openai_provider import GenerationResult, OpenAIProviderError

ReplyFn = Callable[[Sequence[Message]], GenerationResult]
InputFn = Callable[[str], str]

APP_NAME = "NeuroBräu CLI"
USER_PROMPT = "YOU > "
AI_PREFIX = "AI > "
COMMAND_PREFIXES = ("/", "\\")
HELP_TEXT = """Commands:
  /help    show this help
  /reset   clear the conversation history
  /exit    exit the chat
Backslash aliases also work: \\help, \\reset, \\exit.
Press Ctrl+D (EOF) or Ctrl+C at the prompt to exit."""

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


def handle_command(command: str, conversation: Conversation, stdout: TextIO) -> bool:
    """Execute a chat command. Return False when the chat should end."""
    match command:
        case "exit":
            print("Goodbye.", file=stdout)
            return False
        case "help":
            print(HELP_TEXT, file=stdout)
        case "reset":
            conversation.reset()
            print("Conversation history cleared.", file=stdout)
        case _:
            print(f"Unknown command: /{command}. Type /help for commands.", file=stdout)
    return True


def exchange(conversation: Conversation, text: str, reply_fn: ReplyFn) -> GenerationResult:
    """Send `text` with prior context and commit the exchange only on success.

    Exceptions from `reply_fn` propagate and leave the history unchanged.
    """
    result = reply_fn(conversation.with_user_message(text))
    conversation.commit_exchange(text, result.text)
    return result


def send_message(
    text: str,
    conversation: Conversation,
    reply_fn: ReplyFn,
    settings: ChatSettings,
    stdout: TextIO,
    stderr: TextIO,
) -> None:
    """Send a user message and print the reply, or report the failure."""
    try:
        result = exchange(conversation, text, reply_fn)
    except OpenAIProviderError as error:
        print(f"error: {error}", file=stderr)
        return
    except KeyboardInterrupt:
        print("\nRequest cancelled.", file=stderr)
        return

    print(f"{AI_PREFIX}{result.text}", file=stdout)
    if settings.show_usage:
        print(format_usage(result.usage), file=stderr)


def run_chat(
    settings: ChatSettings,
    reply_fn: ReplyFn,
    input_fn: InputFn = input,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the read-send-print loop until the user exits. Return an exit code."""
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    conversation = Conversation()
    print_banner(settings, stdout)

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
            if not handle_command(command, conversation, stdout):
                return EXIT_OK
            continue
        send_message(text, conversation, reply_fn, settings, stdout, stderr)


def enable_line_editing() -> None:
    """Enable arrow-key editing and input history for `input()` where available."""
    try:
        import readline  # noqa: F401  (imported for its side effect)
    except ImportError:
        pass
