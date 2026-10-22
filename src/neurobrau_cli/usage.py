"""Per-request usage records: what each completed (or incomplete) API request cost in tokens.

API usage is not the same as conversation length: every request sends the
whole history again, so input tokens are billed again on each turn. Records
store the provider's reported counts unchanged; nothing is estimated from text.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from neurobrau_cli.providers.base import TokenUsage

COUNT_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
STATUSES = ("completed", "incomplete")


@dataclass(frozen=True)
class UsageRecord:
    """Usage of one request. `status` is "incomplete" for answers cut off early
    (still billed by the provider, but not part of the conversation)."""

    at: datetime
    provider: str
    model: str
    usage: TokenUsage
    service_tier: str | None = None
    status: str = "completed"


def add_usage(total: TokenUsage | None, usage: TokenUsage | None) -> TokenUsage | None:
    """Sum each count; a count never reported by any request stays None."""
    if usage is None:
        return total
    if total is None:
        return usage

    def plus(a: int | None, b: int | None) -> int | None:
        return b if a is None else a if b is None else a + b

    return TokenUsage(*(plus(getattr(total, f), getattr(usage, f)) for f in COUNT_FIELDS))


def total_usage(records: Iterable[UsageRecord]) -> TokenUsage | None:
    total: TokenUsage | None = None
    for record in records:
        total = add_usage(total, record.usage)
    return total


def record_to_json(record: UsageRecord, format_timestamp: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "at": format_timestamp(record.at),
        "provider": record.provider,
        "model": record.model,
        "status": record.status,
    }
    if record.service_tier is not None:
        data["service_tier"] = record.service_tier
    for name in COUNT_FIELDS:
        value = getattr(record.usage, name)
        if value is not None:
            data[name] = value
    return data


def record_from_json(data: object, parse_timestamp: Any) -> UsageRecord | None:
    """Parse one stored record; None if it is malformed (it is then skipped)."""
    if not isinstance(data, dict):
        return None
    try:
        at = parse_timestamp(data.get("at"), "at")
    except ValueError:
        return None
    provider, model = data.get("provider"), data.get("model")
    status, tier = data.get("status", "completed"), data.get("service_tier")
    if not (isinstance(provider, str) and isinstance(model, str) and status in STATUSES):
        return None
    if tier is not None and not isinstance(tier, str):
        return None
    counts = {}
    for name in COUNT_FIELDS:
        value = data.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            return None
        counts[name] = value
    return UsageRecord(at, provider, model, TokenUsage(**counts), tier, status)
