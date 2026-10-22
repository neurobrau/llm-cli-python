"""ESTIMATED API costs from a small, versioned pricing catalog.

These are estimates, not billed amounts. Only prices verified on OpenAI's
official pages are listed (see PRICING_SOURCES); anything that cannot be
priced reliably is reported as unavailable or qualified, never guessed.
All arithmetic uses Decimal.

Billing rules applied (from the official pricing and prompt-caching docs):
* `cached_input_tokens` and `cache_write_tokens` are parts of
  `input_tokens`; ordinary input = input - cached - writes. Each part is
  billed at its own rate (cache-write rates replace, not add to, the input
  rate). Models without a cache-write price bill writes as ordinary input.
* Reasoning tokens are part of `output_tokens` and billed as output.
* Above a model's long-context threshold (input tokens of the request), the
  long-context rates apply to the whole request.
* Processing tiers scale the Standard rates (Flex/Batch 50%, Fast 2x) for
  models whose tiers are listed; a tier without a known price is unavailable.
* This app never sets `prompt_cache_retention`, so the default "30m" cache
  write rate applies. Regional/data-residency endpoints (+10%) are not used.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from neurobrau_cli.providers.base import TokenUsage
from neurobrau_cli.usage import UsageRecord

PRICING_VERSION = "2026-10-22"
PRICING_SOURCES = (
    "https://developers.openai.com/api/docs/pricing",
    "https://developers.openai.com/api/docs/models/gpt-6-luna",
    "https://developers.openai.com/api/docs/models/gpt-5.1",
    "https://developers.openai.com/api/docs/guides/prompt-caching",
)
PER_TOKENS = Decimal(1_000_000)  # catalog prices are USD per 1M tokens
SNAPSHOT_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Rates:
    """USD per 1M tokens. `cache_write` None: writes are billed as ordinary input."""

    input: Decimal
    cached_input: Decimal
    output: Decimal
    cache_write: Decimal | None = None


@dataclass(frozen=True)
class ModelPricing:
    standard: Rates
    long_context: Rates | None = None
    long_context_threshold: int | None = None  # input tokens; above it, long-context rates
    tiers: Mapping[str, Decimal] = field(default_factory=lambda: {"default": Decimal(1)})


D = Decimal
GPT_6_TIERS = {"default": D(1), "flex": D("0.5"), "batch": D("0.5"), "fast": D(2), "priority": D(2)}

# Verified 2026-10-22 on the official pages listed in PRICING_SOURCES.
CATALOG: dict[str, ModelPricing] = {
    "gpt-6-luna": ModelPricing(
        standard=Rates(D("0.10"), D("0.01"), D("0.50"), D("0.125")),
        long_context=Rates(D("0.20"), D("0.02"), D("0.75"), D("0.25")),
        long_context_threshold=272_000,
        tiers=GPT_6_TIERS,  # "priority" was renamed "fast" on 2026-07-30
    ),
    "gpt-5.1": ModelPricing(
        standard=Rates(D("1.25"), D("0.125"), D("10.00")),  # no cache-write charge
    ),
}

ESTIMATED = "estimated"  # all rates and usage known
QUALIFIED = "qualified"  # computed, but on stated assumptions (see notes)
PARTIAL = "partial"  # some requests could not be priced: a lower bound
UNAVAILABLE = "unavailable"
FREE_LOCAL = "free_local"  # confirmed local inference: no API cost


@dataclass(frozen=True)
class CostEstimate:
    status: str
    amount: Decimal | None = None
    notes: tuple[str, ...] = ()


def pricing_for(model: str) -> ModelPricing | None:
    return CATALOG.get(model) or CATALOG.get(SNAPSHOT_SUFFIX.sub("", model))


def is_cloud_model(model: str) -> bool:
    tag = model.rpartition(":")[2]
    return tag == "cloud" or tag.endswith("-cloud")


def estimate(record: UsageRecord) -> CostEstimate:
    """ESTIMATED cost of one request."""
    if record.provider == "ollama":
        if is_cloud_model(record.model):
            return CostEstimate(UNAVAILABLE, notes=(f"{record.model} is an Ollama cloud model",))
        return CostEstimate(FREE_LOCAL, Decimal(0))
    if record.provider != "openai":
        return CostEstimate(UNAVAILABLE, notes=(f"no pricing for provider {record.provider}",))
    pricing = pricing_for(record.model)
    if pricing is None:
        return CostEstimate(UNAVAILABLE, notes=(f"no pricing data for {record.model}",))
    usage = record.usage
    if usage.input_tokens is None or usage.output_tokens is None:
        return CostEstimate(UNAVAILABLE, notes=("token usage was not reported",))

    notes: list[str] = []
    tier = record.service_tier
    if tier in (None, "auto"):
        multiplier = Decimal(1)
        notes.append("Standard tier assumed (tier not reported)")
    elif tier in pricing.tiers:
        multiplier = pricing.tiers[tier]
    else:
        return CostEstimate(UNAVAILABLE, notes=(f"no pricing for the {tier} tier of {record.model}",))

    long_context = (
        pricing.long_context is not None
        and pricing.long_context_threshold is not None
        and usage.input_tokens > pricing.long_context_threshold
    )
    rates = pricing.long_context if long_context and pricing.long_context else pricing.standard
    cached = usage.cached_input_tokens
    writes = usage.cache_write_tokens
    if cached is None:
        notes.append("cached tokens not reported (all input priced as uncached)")
        cached = 0
    if writes is None:
        if rates.cache_write is not None:
            notes.append("cache writes not reported (priced as ordinary input)")
        writes = 0
    ordinary = usage.input_tokens - cached - writes
    if ordinary < 0:
        return CostEstimate(UNAVAILABLE, notes=("inconsistent usage (cached + writes > input)",))
    write_rate = rates.cache_write if rates.cache_write is not None else rates.input
    amount = (
        ordinary * rates.input
        + cached * rates.cached_input
        + writes * write_rate
        + usage.output_tokens * rates.output
    ) * multiplier / PER_TOKENS
    return CostEstimate(QUALIFIED if notes else ESTIMATED, amount, tuple(notes))


def total_cost(records: Iterable[UsageRecord]) -> CostEstimate | None:
    """Combined estimate for a conversation; None if there are no requests."""
    estimates = [estimate(record) for record in records]
    if not estimates:
        return None
    statuses = {e.status for e in estimates}
    if statuses == {FREE_LOCAL}:
        return CostEstimate(FREE_LOCAL, Decimal(0))
    known = [e for e in estimates if e.amount is not None]
    notes = tuple(dict.fromkeys(note for e in estimates for note in e.notes))
    if not known:
        return CostEstimate(UNAVAILABLE, notes=notes)
    amount = sum((e.amount for e in known), Decimal(0))
    if UNAVAILABLE in statuses:
        return CostEstimate(PARTIAL, amount, notes)
    return CostEstimate(QUALIFIED if QUALIFIED in statuses else ESTIMATED, amount, notes)


def format_money(amount: Decimal) -> str:
    """USD for display: 6 decimals below one cent, else 4 (display rounding only)."""
    if amount == 0:
        return "$0"
    places = Decimal("0.000001") if amount < Decimal("0.01") else Decimal("0.0001")
    return f"${amount.quantize(places, rounding=ROUND_HALF_UP)}"


def format_cost_short(cost: CostEstimate | None) -> str:
    """Compact form for the settings panel: `$0.001234`, `~$…` qualified, `>=$…` partial."""
    if cost is None:
        return "-"
    if cost.status == FREE_LOCAL:
        return "$0 (local)"
    if cost.amount is None:
        return "unavailable"
    prefix = {QUALIFIED: "~", PARTIAL: ">="}.get(cost.status, "")
    return prefix + format_money(cost.amount)


def describe_cost(cost: CostEstimate | None) -> str:
    """One line for /usage, always labelled as an estimate."""
    if cost is None:
        return "Estimated cost: - (no requests yet)"
    if cost.status == FREE_LOCAL:
        return "API cost: $0 (local Ollama inference)"
    if cost.amount is None:
        return "Estimated cost: unavailable (" + "; ".join(cost.notes) + ")"
    label = {QUALIFIED: "approximately ", PARTIAL: "at least "}.get(cost.status, "")
    detail = f"; {'; '.join(cost.notes)}" if cost.notes else ""
    return (
        f"Estimated cost: {label}{format_money(cost.amount)} "
        f"(ESTIMATED, not a billed amount; pricing {PRICING_VERSION}{detail})"
    )


def usage_breakdown(usage: TokenUsage | None) -> list[str]:
    """`name: value` lines for every reported count."""
    if usage is None:
        return ["Token usage: not reported"]
    labels = (
        ("input_tokens", "Input tokens"),
        ("cached_input_tokens", "  of which cached"),
        ("cache_write_tokens", "  of which cache writes"),
        ("output_tokens", "Output tokens"),
        ("reasoning_tokens", "  of which reasoning"),
        ("total_tokens", "Total tokens"),
    )
    return [f"{label}: {getattr(usage, name):,}" for name, label in labels
            if getattr(usage, name) is not None]
