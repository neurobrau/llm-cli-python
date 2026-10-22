"""Command-line entry point for NeuroBräu CLI."""

import argparse
import sys
from collections.abc import Sequence
from typing import TextIO

from neurobrau_cli import __version__
from neurobrau_cli.providers import openai_provider
from neurobrau_cli.providers.openai_provider import ErrorKind, OpenAIProviderError, TokenUsage

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
        epilog=f'example: {PROG_NAME} "What is a neural network?"',
    )
    parser.add_argument("prompt", nargs="?", help="prompt text to send to the model")
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
        help="print token usage to stderr",
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


def format_usage(usage: TokenUsage | None) -> str:
    """Format token usage for display, showing only reported values."""
    if usage is None:
        return "Token usage: not reported"
    fields = {
        "input": usage.input_tokens,
        "output": usage.output_tokens,
        "total": usage.total_tokens,
    }
    parts = [f"{name}={count}" for name, count in fields.items() if count is not None]
    return "Token usage: " + (", ".join(parts) if parts else "not reported")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        return EXIT_OK

    parser = build_parser()
    args = parser.parse_args(arguments)
    if args.prompt is None or not args.prompt.strip():
        parser.error("a non-empty prompt is required")
    return run_prompt(args, sys.stdout, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
