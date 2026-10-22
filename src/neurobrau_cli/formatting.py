"""Shared terminal output formatting."""

from collections.abc import Mapping, Sequence

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


def format_stream_stats(metadata: Mapping[str, object]) -> str | None:
    """Duration line from stream metadata (Ollama reports durations), or None."""
    total = metadata.get("total_seconds")
    if not isinstance(total, float):
        return None
    details = [
        f"{label} {value:.2f} s"
        for key, label in (("load_seconds", "load"), ("eval_seconds", "generation"))
        if isinstance(value := metadata.get(key), float)
    ]
    return f"Duration: {total:.2f} s" + (f" ({', '.join(details)})" if details else "")
