"""Tests for usage records: accounting, persistence, backward compatibility and /usage."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from neurobrau_cli.chat import ChatSettings, run_chat, usage_report
from neurobrau_cli.conversation import Message
from neurobrau_cli.providers import openai_provider
from neurobrau_cli.providers.base import (
    GenerationOptions,
    GenerationResult,
    ProviderError,
    StreamCompleted,
    StreamFailed,
    StreamIncomplete,
    TextDelta,
    TokenUsage,
)
from neurobrau_cli.session import ChatSession
from neurobrau_cli.storage import HistoryStore, StoredConversation
from neurobrau_cli.usage import UsageRecord, add_usage, total_usage

USAGE = TokenUsage(1_000, 200, 1_200, cached_input_tokens=600, cache_write_tokens=100,
                   reasoning_tokens=50)


@pytest.fixture
def store(tmp_path: Path) -> HistoryStore:
    return HistoryStore(tmp_path / "conversations")


def reply(usage: TokenUsage | None = USAGE, tier: str | None = "default"):
    def reply_fn(messages: Sequence[Message]) -> GenerationResult:
        return GenerationResult(f"echo {len(messages)}", usage, service_tier=tier)
    return reply_fn


def stream_of(*events: object):
    def stream_fn(messages: Sequence[Message]):
        yield from events
    return stream_fn


# ----- totals -----


def test_totals_sum_every_reported_count() -> None:
    a = TokenUsage(10, 5, 15, cached_input_tokens=4, reasoning_tokens=2)
    b = TokenUsage(20, 7, 27, cached_input_tokens=None, cache_write_tokens=3)

    assert add_usage(a, b) == TokenUsage(30, 12, 42, 4, 3, 2)
    assert add_usage(None, a) == a and add_usage(a, None) == a


def test_replayed_history_is_billed_again() -> None:
    """Input grows with the conversation: request 2 re-sends request 1's messages."""
    session = ChatSession(provider="openai", model="gpt-6-luna")
    sizes = iter([100, 180])

    def reply_fn(messages: Sequence[Message]) -> GenerationResult:
        return GenerationResult("ok", TokenUsage(next(sizes), 20, None))

    session.send("One", reply_fn)
    session.send("Two", reply_fn)

    assert total_usage(session.requests).input_tokens == 280  # billed input, not conversation size
    assert session.requests[-1].usage.input_tokens == 180  # what the conversation costs now


# ----- recording in the session -----


def test_completed_request_is_recorded_once_with_details(store: HistoryStore) -> None:
    session = ChatSession.restore(store, provider="openai", model="gpt-6-luna")

    session.send("Hi", reply())

    [record] = session.requests
    assert (record.provider, record.model, record.service_tier, record.status) == (
        "openai", "gpt-6-luna", "default", "completed"
    )
    assert record.usage == USAGE


def test_streamed_completion_is_recorded_once() -> None:
    session = ChatSession(provider="ollama", model="qwen3.5:4b")
    events = stream_of(TextDelta("a"), TextDelta("b"), StreamCompleted(GenerationResult("ab", USAGE)))

    session.send_stream("Hi", events, lambda delta: None)

    assert len(session.requests) == 1
    assert total_usage(session.requests) == USAGE


def test_incomplete_stream_is_recorded_as_incomplete_without_messages(store: HistoryStore) -> None:
    session = ChatSession.restore(store, provider="openai", model="gpt-6-luna")
    session.send("Before", reply())
    events = stream_of(TextDelta("cut"), StreamIncomplete("token limit", "cut", TokenUsage(500, 64, 564)))

    session.send_stream("Hi", events, lambda delta: None)

    assert [r.status for r in session.requests] == ["completed", "incomplete"]
    assert [m.content for m in session.messages] == ["Before", "echo 1"]
    saved = ChatSession.restore(store)
    assert [r.status for r in saved.requests] == ["completed", "incomplete"]


@pytest.mark.parametrize(
    "events",
    [
        [TextDelta("x"), StreamFailed(ProviderError("boom"), "x")],
        [TextDelta("x"), StreamIncomplete("cut", "x", None)],  # no usage reported: nothing to record
    ],
)
def test_failed_or_unreported_streams_record_nothing(events: list[object]) -> None:
    session = ChatSession(provider="openai", model="gpt-6-luna")

    session.send_stream("Hi", stream_of(*events), lambda delta: None)

    assert session.requests == []


