"""Command-line entry point for NeuroBräu CLI."""

import argparse
import os
import sys
from collections.abc import Sequence
from types import ModuleType
from typing import TextIO

from neurobrau_cli import __version__, chat
from neurobrau_cli.conversation import Message
from neurobrau_cli.engine import ChatEngine
from neurobrau_cli.formatting import format_stream_stats, format_transcript, format_usage
from neurobrau_cli.providers import ollama_provider, openai_provider
from neurobrau_cli.providers.base import (
    ErrorKind,
    GenerationOptions,
    Provider,
    ProviderError,
    StreamCompleted,
    StreamFailed,
    StreamIncomplete,
    TextDelta,
)
from neurobrau_cli.preferences import Overrides, Preferences, resolve_model_settings
from neurobrau_cli.session import ChatSession
from neurobrau_cli.settings import (
    ANSWER_STYLES,
    STYLE_INSTRUCTIONS,
    SettingsError,
    SettingsStore,
    default_settings_path,
)
from neurobrau_cli.storage import HistoryStore, default_history_dir

PROG_NAME = "neurobrau"
PROVIDERS = ("openai", "ollama")
DEFAULT_PROVIDER = "openai"

EXIT_OK = 0
EXIT_API_ERROR = 1
EXIT_USAGE_ERROR = 2  # argparse's own exit code for invalid arguments
EXIT_INCOMPLETE = 8  # streamed answer stopped early (e.g. token limit)
EXIT_INTERRUPTED = 130
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
        help=(
            f"LLM provider (default: {DEFAULT_PROVIDER}; an interactive chat without "
            "--provider/--model continues with the provider of the last conversation)"
        ),
    )
    parser.add_argument(
        "--model",
        metavar="MODEL_ID",
        help=(
            f"model identifier (default: {openai_provider.DEFAULT_MODEL} for openai, "
            "the most recently installed local model for ollama)"
        ),
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=openai_provider.REASONING_EFFORTS,
        metavar="EFFORT",
        help=(
            "reasoning effort: %(choices)s. Default: the saved choice for the model, "
            "else none where the model supports it (Ollama: thinking off)"
        ),
    )
    parser.add_argument(
        "--max-output-tokens",
        type=positive_int,
        metavar="N",
        help="maximum number of tokens to generate (default: saved choice, else the model's)",
    )
    parser.add_argument(
        "--answer-style",
        choices=ANSWER_STYLES,
        help="brief, normal or detailed answers (default: saved choice, else normal)",
    )
    parser.add_argument(
        "--usage",
        action="store_true",
        help="print token usage to stderr after each response",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help=(
            "print the answer while it is generated (one-shot and --plain chat; "
            "the fullscreen interface does not stream yet)"
        ),
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="use the line-oriented chat instead of the fullscreen interface",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="do not load or save conversation history for this run",
    )
    parser.add_argument(
        "--transcript-on-exit",
        action="store_true",
        help="print the conversation after the fullscreen interface closes",
    )
    return parser


def create_providers() -> dict[str, Provider]:
    """One instance per provider. Nothing connects until a provider is used."""
    return {
        "openai": openai_provider.OpenAIProvider(),
        "ollama": ollama_provider.OllamaProvider(),
    }


def report(error: ProviderError, stderr: TextIO) -> int:
    print(f"{PROG_NAME}: error: {error}", file=stderr)
    return EXIT_CODES[error.kind]


def report_settings_error(error: SettingsError, stderr: TextIO) -> int:
    print(f"{PROG_NAME}: error: {error}", file=stderr)
    return EXIT_USAGE_ERROR


def overrides_from(args: argparse.Namespace) -> Overrides:
    return Overrides(args.reasoning_effort, args.max_output_tokens, args.answer_style)


def load_preferences(args: argparse.Namespace) -> tuple[Preferences, list[str]]:
    """Saved settings (never credentials) plus this invocation's CLI flags."""
    try:
        store = SettingsStore(default_settings_path())
    except RuntimeError:  # home directory cannot be determined
        return Preferences(overrides=overrides_from(args)), [
            "Cannot determine the home directory; settings are not loaded or saved."
        ]
    settings, warnings = store.load()
    return Preferences(store, settings, overrides_from(args)), warnings


