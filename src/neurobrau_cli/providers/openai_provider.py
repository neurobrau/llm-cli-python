"""OpenAI provider built on the official SDK and the Responses API.

Conversation context is sent by replaying prior user/assistant text messages
as Responses API input items. Full output-item replay (including reasoning
items and tool calls) is not implemented yet.
"""

import os
import re
from collections.abc import Sequence
from typing import Any

import openai
from openai import OpenAI

from neurobrau_cli.conversation import Message
from neurobrau_cli.providers.base import (
    Availability,
    ErrorKind,
    GenerationOptions,
    GenerationResult,
    ModelInfo,
    ProviderError,
    ReasoningSupport,
    TokenUsage,
)

__all__ = [
    "ErrorKind",
    "GenerationResult",
    "OpenAIProvider",
    "OpenAIProviderError",
    "TokenUsage",
    "generate_reply",
    "generate_text",
]

API_KEY_ENV_VAR = "OPENAI_API_KEY"
DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_REASONING_EFFORT = "none"
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
DISCOVERY_TIMEOUT = 5.0  # seconds for the Models API

# The Models API lists every model the key can access, including ones that
# cannot produce text through the Responses API. These ID fragments hide the
# obvious ones from the model menu. It is a heuristic: a listed model can still
# be rejected, which is reported as a normal API error.
NON_TEXT_MODEL_MARKERS = (
    "embedding", "tts", "whisper", "dall-e", "moderation", "transcribe",
    "realtime", "audio", "image",
)


# Confirmed capabilities: (reasoning efforts, max output tokens). The Models
# API does not report these, so only models listed here get reasoning
# choices; others show "Default (support unknown)". An empty effort tuple
# means the model is known not to accept the reasoning parameter. Dated
# snapshots ("gpt-5-2025-08-07") share their base model's entry.
KNOWN_MODELS: dict[str, tuple[tuple[str, ...], int | None]] = {
    # The project's default model; "none" is its specified default effort.
    # Other efforts are not confirmed yet.
    "gpt-6-luna": (("none",), None),
    "gpt-5.2": (("none", "low", "medium", "high", "xhigh"), 128_000),
    "gpt-5.1": (("none", "low", "medium", "high"), 128_000),
    "gpt-5": (("minimal", "low", "medium", "high"), 128_000),
    "gpt-5-mini": (("minimal", "low", "medium", "high"), 128_000),
    "gpt-5-nano": (("minimal", "low", "medium", "high"), 128_000),
    "o3": (("low", "medium", "high"), 100_000),
    "o3-mini": (("low", "medium", "high"), 100_000),
    "o4-mini": (("low", "medium", "high"), 100_000),
    "gpt-4.1": ((), 32_768),
    "gpt-4.1-mini": ((), 32_768),
    "gpt-4.1-nano": ((), 32_768),
    "gpt-4o": ((), 16_384),
    "gpt-4o-mini": ((), 16_384),
}
SNAPSHOT_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def known_model(model: str) -> tuple[tuple[str, ...], int | None] | None:
    return KNOWN_MODELS.get(model) or KNOWN_MODELS.get(SNAPSHOT_SUFFIX.sub("", model))


def reasoning_support_for(model: str) -> ReasoningSupport:
    """Efforts from KNOWN_MODELS; unknown models get no confirmed values."""
    entry = known_model(model)
    if entry is None:
        return ReasoningSupport(known=False)
    efforts = entry[0]
    return ReasoningSupport(
        values=efforts, known=True, default="none" if "none" in efforts else None
    )


class OpenAIProviderError(ProviderError):
    """A user-facing, credential-free error raised by the OpenAI provider."""


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
    input_: str | list[dict[str, str]],
    model: str,
    reasoning_effort: str | None,
    max_output_tokens: int | None,
    instructions: str | None = None,
) -> dict[str, Any]:
    """Build keyword arguments for `client.responses.create`.

    `reasoning_effort=None` omits the reasoning parameter entirely, for models
    that reject it. `instructions` is the Responses API system/developer
    message; it applies to this request only and is not part of `input`.
    """
    request: dict[str, Any] = {"model": model, "input": input_}
    if instructions is not None:
        request["instructions"] = instructions
    if reasoning_effort is not None:
        request["reasoning"] = {"effort": reasoning_effort}
    request["store"] = False
    if max_output_tokens is not None:
        request["max_output_tokens"] = max_output_tokens
    return request


def to_input_items(messages: Sequence[Message]) -> list[dict[str, str]]:
    """Convert conversation messages into Responses API input messages."""
    return [{"role": message.role, "content": message.content} for message in messages]


