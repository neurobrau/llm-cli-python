"""Tests for the OpenAI provider using a mocked SDK client."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx2
import openai
import pytest

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
