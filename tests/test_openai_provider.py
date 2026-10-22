"""Tests for the OpenAI provider using a mocked SDK client."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx2
import openai
import pytest

from neurobrau_cli.conversation import Message
from neurobrau_cli.providers.base import GenerationOptions
from neurobrau_cli.providers import openai_provider
from neurobrau_cli.providers.openai_provider import (
    ErrorKind,
    GenerationResult,
    OpenAIProviderError,
    TokenUsage,
)

REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/responses")


def make_client(output_text: str = "Hello!", usage: object | None = None) -> MagicMock:
    client = MagicMock()
    client.responses.create.return_value = SimpleNamespace(
        output_text=output_text, usage=usage
    )
    return client


def make_status_error(
    error_class: type[openai.APIStatusError], status_code: int, message: str
) -> openai.APIStatusError:
    response = httpx2.Response(status_code, request=REQUEST)
    return error_class(message, response=response, body=None)


def test_generate_text_returns_output_text_and_usage() -> None:
    usage = SimpleNamespace(input_tokens=5, output_tokens=7, total_tokens=12)
    client = make_client("Hi there", usage)

    result = openai_provider.generate_text("Say hi", client=client)

    assert result == GenerationResult(
        text="Hi there",
        usage=TokenUsage(input_tokens=5, output_tokens=7, total_tokens=12),
    )


def test_generate_text_uses_defaults_and_disables_storage() -> None:
    client = make_client()

    openai_provider.generate_text("Say hi", client=client)

    client.responses.create.assert_called_once_with(
        model="gpt-6-luna",
        input="Say hi",
        reasoning={"effort": "none"},
        store=False,
    )


def test_generate_text_passes_custom_options() -> None:
    client = make_client()

    openai_provider.generate_text(
        "Say hi",
        model="gpt-custom",
        reasoning_effort="high",
        max_output_tokens=50,
        client=client,
    )

    client.responses.create.assert_called_once_with(
        model="gpt-custom",
        input="Say hi",
        reasoning={"effort": "high"},
        store=False,
        max_output_tokens=50,
    )


def test_generate_reply_sends_history_as_input_messages() -> None:
    client = make_client("Fine.")
    history = [
        Message("user", "Hi"),
        Message("assistant", "Hello!"),
        Message("user", "How are you?"),
    ]

    result = openai_provider.generate_reply(
        history, model="gpt-custom", max_output_tokens=20, client=client
    )

    assert result.text == "Fine."
    client.responses.create.assert_called_once_with(
        model="gpt-custom",
        input=[
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "How are you?"},
        ],
        reasoning={"effort": "none"},
        store=False,
        max_output_tokens=20,
    )


def test_generate_reply_translates_sdk_errors() -> None:
    client = MagicMock()
    client.responses.create.side_effect = openai.APITimeoutError(request=REQUEST)

    with pytest.raises(OpenAIProviderError) as exc_info:
        openai_provider.generate_reply([Message("user", "Hi")], client=client)

    assert exc_info.value.kind is ErrorKind.TIMEOUT


def test_generate_text_handles_missing_usage() -> None:
    result = openai_provider.generate_text("Say hi", client=make_client())

    assert result.usage is None


@pytest.mark.parametrize("value", [None, "", "   "])
def test_read_api_key_raises_when_missing_or_blank(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is not None:
        monkeypatch.setenv("OPENAI_API_KEY", value)

    with pytest.raises(OpenAIProviderError, match="OPENAI_API_KEY") as exc_info:
        openai_provider.read_api_key()

    assert exc_info.value.kind is ErrorKind.MISSING_API_KEY


def test_read_api_key_returns_key_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    assert openai_provider.read_api_key() == "sk-test"


def test_generate_text_without_client_requires_api_key() -> None:
    with pytest.raises(OpenAIProviderError) as exc_info:
        openai_provider.generate_text("Say hi")

    assert exc_info.value.kind is ErrorKind.MISSING_API_KEY


@pytest.mark.parametrize(
    ("error", "kind", "expected"),
    [
        (
            make_status_error(openai.AuthenticationError, 401, "Incorrect API key"),
            ErrorKind.AUTHENTICATION,
            "authentication failed",
        ),
        (
            make_status_error(openai.RateLimitError, 429, "slow down"),
            ErrorKind.RATE_LIMIT,
            "rate limit",
        ),
        (openai.APITimeoutError(request=REQUEST), ErrorKind.TIMEOUT, "timed out"),
        (openai.APIConnectionError(request=REQUEST), ErrorKind.CONNECTION, "Could not connect"),
        (
            make_status_error(openai.BadRequestError, 400, "unknown model"),
            ErrorKind.API,
            r"\(HTTP 400\): unknown model",
        ),
    ],
)
def test_generate_text_translates_sdk_errors(
    error: openai.OpenAIError, kind: ErrorKind, expected: str
) -> None:
    client = MagicMock()
    client.responses.create.side_effect = error

    with pytest.raises(OpenAIProviderError, match=expected) as exc_info:
        openai_provider.generate_text("Say hi", client=client)

    assert exc_info.value.kind is kind
    assert exc_info.value.__cause__ is error


def test_authentication_error_message_does_not_echo_server_text() -> None:
    error = make_status_error(openai.AuthenticationError, 401, "Incorrect API key: sk-abc***xyz")

    message = str(openai_provider.translate_error(error))

    assert "sk-" not in message


# ----- OpenAIProvider: discovery, availability, reasoning fallback -----


def make_models_client(ids: list[str] | Exception) -> MagicMock:
    client = MagicMock()
    listing = client.with_options.return_value.models.list
    if isinstance(ids, Exception):
        listing.side_effect = ids
    else:
        listing.return_value = [SimpleNamespace(id=model_id) for model_id in ids]
    return client


def test_list_models_uses_models_api_with_bounded_timeout() -> None:
    client = make_models_client(["gpt-b", "gpt-a", "text-embedding-3-small", "tts-1", "whisper-1"])
    provider = openai_provider.OpenAIProvider(client)

    models = provider.list_models()

    assert [model.id for model in models] == ["gpt-a", "gpt-b"]
    client.with_options.assert_called_once_with(max_retries=0)
    client.with_options.return_value.models.list.assert_called_once_with(
        timeout=openai_provider.DISCOVERY_TIMEOUT
    )


def test_list_models_is_cached_until_refresh() -> None:
    client = make_models_client(["gpt-a"])
    provider = openai_provider.OpenAIProvider(client)

    provider.list_models()
    provider.list_models()
    provider.list_models(refresh=True)

    assert client.with_options.return_value.models.list.call_count == 2


def test_list_models_translates_errors() -> None:
    client = make_models_client(openai.APIConnectionError(request=REQUEST))

    with pytest.raises(OpenAIProviderError) as exc_info:
        openai_provider.OpenAIProvider(client).list_models()

    assert exc_info.value.kind is ErrorKind.CONNECTION


def test_availability_without_api_key_makes_no_request() -> None:
    availability = openai_provider.OpenAIProvider().check_availability()

    assert not availability.available
    assert "OPENAI_API_KEY is not set" in availability.message


def test_availability_with_working_api() -> None:
    provider = openai_provider.OpenAIProvider(make_models_client(["gpt-a"]))

    assert provider.check_availability().available


def test_availability_reports_authentication_failure() -> None:
    error = make_status_error(openai.AuthenticationError, 401, "bad key")
    provider = openai_provider.OpenAIProvider(make_models_client(error))

    availability = provider.check_availability()

    assert not availability.available
    assert "authentication failed" in availability.message


def test_provider_generate_sends_history_and_options() -> None:
    client = make_client("Fine.")
    provider = openai_provider.OpenAIProvider(client)

    result = provider.generate(
        [Message("user", "Hi")], GenerationOptions("gpt-x", "low", 32)
    )

    assert result.text == "Fine."
    client.responses.create.assert_called_once_with(
        model="gpt-x",
        input=[{"role": "user", "content": "Hi"}],
        reasoning={"effort": "low"},
        store=False,
        max_output_tokens=32,
    )


def test_model_rejecting_reasoning_is_retried_without_it_and_remembered() -> None:
    rejection = make_status_error(openai.BadRequestError, 400, "Unsupported parameter")
    rejection.param = "reasoning.effort"
    client = make_client("Answer")
    client.responses.create.side_effect = [rejection, client.responses.create.return_value,
                                           client.responses.create.return_value]
    provider = openai_provider.OpenAIProvider(client)
    options = GenerationOptions("gpt-classic", "none")

    assert provider.generate([Message("user", "Hi")], options).text == "Answer"
    provider.generate([Message("user", "Again")], options)

    calls = client.responses.create.call_args_list
    assert "reasoning" in calls[0].kwargs
    assert "reasoning" not in calls[1].kwargs
    assert "reasoning" not in calls[2].kwargs  # remembered: no second failed attempt


def test_other_bad_requests_are_not_retried() -> None:
    rejection = make_status_error(openai.BadRequestError, 400, "context too long")
    rejection.param = "input"
    client = make_client()
    client.responses.create.side_effect = rejection

    with pytest.raises(OpenAIProviderError, match="context too long"):
        openai_provider.OpenAIProvider(client).generate(
            [Message("user", "Hi")], GenerationOptions("gpt-x", "none")
        )

    assert client.responses.create.call_count == 1


def test_provider_default_model() -> None:
    assert openai_provider.OpenAIProvider().default_model() == "gpt-6-luna"


# ----- Task 007: capabilities, instructions, explicit reasoning -----


@pytest.mark.parametrize(
    ("model", "values", "known", "limit"),
    [
        ("gpt-6-luna", ("none",), True, None),
        ("gpt-5", ("minimal", "low", "medium", "high"), True, 128_000),
        ("gpt-5-2025-08-07", ("minimal", "low", "medium", "high"), True, 128_000),
        ("gpt-4o", (), True, 16_384),
        ("gpt-5-preview-xyz", (), False, None),  # not a dated snapshot: unknown
        ("my-fine-tune", (), False, None),
    ],
)
def test_known_capabilities(model: str, values: tuple, known: bool, limit: int | None) -> None:
    provider = openai_provider.OpenAIProvider()

    support = provider.reasoning_support(model)

    assert (support.values, support.known) == (values, known)
    assert provider.max_output_limit(model) == limit


def test_capability_lookup_makes_no_request() -> None:
    client = make_client()
    provider = openai_provider.OpenAIProvider(client)

    provider.reasoning_support("gpt-5")
    provider.max_output_limit("gpt-5")

    assert client.mock_calls == []


def test_instructions_are_sent_separately_from_input() -> None:
    client = make_client()

    openai_provider.OpenAIProvider(client).generate(
        [Message("user", "Hi")], GenerationOptions("gpt-x", None, None, instructions="Be brief.")
    )

    kwargs = client.responses.create.call_args.kwargs
    assert kwargs["instructions"] == "Be brief."
    assert kwargs["input"] == [{"role": "user", "content": "Hi"}]
    assert "reasoning" not in kwargs


def test_one_shot_instructions() -> None:
    client = make_client()

    openai_provider.generate_text("Hi", "gpt-x", None, client=client, instructions="Be brief.")

    assert client.responses.create.call_args.kwargs["instructions"] == "Be brief."


def test_explicit_reasoning_rejection_is_reported_not_retried() -> None:
    rejection = make_status_error(openai.BadRequestError, 400, "Unsupported value")
    rejection.param = "reasoning.effort"
    client = make_client()
    client.responses.create.side_effect = rejection
    options = GenerationOptions("gpt-x", "high", reasoning_explicit=True)

    with pytest.raises(OpenAIProviderError, match="does not accept reasoning effort 'high'.*F4"):
        openai_provider.OpenAIProvider(client).generate([Message("user", "Hi")], options)

    assert client.responses.create.call_count == 1


def test_rejected_default_marks_model_as_without_reasoning() -> None:
    rejection = make_status_error(openai.BadRequestError, 400, "Unsupported parameter")
    rejection.param = "reasoning.effort"
    client = make_client("ok")
    client.responses.create.side_effect = [rejection, client.responses.create.return_value]
    provider = openai_provider.OpenAIProvider(client)

    provider.generate([Message("user", "Hi")], GenerationOptions("gpt-6-luna", "none"))

    assert provider.reasoning_support("gpt-6-luna").values == ()
