"""Shared terminal output formatting."""

from collections.abc import Sequence

from neurobrau_cli.conversation import Message
from neurobrau_cli.providers.openai_provider import TokenUsage


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


def format_transcript(messages: Sequence[Message]) -> str:
    """Plain-text transcript with `YOU > ` / `AI > ` labels, blank line between messages."""
    labels = {"user": "YOU > ", "assistant": "AI > "}
    return "\n\n".join(f"{labels[message.role]}{message.content}" for message in messages)
