"""Command-line entry point for NeuroBräu CLI."""

import argparse
import functools
import os
import sys
from collections.abc import Sequence
from types import ModuleType
from typing import TextIO

from neurobrau_cli import __version__, chat
from neurobrau_cli.formatting import format_usage
from neurobrau_cli.providers import openai_provider
from neurobrau_cli.providers.openai_provider import ErrorKind, OpenAIProviderError

PROG_NAME = "neurobrau"
PROVIDERS = ("openai",)

EXIT_OK = 0
EXIT_API_ERROR = 1
EXIT_USAGE_ERROR = 2  # argparse's own exit code for invalid arguments
EXIT_CODES: dict[ErrorKind, int] = {
    ErrorKind.API: EXIT_API_ERROR,
    ErrorKind.MISSING_API_KEY: 3,
    ErrorKind.AUTHENTICATION: 4,
    ErrorKind.RATE_LIMIT: 5,
    ErrorKind.TIMEOUT: 6,
    ErrorKind.CONNECTION: 7,
}


def positive_int(value: str) -> int:
    """argparse type for integers greater than zero."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid integer: {value!r}") from None
    if number <= 0:
        raise argparse.ArgumentTypeError(f"must be greater than 0, got {number}")
    return number


def build_parser() -> argparse.ArgumentParser:
    """Create the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROG_NAME,
        description="Educational multi-provider LLM command-line client.",
        epilog=(
            f"Run without a prompt to start an interactive chat.\n"
            f'example: {PROG_NAME} "What is a neural network?"'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        help="prompt text for a one-shot request; omit to start an interactive chat",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--provider",
        choices=PROVIDERS,
        default="openai",
        help="LLM provider (default: %(default)s)",
    )
    parser.add_argument(
        "--model",
        default=openai_provider.DEFAULT_MODEL,
        metavar="MODEL_ID",
        help="model identifier (default: %(default)s)",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=openai_provider.REASONING_EFFORTS,
        default=openai_provider.DEFAULT_REASONING_EFFORT,
        metavar="EFFORT",
        help="reasoning effort: %(choices)s (default: %(default)s)",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=positive_int,
        metavar="N",
        help="maximum number of tokens to generate",
    )
    parser.add_argument(
        "--usage",
        action="store_true",
        help="print token usage to stderr after each response",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="use the line-oriented chat instead of the fullscreen interface",
    )
    return parser


def run_prompt(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Send the prompt to the provider and print the result."""
    try:
        result = openai_provider.generate_text(
            prompt=args.prompt,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            max_output_tokens=args.max_output_tokens,
        )
    except OpenAIProviderError as error:
        print(f"{PROG_NAME}: error: {error}", file=stderr)
        return EXIT_CODES[error.kind]

    print(result.text, file=stdout)
    if args.usage:
        print(format_usage(result.usage), file=stderr)
    return EXIT_OK


def run_interactive(args: argparse.Namespace) -> int:
    """Start an interactive chat session with the configured model."""
    try:
        client = openai_provider.create_client()
    except OpenAIProviderError as error:
        print(f"{PROG_NAME}: error: {error}", file=sys.stderr)
        return EXIT_CODES[error.kind]

    reply_fn = functools.partial(
        openai_provider.generate_reply,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        max_output_tokens=args.max_output_tokens,
        client=client,
    )
    settings = chat.ChatSettings(
        provider=args.provider, model=args.model, show_usage=args.usage
    )
    if not args.plain and is_fullscreen_terminal():
        tui = load_tui()
        if tui is not None:
            return tui.run_tui(settings, reply_fn)
    chat.enable_line_editing()
    return chat.run_chat(settings, reply_fn, input_fn=input)


def is_fullscreen_terminal() -> bool:
    """True when stdin and stdout are terminals capable of fullscreen output."""
    return (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and os.environ.get("TERM", "") not in ("", "dumb")
    )


def load_tui() -> ModuleType | None:
    """Import the curses UI lazily; return None where curses is unavailable."""
    try:
        from neurobrau_cli.ui import tui
    except ImportError:
        return None
    return tui


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.prompt is None:
        return run_interactive(args)
    if not args.prompt.strip():
        parser.error("prompt must not be empty")
    return run_prompt(args, sys.stdout, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
