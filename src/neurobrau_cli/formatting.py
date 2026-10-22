"""Shared terminal output formatting."""

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
