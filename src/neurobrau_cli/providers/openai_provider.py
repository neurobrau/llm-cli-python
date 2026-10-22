"""OpenAI provider built on the official SDK and the Responses API."""

import os
from dataclasses import dataclass
from enum import Enum
from typing import Any

import openai
from openai import OpenAI

API_KEY_ENV_VAR = "OPENAI_API_KEY"
DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_REASONING_EFFORT = "none"
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


class ErrorKind(Enum):
    """Categories of provider failures, so callers can react to each one."""

    MISSING_API_KEY = "missing_api_key"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    API = "api"


class OpenAIProviderError(Exception):
    """A user-facing, credential-free error raised by the OpenAI provider."""

    def __init__(self, message: str, kind: ErrorKind = ErrorKind.API) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class TokenUsage:
    """Token counts reported by the API."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class GenerationResult:
    """Text output and optional token usage of a single generation."""

    text: str
    usage: TokenUsage | None = None


def read_api_key() -> str:
    """Return the API key from the process environment."""
    api_key = os.environ.get(API_KEY_ENV_VAR, "").strip()
    if not api_key:
        raise OpenAIProviderError(
            f"{API_KEY_ENV_VAR} is not set. Export it in your environment and try again.",
            ErrorKind.MISSING_API_KEY,
        )
    return api_key


def create_client() -> OpenAI:
    """Create an SDK client using the API key from the environment."""
    return OpenAI(api_key=read_api_key())


def build_request(
    prompt: str,
    model: str,
    reasoning_effort: str,
    max_output_tokens: int | None,
) -> dict[str, Any]:
    """Build keyword arguments for `client.responses.create`."""
    request: dict[str, Any] = {
        "model": model,
        "input": prompt,
        "reasoning": {"effort": reasoning_effort},
        "store": False,
    }
    if max_output_tokens is not None:
        request["max_output_tokens"] = max_output_tokens
    return request


def generate_text(
    prompt: str,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    max_output_tokens: int | None = None,
    client: OpenAI | None = None,
) -> GenerationResult:
    """Send a one-shot prompt to the Responses API and return its text output."""
    if client is None:
        client = create_client()

    request = build_request(prompt, model, reasoning_effort, max_output_tokens)
    try:
        response = client.responses.create(**request)
    except openai.OpenAIError as error:
        raise translate_error(error) from error

    return GenerationResult(text=response.output_text, usage=extract_usage(response.usage))


def extract_usage(usage: Any) -> TokenUsage | None:
    """Convert the SDK usage object into TokenUsage, if usage was reported."""
    if usage is None:
        return None
    return TokenUsage(
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        total_tokens=getattr(usage, "total_tokens", None),
    )


def translate_error(error: openai.OpenAIError) -> OpenAIProviderError:
    """Map an SDK exception to a categorized, credential-free provider error."""
    match error:
        case openai.AuthenticationError():
            return OpenAIProviderError(
                f"OpenAI authentication failed. Check that {API_KEY_ENV_VAR} is valid.",
                ErrorKind.AUTHENTICATION,
            )
        case openai.RateLimitError():
            return OpenAIProviderError(
                "OpenAI rate limit or quota exceeded. Wait and try again, "
                "or check your plan and billing.",
                ErrorKind.RATE_LIMIT,
            )
        case openai.APITimeoutError():
            return OpenAIProviderError("Request to OpenAI timed out.", ErrorKind.TIMEOUT)
        case openai.APIConnectionError():
            return OpenAIProviderError(
                "Could not connect to OpenAI. Check your network connection.",
                ErrorKind.CONNECTION,
            )
        case openai.APIStatusError(status_code=status_code):
            return OpenAIProviderError(f"OpenAI API error (HTTP {status_code}): {error.message}")
        case _:
            return OpenAIProviderError(f"OpenAI request failed: {type(error).__name__}")
