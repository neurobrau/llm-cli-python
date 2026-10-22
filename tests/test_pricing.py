"""Tests for ESTIMATED cost calculation with known official prices."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from neurobrau_cli import pricing
from neurobrau_cli.pricing import (
    ESTIMATED,
    FREE_LOCAL,
    PARTIAL,
    QUALIFIED,
    UNAVAILABLE,
    describe_cost,
    estimate,
    format_cost_short,
    format_money,
    total_cost,
)
from neurobrau_cli.providers.base import TokenUsage
from neurobrau_cli.usage import UsageRecord

AT = datetime(2026, 10, 22, tzinfo=UTC)


def record(
    usage: TokenUsage,
    model: str = "gpt-6-luna",
    tier: str | None = "default",
    provider: str = "openai",
) -> UsageRecord:
    return UsageRecord(AT, provider, model, usage, tier)


def full(input_: int, output: int, cached: int = 0, writes: int = 0, reasoning: int = 0) -> TokenUsage:
    return TokenUsage(input_, output, input_ + output, cached, writes, reasoning)


def test_gpt_6_luna_standard_short_context() -> None:
    # 10,000 ordinary input @ $0.10, 4,000 cached @ $0.01, 1,000 writes @ $0.125, 2,000 output @ $0.50 per 1M
    cost = estimate(record(full(15_000, 2_000, cached=4_000, writes=1_000)))

    expected = (Decimal(10_000) * Decimal("0.10") + 4_000 * Decimal("0.01")
                + 1_000 * Decimal("0.125") + 2_000 * Decimal("0.50")) / 1_000_000
    assert cost == pricing.CostEstimate(ESTIMATED, expected)
    assert cost.amount == Decimal("0.002165")
    assert isinstance(cost.amount, Decimal)


def test_reasoning_tokens_are_part_of_output_not_added() -> None:
    with_reasoning = estimate(record(full(1_000, 2_000, reasoning=1_500)))
    without = estimate(record(full(1_000, 2_000)))

    assert with_reasoning.amount == without.amount == Decimal("0.0011")


def test_long_context_rates_apply_to_whole_request_above_272k() -> None:
    at_threshold = estimate(record(full(272_000, 1_000)))
    above = estimate(record(full(272_001, 1_000)))

    assert at_threshold.amount == (272_000 * Decimal("0.10") + 1_000 * Decimal("0.50")) / 1_000_000
    assert above.amount == (272_001 * Decimal("0.20") + 1_000 * Decimal("0.75")) / 1_000_000


@pytest.mark.parametrize(
    ("tier", "factor"),
    [("default", Decimal(1)), ("flex", Decimal("0.5")), ("batch", Decimal("0.5")),
     ("fast", Decimal(2)), ("priority", Decimal(2))],
)
def test_processing_tiers(tier: str, factor: Decimal) -> None:
    base = estimate(record(full(100_000, 10_000))).amount

    assert estimate(record(full(100_000, 10_000), tier=tier)).amount == base * factor


def test_unknown_tier_is_unavailable() -> None:
    cost = estimate(record(full(1_000, 1_000), tier="ultrafast"))

    assert cost.status == UNAVAILABLE
    assert "no pricing for the ultrafast tier" in cost.notes[0]


def test_missing_tier_is_a_qualified_standard_estimate() -> None:
    cost = estimate(record(full(1_000, 1_000), tier=None))

    assert cost.status == QUALIFIED
    assert cost.amount == Decimal("0.0006")
    assert cost.notes == ("Standard tier assumed (tier not reported)",)


def test_missing_cache_breakdown_is_qualified_upper_bound() -> None:
    cost = estimate(record(TokenUsage(10_000, 1_000, 11_000)))

    assert cost.status == QUALIFIED
    assert cost.amount == (10_000 * Decimal("0.10") + 1_000 * Decimal("0.50")) / 1_000_000
    assert "cached tokens not reported" in cost.notes[0]


def test_model_without_cache_write_price_bills_writes_as_input() -> None:
    cost = estimate(record(full(10_000, 0, cached=2_000, writes=3_000), model="gpt-5.1"))

    assert cost.amount == (8_000 * Decimal("1.25") + 2_000 * Decimal("0.125")) / 1_000_000
    assert cost.status == ESTIMATED


@pytest.mark.parametrize(
    ("usage", "model", "note"),
    [
        (full(1, 1), "gpt-4o", "no pricing data for gpt-4o"),
        (full(1, 1), "my-fine-tune", "no pricing data for my-fine-tune"),
        (TokenUsage(None, 5, None), "gpt-6-luna", "token usage was not reported"),
        (TokenUsage(10, None, None), "gpt-6-luna", "token usage was not reported"),
        (TokenUsage(10, 1, 11, cached_input_tokens=8, cache_write_tokens=5), "gpt-6-luna", "inconsistent"),
    ],
)
def test_unpriceable_requests(usage: TokenUsage, model: str, note: str) -> None:
    cost = estimate(record(usage, model=model))

    assert cost.status == UNAVAILABLE and cost.amount is None
    assert note in cost.notes[0]


def test_dated_snapshot_uses_base_model_prices() -> None:
    assert estimate(record(full(1_000, 1_000), model="gpt-6-luna-2026-09-01")).amount == Decimal("0.0006")


def test_ollama_local_is_free_and_cloud_is_not() -> None:
    local = estimate(record(full(10, 10), model="qwen3.5:4b", provider="ollama", tier=None))
    cloud = estimate(record(full(10, 10), model="glm-4.7:cloud", provider="ollama", tier=None))

    assert local == pricing.CostEstimate(FREE_LOCAL, Decimal(0))
    assert cloud.status == UNAVAILABLE and "cloud model" in cloud.notes[0]


def test_conversation_totals() -> None:
    a = record(full(1_000, 1_000))
    b = record(full(2_000, 1_000))

    assert total_cost([]) is None
    assert total_cost([a, b]) == pricing.CostEstimate(ESTIMATED, Decimal("0.0013"))
    partial = total_cost([a, record(full(1, 1), model="unknown-model")])
    assert (partial.status, partial.amount) == (PARTIAL, Decimal("0.0006"))
    qualified = total_cost([a, record(full(1_000, 1_000), tier=None)])
    assert qualified.status == QUALIFIED
    unavailable = total_cost([record(full(1, 1), model="x")])
    assert unavailable.status == UNAVAILABLE


@pytest.mark.parametrize(
    ("amount", "text"),
    [("0", "$0"), ("0.0000004", "$0.000000"), ("0.000165", "$0.000165"),
     ("0.0021654", "$0.002165"), ("0.01", "$0.0100"), ("12.345678", "$12.3457")],
)
def test_format_money(amount: str, text: str) -> None:
    assert format_money(Decimal(amount)) == text


def test_short_and_long_descriptions_are_labelled() -> None:
    estimated = pricing.CostEstimate(ESTIMATED, Decimal("0.002165"))
    qualified = pricing.CostEstimate(QUALIFIED, Decimal("0.0006"), ("Standard tier assumed",))
    partial = pricing.CostEstimate(PARTIAL, Decimal("0.0006"), ("no pricing data for x",))

    assert format_cost_short(estimated) == "$0.002165"
    assert format_cost_short(qualified) == "~$0.000600"
    assert format_cost_short(partial) == ">=$0.000600"
    assert format_cost_short(pricing.CostEstimate(FREE_LOCAL, Decimal(0))) == "$0 (local)"
    assert format_cost_short(pricing.CostEstimate(UNAVAILABLE)) == "unavailable"
    assert format_cost_short(None) == "-"
    assert describe_cost(estimated) == (
        f"Estimated cost: $0.002165 (ESTIMATED, not a billed amount; pricing {pricing.PRICING_VERSION})"
    )
    assert describe_cost(qualified).startswith("Estimated cost: approximately $0.000600 (ESTIMATED")
    assert "Standard tier assumed" in describe_cost(qualified)
    assert describe_cost(partial).startswith("Estimated cost: at least $0.000600")
    assert describe_cost(pricing.CostEstimate(FREE_LOCAL, Decimal(0))) == (
        "API cost: $0 (local Ollama inference)"
    )
    assert describe_cost(pricing.CostEstimate(UNAVAILABLE, notes=("no pricing data for x",))) == (
        "Estimated cost: unavailable (no pricing data for x)"
    )


def test_catalog_is_versioned_and_sourced() -> None:
    assert pricing.PRICING_VERSION == "2026-10-22"
    assert all(source.startswith("https://developers.openai.com/") for source in pricing.PRICING_SOURCES)
    assert all(isinstance(rate, Decimal) for model in pricing.CATALOG.values()
               for rate in vars(model.standard).values() if rate is not None)