def generate_text(
    prompt: str,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
    max_output_tokens: int | None = None,
    client: OpenAI | None = None,
    instructions: str | None = None,
) -> GenerationResult:
    """Send a one-shot prompt to the Responses API and return its text output."""
    request = build_request(prompt, model, reasoning_effort, max_output_tokens, instructions)
    return send_request(request, client)


def generate_reply(
    messages: Sequence[Message],
    model: str = DEFAULT_MODEL,
    reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
    max_output_tokens: int | None = None,
    client: OpenAI | None = None,
    instructions: str | None = None,
) -> GenerationResult:
    """Send a conversation, oldest message first, and return the model's reply."""
    request = build_request(
        to_input_items(messages), model, reasoning_effort, max_output_tokens, instructions
    )
    return send_request(request, client)


def send_request(request: dict[str, Any], client: OpenAI | None = None) -> GenerationResult:
    """Call the Responses API and convert SDK errors into provider errors."""
    if client is None:
        client = create_client()

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


def rejects_reasoning(error: ProviderError) -> bool:
    """True if the API refused the request because of the reasoning parameter."""
    cause = error.__cause__
    param = getattr(cause, "param", None) if isinstance(cause, openai.BadRequestError) else None
    return isinstance(param, str) and param.startswith("reasoning")


def is_text_model(model_id: str) -> bool:
    return not any(marker in model_id for marker in NON_TEXT_MODEL_MARKERS)


class OpenAIProvider:
    """`Provider` implementation for the OpenAI Responses API."""

    name = "openai"
    label = "OpenAI"

    def __init__(self, client: OpenAI | None = None) -> None:
        self._client = client
        self._models: list[ModelInfo] | None = None
        self._no_reasoning: set[str] = set()  # models that rejected the reasoning parameter

    def client(self) -> OpenAI:
        """The SDK client, created on first use. Raises if OPENAI_API_KEY is missing."""
        if self._client is None:
            self._client = create_client()
        return self._client

    def generate(self, messages: Sequence[Message], options: GenerationOptions) -> GenerationResult:
        effort = options.reasoning_effort
        if effort is not None and not options.reasoning_explicit and options.model in self._no_reasoning:
            effort = None
        try:
            return self._generate(messages, options, effort)
        except OpenAIProviderError as error:
            if effort is None or not rejects_reasoning(error):
                raise
            if options.reasoning_explicit:
                # Never silently drop a value the user chose.
                raise OpenAIProviderError(
                    f"{options.model} does not accept reasoning effort '{effort}' "
                    f"({error}). Choose another value with F4 or --reasoning-effort."
                ) from error
        # A built-in default the model rejects: retry once without it and
        # remember the model, instead of failing every message.
        self._no_reasoning.add(options.model)
        return self._generate(messages, options, None)

    def _generate(
        self, messages: Sequence[Message], options: GenerationOptions, effort: str | None
    ) -> GenerationResult:
        kwargs: dict[str, Any] = {}
        if options.instructions is not None:
            kwargs["instructions"] = options.instructions
        return generate_reply(
            list(messages),
            model=options.model,
            reasoning_effort=effort,
            max_output_tokens=options.max_output_tokens,
            client=self.client(),
            **kwargs,
        )

    def reasoning_support(self, model: str) -> ReasoningSupport:
        """From KNOWN_MODELS only; no request. A rejection seen this session wins."""
        if model in self._no_reasoning:
            return ReasoningSupport(known=True)
        return reasoning_support_for(model)

    def max_output_limit(self, model: str) -> int | None:
        entry = known_model(model)
        return entry[1] if entry else None

    def list_models(self, refresh: bool = False) -> list[ModelInfo]:
        """Text-capable model IDs from the Models API, sorted. One bounded request."""
        if self._models is not None and not refresh:
            return self._models
        client = self.client()
        try:
            page = client.with_options(max_retries=0).models.list(timeout=DISCOVERY_TIMEOUT)
            ids = sorted({model.id for model in page if is_text_model(model.id)})
        except openai.OpenAIError as error:
            raise translate_error(error) from error
        self._models = [ModelInfo(model_id) for model_id in ids]
        return self._models

    def check_availability(self) -> Availability:
        """Credentials are present and the API answers (reuses the model list)."""
        try:
            self.list_models()
        except ProviderError as error:
            return Availability(False, str(error))
        return Availability(True)

    def default_model(self) -> str:
        return DEFAULT_MODEL