def test_cancelled_stream_records_nothing() -> None:
    session = ChatSession(provider="openai", model="gpt-6-luna")

    def interrupt(delta: object) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        session.send_stream("Hi", stream_of(TextDelta("x"), StreamCompleted(GenerationResult("x", USAGE))),
                            interrupt)

    assert session.requests == []


def test_missing_usage_is_not_estimated() -> None:
    session = ChatSession(provider="openai", model="gpt-6-luna")

    session.send("Hi", reply(usage=None))

    assert session.requests == []


# ----- persistence and compatibility -----


def test_usage_survives_restart(store: HistoryStore) -> None:
    session = ChatSession.restore(store, provider="openai", model="gpt-6-luna")
    session.send("One", reply())
    session.send("Two", reply(tier=None))

    restored = ChatSession.restore(store)

    def stored_precision(record: UsageRecord) -> UsageRecord:  # timestamps keep milliseconds
        return UsageRecord(record.at.replace(microsecond=record.at.microsecond // 1000 * 1000),
                           record.provider, record.model, record.usage, record.service_tier,
                           record.status)

    assert restored.requests == [stored_precision(r) for r in session.requests]
    assert total_usage(restored.requests).input_tokens == 2_000


def test_stored_record_format(store: HistoryStore) -> None:
    session = ChatSession.restore(store, provider="openai", model="gpt-6-luna")
    session.send("Hi", reply())

    data = json.loads(store.path_for(session.id).read_text())

    [entry] = data["requests"]
    assert set(entry) == {"at", "provider", "model", "status", "service_tier", "input_tokens",
                          "output_tokens", "total_tokens", "cached_input_tokens",
                          "cache_write_tokens", "reasoning_tokens"}
    assert entry["cached_input_tokens"] == 600 and entry["at"].endswith("Z")
    assert list(data)[-1] == "requests"


def test_old_files_without_usage_still_load(store: HistoryStore) -> None:
    store.ensure_directory()
    store.path_for("11111111-1111-4111-8111-111111111111").write_text(json.dumps({
        "schema_version": 1, "id": "11111111-1111-4111-8111-111111111111",
        "created_at": "2026-10-21T12:00:00.000Z", "updated_at": "2026-10-21T12:00:00.000Z",
        "messages": [{"role": "user", "content": "Old"}],
    }))

    session = ChatSession.restore(store)

    assert session.messages[0].content == "Old"
    assert session.requests == []
    assert usage_report(session) == "Token usage: no API requests in this conversation yet."


def test_files_without_requests_are_written_as_before(store: HistoryStore) -> None:
    store.save(StoredConversation("11111111-1111-4111-8111-111111111111",
                                  datetime(2026, 10, 21, tzinfo=UTC), datetime(2026, 10, 21, tzinfo=UTC)))

    data = json.loads(store.path_for("11111111-1111-4111-8111-111111111111").read_text())

    assert "requests" not in data


def test_malformed_usage_records_are_skipped(store: HistoryStore) -> None:
    good = {"at": "2026-10-21T12:00:00.000Z", "provider": "openai", "model": "gpt-6-luna",
            "status": "completed", "input_tokens": 5, "output_tokens": 1, "future_field": 1}
    store.ensure_directory()
    store.path_for("11111111-1111-4111-8111-111111111111").write_text(json.dumps({
        "schema_version": 1, "id": "11111111-1111-4111-8111-111111111111",
        "created_at": "2026-10-21T12:00:00.000Z", "updated_at": "2026-10-21T12:00:00.000Z",
        "messages": [],
        "requests": [good, {"at": "nope"}, {**good, "input_tokens": -1}, {**good, "status": "x"},
                     {**good, "output_tokens": "1"}, 42],
    }))

    session = ChatSession.restore(store)

    assert session.requests == [UsageRecord(datetime(2026, 10, 21, 12, tzinfo=UTC), "openai", "gpt-6-luna",
                                            TokenUsage(5, 1), None, "completed")]


def test_usage_stays_with_its_selection_when_switching(store: HistoryStore) -> None:
    session = ChatSession.restore(store, provider="openai", model="gpt-6-luna")
    session.send("Paid", reply())
    session.switch("ollama", "qwen3.5:4b")
    session.send("Local", reply(TokenUsage(30, 5, 35), tier=None))

    assert [r.provider for r in session.requests] == ["ollama"]
    session.switch("openai", "gpt-6-luna")
    assert [r.provider for r in session.requests] == ["openai"]
    restarted = ChatSession.restore(store)
    assert [r.provider for r in restarted.requests] == ["openai"]


def test_reset_and_cleanup_start_with_no_usage(store: HistoryStore) -> None:
    session = ChatSession.restore(store, provider="openai", model="gpt-6-luna")
    session.send("Hi", reply())

    session.reset()
    assert session.requests == []
    session.send("Again", reply())
    session.cleanup()
    assert session.requests == []


# ----- /usage -----


def test_usage_report_details_and_estimate() -> None:
    session = ChatSession(provider="openai", model="gpt-6-luna")
    session.send("One", reply())
    session.send("Two", reply(TokenUsage(1_500, 100, 1_600, 1_000, 0, 0)))

    report = usage_report(session)

    assert report.splitlines()[:7] == [
        "Conversation usage (2 request(s), as reported by the API):",
        "Input tokens: 2,500",
        "  of which cached: 1,600",
        "  of which cache writes: 100",
        "Output tokens: 300",
        "  of which reasoning: 50",
        "Total tokens: 2,800",
    ]
    assert "Latest request input: 1,500 tokens (the whole history is sent, and billed, with every request)" in report
    # 300×0.10 + 600×0.01 + 100×0.125 + 200×0.50 = 148.5 and 500×0.10 + 1000×0.01 + 100×0.50 = 110
    # → $258.5 per 1M tokens = $0.0002585, shown rounded to 6 decimals.
    assert "Estimated cost: $0.000259 (ESTIMATED, not a billed amount; pricing 2026-10-22)" in report


def test_usage_report_for_local_ollama() -> None:
    session = ChatSession(provider="ollama", model="qwen3.5:4b")
    session.send("Hi", reply(TokenUsage(30, 5, 35), tier=None))

    assert usage_report(session).splitlines()[-1] == "API cost: $0 (local Ollama inference)"


def test_plain_chat_usage_command(capsys: pytest.CaptureFixture[str]) -> None:
    session = ChatSession(provider="openai", model="gpt-6-luna")
    lines = iter(["Hi", "/usage", "\\usage"])

    def input_fn(prompt: str) -> str:
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    run_chat(ChatSettings("openai", "gpt-6-luna"), reply(), input_fn=input_fn, session=session)

    out = capsys.readouterr().out
    assert out.count("Conversation usage (1 request(s)") == 2
    assert "Estimated cost:" in out


# ----- OpenAI usage details and tier -----


def test_openai_usage_details_and_service_tier() -> None:
    response = SimpleNamespace(
        output_text="Hi", service_tier="flex",
        usage=SimpleNamespace(
            input_tokens=1_000, output_tokens=300, total_tokens=1_300,
            input_tokens_details=SimpleNamespace(cached_tokens=700, cache_write_tokens=200),
            output_tokens_details=SimpleNamespace(reasoning_tokens=120),
        ),
    )
    client = MagicMock()
    client.responses.create.return_value = response

    result = openai_provider.OpenAIProvider(client).generate([Message("user", "Hi")],
                                                             GenerationOptions("gpt-6-luna"))

    assert result.usage == TokenUsage(1_000, 300, 1_300, 700, 200, 120)
    assert result.service_tier == "flex"


def test_openai_missing_details_stay_unknown() -> None:
    usage = openai_provider.extract_usage(SimpleNamespace(
        input_tokens=10, output_tokens=2, total_tokens=12,
        input_tokens_details=SimpleNamespace(cached_tokens=None),
        output_tokens_details=None,
    ))

    assert usage == TokenUsage(10, 2, 12, None, None, None)


def test_openai_stream_completion_carries_details_and_tier() -> None:
    usage = SimpleNamespace(input_tokens=50, output_tokens=5, total_tokens=55,
                            input_tokens_details=SimpleNamespace(cached_tokens=40, cache_write_tokens=0),
                            output_tokens_details=SimpleNamespace(reasoning_tokens=3))
    response = SimpleNamespace(id="r", model="gpt-6-luna", output_text="", usage=usage, service_tier="default")
    event = SimpleNamespace(type="response.completed", response=response)

    completed = openai_provider.handle_stream_event(event, ["Hi"])

    assert completed.result.usage == TokenUsage(50, 5, 55, 40, 0, 3)
    assert completed.result.service_tier == "default"
