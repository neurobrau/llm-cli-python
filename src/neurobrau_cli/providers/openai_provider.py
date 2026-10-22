"""OpenAI provider built on the official SDK and the Responses API.

Conversation context is sent by replaying prior user/assistant text messages
as Responses API input items. Full output-item replay (including reasoning
items and tool calls) is not implemented yet.
"""

import os
import re
from collections.abc import Iterator, Sequence
from contextlib import closing
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
    StreamCompleted,
    StreamEvent,
    StreamFailed,
    StreamIncomplete,
    TextDelta,
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
    # The project's default model. Without a saved or CLI choice this app
    # sends "none" (Task 002); the model's own documented default is "medium".
    "gpt-6-luna": (("none", "low", "medium", "high", "xhigh", "max"), None),
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
# The model's own default effort when no reasoning parameter is sent, where documented.
MODEL_DEFAULT_EFFORTS: dict[str, str] = {"gpt-6-luna": "medium"}
SNAPSHOT_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def known_model(model: str) -> tuple[tuple[str, ...], int | None] | None:
    return KNOWN_MODELS.get(model) or KNOWN_MODELS.get(SNAPSHOT_SUFFIX.sub("", model))


def reasoning_support_for(model: str) -> ReasoningSupport:
    """Efforts from KNOWN_MODELS; unknown models get no confirmed values."""
    entry = known_model(model)
    if entry is None:
        return ReasoningSupport(known=False)
    efforts = entry[0]
    base = SNAPSHOT_SUFFIX.sub("", model)
    return ReasoningSupport(
        values=efforts,
        known=True,
        default="none" if "none" in efforts else None,
        model_default=MODEL_DEFAULT_EFFORTS.get(model) or MODEL_DEFAULT_EFFORTS.get(base),
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

    return GenerationResult(
        text=response.output_text,
        usage=extract_usage(response.usage),
        service_tier=service_tier_of(response),
    )


def stream_reply(
    messages: Sequence[Message],
    model: str = DEFAULT_MODEL,
    reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
    max_output_tokens: int | None = None,
    client: OpenAI | None = None,
    instructions: str | None = None,
) -> Iterator[StreamEvent]:
    """Stream a reply with the Responses API (`stream=True`, `store=False`).

    Yields TextDelta for `response.output_text.delta` and one terminal event
    for `response.completed`, `response.incomplete`, `response.failed` or
    `error`. The SDK stream is closed when the generator finishes or is closed.
    """
    request = build_request(
        to_input_items(messages), model, reasoning_effort, max_output_tokens, instructions
    )
    request["stream"] = True
    try:
        stream = (client or create_client()).responses.create(**request)
    except OpenAIProviderError as error:  # missing API key
        yield StreamFailed(error)
        return
    except openai.OpenAIError as error:
        yield StreamFailed(translated(error))
        return

    parts: list[str] = []
    try:
        for event in stream:
            terminal = handle_stream_event(event, parts)
            if isinstance(terminal, TextDelta):
                yield terminal
            elif terminal is not None:
                yield terminal
                return
        yield StreamFailed(
            OpenAIProviderError("OpenAI stream ended before the response was complete."),
            "".join(parts),
        )
    except openai.OpenAIError as error:
        yield StreamFailed(translated(error), "".join(parts))
    finally:
        stream.close()


def handle_stream_event(
    event: Any, parts: list[str]
) -> TextDelta | StreamCompleted | StreamIncomplete | StreamFailed | None:
    """Map one SDK event. Returns a delta, a terminal event, or None to skip it."""
    kind = getattr(event, "type", None)
    if kind == "response.output_text.delta":
        delta = getattr(event, "delta", None)
        if not isinstance(delta, str):
            return StreamFailed(
                OpenAIProviderError("Invalid stream event from OpenAI: text delta is not a string."),
                "".join(parts),
            )
        if not delta:
            return None
        parts.append(delta)
        return TextDelta(delta)
    if kind == "response.completed":
        response = event.response
        text = "".join(parts) or (getattr(response, "output_text", "") or "")
        metadata = {"response_id": getattr(response, "id", None), "model": getattr(response, "model", None)}
        return StreamCompleted(
            GenerationResult(
                text=text,
                usage=extract_usage(getattr(response, "usage", None)),
                service_tier=service_tier_of(response),
            ),
            {key: value for key, value in metadata.items() if value is not None},
        )
    if kind == "response.incomplete":
        response = event.response
        details = getattr(response, "incomplete_details", None)
        reason = getattr(details, "reason", None) or "unknown reason"
        readable = {"max_output_tokens": "the max output token limit was reached",
                    "content_filter": "the content filter stopped the response"}
        return StreamIncomplete(
            readable.get(reason, reason), "".join(parts), extract_usage(getattr(response, "usage", None))
        )
    if kind == "response.failed":
        error = getattr(event.response, "error", None)
        message = getattr(error, "message", None) or "no details"
        return StreamFailed(OpenAIProviderError(f"OpenAI response failed: {message}"), "".join(parts))
    if kind == "error":
        message = getattr(event, "message", None) or "no details"
        return StreamFailed(OpenAIProviderError(f"OpenAI stream error: {message}"), "".join(parts))
    return None  # other event types (reasoning, output items, ...) are not needed


def translated(error: openai.OpenAIError) -> OpenAIProviderError:
    """translate_error, keeping the SDK error as the cause (as `raise ... from` would)."""
    result = translate_error(error)
    result.__cause__ = error
    return result


def count(value: Any) -> int | None:
    """A reported token count, or None if missing or not a non-negative integer."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def extract_usage(usage: Any) -> TokenUsage | None:
    """Convert the SDK usage object into TokenUsage, if usage was reported.

    Includes the cache and reasoning breakdowns from `input_tokens_details`
    and `output_tokens_details` when the API reports them.
    """
    if usage is None:
        return None
    inputs = getattr(usage, "input_tokens_details", None)
    outputs = getattr(usage, "output_tokens_details", None)
    return TokenUsage(
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        total_tokens=getattr(usage, "total_tokens", None),
        cached_input_tokens=count(getattr(inputs, "cached_tokens", None)),
        cache_write_tokens=count(getattr(inputs, "cache_write_tokens", None)),
        reasoning_tokens=count(getattr(outputs, "reasoning_tokens", None)),
    )


def service_tier_of(response: Any) -> str | None:
    tier = getattr(response, "service_tier", None)
    return tier if isinstance(tier, str) else None


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

    def stream(self, messages: Sequence[Message], options: GenerationOptions) -> Iterator[StreamEvent]:
        """Stream a reply. Rejected reasoning values follow the same rules as `generate`."""
        effort = options.reasoning_effort
        if effort is not None and not options.reasoning_explicit and options.model in self._no_reasoning:
            effort = None
        try:
            client = self.client()
        except OpenAIProviderError as error:
            yield StreamFailed(error)
            return
        with closing(self._stream(messages, options, effort, client)) as events:
            yield from self._with_reasoning_fallback(events, messages, options, effort, client)

    def _with_reasoning_fallback(
        self,
        events: Iterator[StreamEvent],
        messages: Sequence[Message],
        options: GenerationOptions,
        effort: str | None,
        client: OpenAI,
    ) -> Iterator[StreamEvent]:
        for event in events:
            rejected = (
                isinstance(event, StreamFailed)
                and effort is not None
                and not event.partial_text
                and rejects_reasoning(event.error)
            )
            if not rejected:
                yield event
                continue
            assert isinstance(event, StreamFailed)
            if options.reasoning_explicit:
                yield StreamFailed(OpenAIProviderError(
                    f"{options.model} does not accept reasoning effort '{effort}' "
                    f"({event.error}). Choose another value with F4 or --reasoning-effort."
                ))
                return
            self._no_reasoning.add(options.model)
            yield from self._stream(messages, options, None, client)
            return

    def _stream(
        self, messages: Sequence[Message], options: GenerationOptions, effort: str | None, client: OpenAI
    ) -> Iterator[StreamEvent]:
        return stream_reply(
            list(messages),
            model=options.model,
            reasoning_effort=effort,
            max_output_tokens=options.max_output_tokens,
            client=client,
            instructions=options.instructions,
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