def run_prompt(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Send the prompt to the provider and print the result."""
    provider_name = args.provider or DEFAULT_PROVIDER
    preferences, warnings = load_preferences(args)
    provider = create_providers()[provider_name]
    instructions = STYLE_INSTRUCTIONS[preferences.answer_style]
    try:
        model = args.model or provider.default_model()
        resolved, notices = resolve_model_settings(
            provider,
            model,
            preferences.settings.for_model(provider_name, model),
            preferences.overrides,
            strict=True,
            # OpenAI's defaults come from a built-in table; for Ollama the
            # provider's own default avoids an extra /api/show request.
            include_default=provider_name == "openai",
        )
        for note in [*warnings, *notices]:
            print(f"{PROG_NAME}: note: {note}", file=stderr)
        if args.stream:
            options = GenerationOptions(
                model=model,
                reasoning_effort=resolved.reasoning,
                max_output_tokens=resolved.max_output_tokens,
                instructions=instructions,
                reasoning_explicit=resolved.reasoning_explicit,
            )
            return stream_prompt(provider, args, options, stdout, stderr)
        if provider_name == "openai":
            extra = {"instructions": instructions} if instructions else {}
            result = openai_provider.generate_text(
                prompt=args.prompt,
                model=model,
                reasoning_effort=resolved.reasoning,
                max_output_tokens=resolved.max_output_tokens,
                **extra,
            )
        else:
            options = GenerationOptions(
                model=model,
                reasoning_effort=resolved.reasoning,
                max_output_tokens=resolved.max_output_tokens,
                instructions=instructions,
                reasoning_explicit=resolved.reasoning_explicit,
            )
            result = provider.generate([Message("user", args.prompt)], options)
    except SettingsError as error:
        return report_settings_error(error, stderr)
    except ProviderError as error:
        return report(error, stderr)

    print(result.text, file=stdout)
    if args.usage:
        print(format_usage(result.usage), file=stderr)
    return EXIT_OK


def stream_prompt(
    provider: Provider,
    args: argparse.Namespace,
    options: GenerationOptions,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """One-shot streaming: print text as it arrives, then usage on success."""
    events = provider.stream([Message("user", args.prompt)], options)
    printed = False
    try:
        for event in events:
            if isinstance(event, TextDelta):
                stdout.write(event.text)
                stdout.flush()
                printed = True
            elif isinstance(event, (StreamCompleted, StreamIncomplete, StreamFailed)):
                break
        else:
            event = StreamFailed(ProviderError("The response stream ended without a result."))
    except KeyboardInterrupt:
        if printed:
            print(file=stdout, flush=True)
        print(f"{PROG_NAME}: interrupted", file=stderr)
        return EXIT_INTERRUPTED
    finally:
        close = getattr(events, "close", None)
        if close is not None:
            close()  # releases the network connection

    if printed or isinstance(event, StreamCompleted):
        print(file=stdout, flush=True)  # before any stderr message
    if isinstance(event, StreamCompleted):
        if args.usage:
            print(format_usage(event.result.usage), file=stderr)
            stats = format_stream_stats(event.metadata)
            if stats:
                print(stats, file=stderr)
        return EXIT_OK
    if isinstance(event, StreamIncomplete):
        print(f"{PROG_NAME}: warning: response incomplete: {event.reason}.", file=stderr)
        if args.usage and event.usage:
            print(format_usage(event.usage), file=stderr)
        return EXIT_INCOMPLETE
    return report(event.error, stderr)


def run_interactive(args: argparse.Namespace) -> int:
    """Start an interactive chat session with the configured model."""
    session = open_session(args)
    if session.provider is not None and session.provider not in PROVIDERS:
        # e.g. a hand-edited active.json: start fresh with the defaults
        warnings = session.startup_warnings
        session = ChatSession(session.store, provider=args.provider, model=args.model)
        session.startup_warnings = warnings
    provider_name = args.provider or session.provider or DEFAULT_PROVIDER
    providers = create_providers()
    try:
        if provider_name == "openai":
            # Check the API key now rather than at the first message.
            providers["openai"] = openai_provider.OpenAIProvider(openai_provider.create_client())
        model = args.model or session.model or providers[provider_name].default_model()
    except ProviderError as error:
        return report(error, sys.stderr)
    session.provider, session.model = provider_name, model

    engine = ChatEngine(providers, provider_name, model)
    preferences, warnings = load_preferences(args)
    try:
        notices = preferences.apply(engine, strict=True)
    except SettingsError as error:
        return report_settings_error(error, sys.stderr)
    session.startup_warnings.extend([*warnings, *notices])
    settings = display_settings(engine, args.usage)
    if not args.plain and is_fullscreen_terminal():
        tui = load_tui()
        if tui is not None:
            exit_code = tui.run_tui(
                settings, engine.reply, session, engine=engine, preferences=preferences
            )
            # curses has restored the terminal by now, so this is normal output.
            if args.transcript_on_exit and session.messages:
                print(format_transcript(session.messages))
            return exit_code
    chat.enable_line_editing()
    stream_fn = engine.stream if args.stream else None
    return chat.run_chat(
        settings, engine.reply, input_fn=input, session=session, stream_fn=stream_fn
    )


def display_settings(engine: ChatEngine, show_usage: bool) -> chat.ChatSettings:
    return chat.ChatSettings(
        provider=engine.provider,
        model=engine.model,
        show_usage=show_usage,
        reasoning_effort=engine.reasoning_effort,
        max_output_tokens=engine.max_output_tokens,
        answer_style=engine.answer_style,
    )


def open_session(args: argparse.Namespace) -> ChatSession:
    """Restore the latest saved conversation for the requested provider/model.

    Without --provider and --model, the most recent conversation of any
    provider is continued with its own provider and model.
    """
    if args.no_history:
        return ChatSession(provider=args.provider, model=args.model)
    try:
        directory = default_history_dir()
    except RuntimeError:  # home directory cannot be determined
        session = ChatSession(provider=args.provider, model=args.model)
        session.startup_warnings.append(
            "Cannot determine the home directory; conversation history is disabled."
        )
        return session
    return ChatSession.restore(HistoryStore(directory), provider=args.provider, model=args.model)


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
