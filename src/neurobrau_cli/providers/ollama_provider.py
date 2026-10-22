"""Ollama provider using the native HTTP API (`/api/chat`, `/api/tags`, `/api/show`).

Only models that run locally can be selected. Ollama also lists cloud models
(e.g. `glm-4.7:cloud`) that forward requests to ollama.com; those are
excluded. Models are never downloaded automatically.
"""

import codecs
import ipaddress
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from typing import Any
from urllib.parse import urlsplit

from neurobrau_cli.conversation import Message
from neurobrau_cli.providers.base import (
    Availability,
    ErrorKind,
    GenerationOptions,
    GenerationResult,
    ModelInfo,
    ProviderError,
    ReasoningDelta,
    ReasoningSupport,
    StreamCompleted,
    StreamEvent,
    StreamFailed,
    StreamIncomplete,
    TextDelta,
    TokenUsage,
)

HOST_ENV_VAR = "OLLAMA_HOST"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_PORT = 11434
CHAT_TIMEOUT = 300.0  # seconds; the first request may have to load the model
DISCOVERY_TIMEOUT = 3.0  # seconds for /api/tags and /api/show

THINK_TAGS = re.compile(r"^\s*<think>(.*?)</think>\s*", re.DOTALL)

# transport(method, url, payload, timeout) -> parsed JSON; raises OllamaProviderError
Transport = Callable[[str, str, dict[str, Any] | None, float], Any]
# stream_transport(method, url, payload, timeout) -> raw byte chunks as they arrive.
# Closing the iterator closes the connection. Raises OllamaProviderError.
StreamTransport = Callable[[str, str, dict[str, Any] | None, float], Iterator[bytes]]
STREAM_READ_SIZE = 8192


class OllamaProviderError(ProviderError):
    """A user-facing error raised by the Ollama provider."""


class OllamaHTTPError(OllamaProviderError):
    """Ollama answered with an HTTP error status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Ollama API error (HTTP {status}): {message}")
        self.status = status
        self.detail = message


def resolve_base_url(env: Mapping[str, str] | None = None) -> str:
    """Base URL from OLLAMA_HOST (`host`, `host:port` or a URL), else the local default."""
    env = os.environ if env is None else env
    value = env.get(HOST_ENV_VAR, "").strip()
    if not value:
        return DEFAULT_BASE_URL
    if "://" not in value:
        value = f"http://{value}"
    parts = urlsplit(value)
    host = parts.hostname or "127.0.0.1"
    if host in ("0.0.0.0", "::"):  # a bind-all server address; connect locally
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}:{parts.port or DEFAULT_PORT}{parts.path.rstrip('/')}"


def is_loopback(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def open_request(method: str, url: str, payload: dict[str, Any] | None, timeout: float) -> Any:
    """Open a JSON request with the standard library; returns the HTTP response."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    # Never route requests to a local server through an HTTP proxy.
    handlers = [urllib.request.ProxyHandler({})] if is_loopback(url) else []
    return urllib.request.build_opener(*handlers).open(request, timeout=timeout)


def urllib_transport(method: str, url: str, payload: dict[str, Any] | None, timeout: float) -> Any:
    """Send one JSON request with the standard library and parse the JSON reply."""
    with transport_errors(url):
        with open_request(method, url, payload, timeout) as response:
            body = response.read()
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError as error:
        raise invalid_response("reply is not valid JSON") from error


def urllib_stream_transport(
    method: str, url: str, payload: dict[str, Any] | None, timeout: float
) -> Iterator[bytes]:
    """Yield the response body in chunks as they arrive; the connection closes with the generator.

    `timeout` applies to connecting and to each read, so a stalled stream fails
    instead of hanging.
    """
    with transport_errors(url):
        response = open_request(method, url, payload, timeout)
    try:
        while True:
            with transport_errors(url):
                chunk = response.read1(STREAM_READ_SIZE)
            if not chunk:
                return
            yield chunk
    finally:
        response.close()


@contextmanager
def transport_errors(url: str) -> Iterator[None]:
    """Turn urllib/socket exceptions into categorized OllamaProviderErrors."""
    try:
        yield
    except urllib.error.HTTPError as error:
        raise OllamaHTTPError(error.code, error_detail(error.read())) from error
    except TimeoutError as error:
        raise OllamaProviderError("Request to Ollama timed out.", ErrorKind.TIMEOUT) from error
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise OllamaProviderError("Request to Ollama timed out.", ErrorKind.TIMEOUT) from error
        raise OllamaProviderError(
            f"Could not connect to Ollama at {base_of(url)}. Is the Ollama service running?",
            ErrorKind.CONNECTION,
        ) from error
    except OSError as error:  # e.g. connection reset while reading
        raise OllamaProviderError(
            f"Connection to Ollama at {base_of(url)} failed: {error.strerror or error}",
            ErrorKind.CONNECTION,
        ) from error


def iter_ndjson(chunks: Iterable[bytes]) -> Iterator[object]:
    """Parse newline-delimited JSON from arbitrary byte chunks.

    Bytes are buffered until a full line arrives, so a JSON object or a
    multi-byte UTF-8 character split across chunks is decoded intact. Blank
    lines are skipped. Raises OllamaProviderError for a line that is not JSON.
    """
    buffer = b""
    for chunk in chunks:
        buffer += chunk
        *lines, buffer = buffer.split(b"\n")
        for line in lines:
            if line.strip():
                yield parse_ndjson_line(line)
    if buffer.strip():
        yield parse_ndjson_line(buffer)


def parse_ndjson_line(line: bytes) -> object:
    try:
        return json.loads(codecs.decode(line, "utf-8"))
    except ValueError as error:
        raise invalid_response(f"stream line is not valid JSON: {line[:80]!r}") from error


def base_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def error_detail(body: bytes) -> str:
    """Ollama returns errors as {"error": "..."}; fall back to the raw text."""
    text = body.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        return text[:200] or "no details"
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        return data["error"]
    return text[:200]


def invalid_response(reason: str) -> OllamaProviderError:
    return OllamaProviderError(f"Invalid response from Ollama: {reason}.")


# ----- response parsing -----


def is_cloud_name(model_id: str) -> bool:
    """Ollama cloud models use a `cloud` tag: `glm-4.7:cloud`, `gemma4:31b-cloud`."""
    tag = model_id.rpartition(":")[2]
    return tag == "cloud" or tag.endswith("-cloud")


def parse_tag_entry(entry: object) -> ModelInfo:
    if not isinstance(entry, dict):
        raise invalid_response("model entry is not an object")
    model_id = entry.get("model") or entry.get("name")
    if not isinstance(model_id, str) or not model_id:
        raise invalid_response("model entry has no name")
    remote = bool(entry.get("remote_host") or entry.get("remote_model")) or is_cloud_name(model_id)
    capabilities = entry.get("capabilities")
    details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
    summary = " ".join(
        str(details[key]) for key in ("parameter_size", "quantization_level") if details.get(key)
    )
    context = details.get("context_length")
    return ModelInfo(
        id=model_id,
        remote=remote,
        capabilities=tuple(capabilities) if isinstance(capabilities, list) else None,
        details=summary,
        context_length=context if isinstance(context, int) and context > 0 else None,
    )


def parse_tags(data: object) -> list[ModelInfo]:
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise invalid_response("expected an object with a 'models' list")
    return [parse_tag_entry(entry) for entry in data["models"]]


def is_selectable(model: ModelInfo) -> bool:
    """Local models that can chat. Unknown capabilities are given the benefit of the doubt."""
    return not model.remote and (model.capabilities is None or "completion" in model.capabilities)


def split_reasoning(content: str, thinking: object) -> tuple[str, str | None]:
    """Separate reasoning from the answer: the `thinking` field, or a leading <think> block."""
    reasoning = thinking if isinstance(thinking, str) and thinking.strip() else None
    match = THINK_TAGS.match(content)
    if match:
        inline = match.group(1).strip()
        content = content[match.end():]
        reasoning = reasoning or inline or None
    return content, reasoning


def parse_chat_response(data: object) -> GenerationResult:
    if not isinstance(data, dict):
        raise invalid_response("expected a JSON object")
    message = data.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise invalid_response("missing 'message.content'")
    text, reasoning = split_reasoning(message["content"], message.get("thinking"))

    prompt_tokens = data.get("prompt_eval_count")
    output_tokens = data.get("eval_count")
    prompt_tokens = prompt_tokens if isinstance(prompt_tokens, int) else None
    output_tokens = output_tokens if isinstance(output_tokens, int) else None
    usage = None
    if prompt_tokens is not None or output_tokens is not None:
        total = (
            prompt_tokens + output_tokens
            if prompt_tokens is not None and output_tokens is not None
            else None
        )
        usage = TokenUsage(prompt_tokens, output_tokens, total)
    return GenerationResult(text=text, usage=usage, reasoning=reasoning)


def to_ollama_messages(
    messages: Sequence[Message], instructions: str | None = None
) -> list[dict[str, str]]:
    """Chat messages, with `instructions` as a leading system message for this request only."""
    converted = [{"role": message.role, "content": message.content} for message in messages]
    if instructions:
        converted.insert(0, {"role": "system", "content": instructions})
    return converted


def thinking_support(capabilities: tuple[str, ...], show: object) -> ReasoningSupport:
    """Reasoning choices from `/api/show`'s `thinking` metadata.

    `{"values": [false, true]}` gives Off/On; string values are named levels.
    A model with the `thinking` capability but no metadata gets Off/On, the
    generic `think` boolean. Models without the capability get no choices.
    """
    metadata = show.get("thinking") if isinstance(show, dict) else None
    reported = metadata.get("values") if isinstance(metadata, dict) else None
    values: list[str] = []
    if isinstance(reported, list) and reported:
        for value in reported:
            if value is False:
                values.append("off")
            elif value is True:
                values.append("on")
            elif isinstance(value, str) and value:
                values.append(value)
    elif "thinking" in capabilities:
        values = ["off", "on"]
    if not values:
        return ReasoningSupport(known=True)
    off = "off" in values
    # Without a choice, thinking is off where possible (the Task 006 default).
    return ReasoningSupport(tuple(values), known=True, default="off" if off else None,
                            allows_default=not off)


# ----- provider -----


class OllamaProvider:
    """`Provider` implementation for a local Ollama server."""

    name = "ollama"
    label = "Ollama"

    def __init__(
        self,
        base_url: str | None = None,
        transport: Transport | None = None,
        stream_transport: StreamTransport | None = None,
        chat_timeout: float = CHAT_TIMEOUT,
        discovery_timeout: float = DISCOVERY_TIMEOUT,
    ) -> None:
        self.base_url = base_url or resolve_base_url()
        self.transport = transport or urllib_transport
        self.stream_transport = stream_transport or urllib_stream_transport
        self.chat_timeout = chat_timeout
        self.discovery_timeout = discovery_timeout
        self._models: list[ModelInfo] | None = None  # everything /api/tags reported
        self._show: dict[str, Any] = {}  # /api/show replies, per model

    def request(self, method: str, path: str, payload: dict[str, Any] | None, timeout: float) -> Any:
        return self.transport(method, f"{self.base_url}{path}", payload, timeout)

    # ----- discovery -----

    def discover(self, refresh: bool = False) -> list[ModelInfo]:
        """All models from /api/tags, including remote ones. Cached unless `refresh`."""
        if self._models is None or refresh:
            self._models = parse_tags(self.request("GET", "/api/tags", None, self.discovery_timeout))
        return self._models

    def list_models(self, refresh: bool = False) -> list[ModelInfo]:
        """Installed local chat models, in Ollama's order (most recently modified first)."""
        return [model for model in self.discover(refresh) if is_selectable(model)]

    def check_availability(self) -> Availability:
        try:
            models = self.list_models(refresh=True)
        except ProviderError as error:
            return Availability(False, str(error))
        if not models:
            return Availability(False, no_local_models_message())
        return Availability(True)

    def default_model(self) -> str:
        models = self.list_models()
        if not models:
            raise OllamaProviderError(no_local_models_message())
        return models[0].id

    def local_model(self, model_id: str) -> ModelInfo:
        """Look up an installed local model. Refuses cloud and missing models."""
        model = self.find(self.discover(), model_id)
        if model is None:  # maybe installed since the list was cached
            model = self.find(self.discover(refresh=True), model_id)
        if model is None:
            raise OllamaProviderError(
                f"Ollama model '{model_id}' is not installed locally. "
                f"Install it yourself with: ollama pull {model_id}"
            )
        if model.remote:
            raise OllamaProviderError(
                f"'{model_id}' is an Ollama cloud model that runs on a remote service. "
                "Choose a local model."
            )
        return model

    @staticmethod
    def find(models: list[ModelInfo], model_id: str) -> ModelInfo | None:
        return next((model for model in models if model.id == model_id), None)

    def show(self, model: ModelInfo) -> Any:
        """One cached /api/show request. Only ever called for local models."""
        if model.id not in self._show:
            self._show[model.id] = self.request(
                "POST", "/api/show", {"model": model.id}, self.discovery_timeout
            )
        return self._show[model.id]

    def capabilities(self, model: ModelInfo) -> tuple[str, ...]:
        """Capabilities from /api/tags, or from /api/show if tags omit them."""
        if model.capabilities is not None:
            return model.capabilities
        data = self.show(model)
        reported = data.get("capabilities") if isinstance(data, dict) else None
        return tuple(reported) if isinstance(reported, list) else ()

    def reasoning_support(self, model_id: str) -> ReasoningSupport:
        model = self.local_model(model_id)
        return thinking_support(self.capabilities(model), self.show(model))

    def max_output_limit(self, model_id: str) -> int | None:
        """The model's context length from /api/tags; num_predict cannot usefully exceed it."""
        return self.local_model(model_id).context_length

    def think_value(self, model: ModelInfo, options: GenerationOptions) -> bool | str:
        """Map a reasoning value to Ollama's `think` parameter (model supports thinking)."""
        value = options.reasoning_effort
        if value in (None, "none", "off"):
            return False
        if value == "on":
            return True
        support = self.reasoning_support(model.id)
        if value in support.values:
            return value  # a named level such as "high"
        if "on" in support.values:
            return True  # an effort name (e.g. --reasoning-effort low) on an On/Off model
        if options.reasoning_explicit:
            raise OllamaProviderError(
                f"{model.id} does not support thinking level '{value}'. "
                f"Supported: {', '.join(support.values)}."
            )
        return True

    # ----- generation -----

    def chat_payload(
        self, messages: Sequence[Message], options: GenerationOptions, stream: bool
    ) -> tuple[ModelInfo, dict[str, Any]]:
        """Validate the model and options; build the /api/chat body. Raises OllamaProviderError."""
        model = self.local_model(options.model)
        payload: dict[str, Any] = {
            "model": model.id,
            "messages": to_ollama_messages(messages, options.instructions),
            "stream": stream,
        }
        if "thinking" in self.capabilities(model):
            # Thinking is off unless another value is requested.
            payload["think"] = self.think_value(model, options)
        elif options.reasoning_explicit and options.reasoning_effort not in (None, "none", "off"):
            raise OllamaProviderError(
                f"{model.id} does not support thinking; turn reasoning off (F4) "
                "or choose another model."
            )
        if options.max_output_tokens is not None:
            payload["options"] = {"num_predict": options.max_output_tokens}
        return model, payload

    def generate(self, messages: Sequence[Message], options: GenerationOptions) -> GenerationResult:
        model, payload = self.chat_payload(messages, options, stream=False)
        try:
            data = self.request("POST", "/api/chat", payload, self.chat_timeout)
        except OllamaHTTPError as error:
            raise explain_http_error(error, model.id) from error
        return parse_chat_response(data)

    def stream(self, messages: Sequence[Message], options: GenerationOptions) -> Iterator[StreamEvent]:
        """Stream /api/chat (`stream: true`) as events. The connection closes with the generator."""
        try:
            model, payload = self.chat_payload(messages, options, stream=True)
        except ProviderError as error:
            yield StreamFailed(error)
            return
        try:
            chunks = self.stream_transport(
                "POST", f"{self.base_url}/api/chat", payload, self.chat_timeout
            )
        except OllamaHTTPError as error:
            yield StreamFailed(explain_http_error(error, model.id))
            return
        except ProviderError as error:
            yield StreamFailed(error)
            return
        with closing(chunks):
            yield from stream_chat_events(chunks, model.id)


def explain_http_error(error: OllamaHTTPError, model_id: str) -> OllamaProviderError:
    if error.status == 404:
        return OllamaProviderError(
            f"Ollama model '{model_id}' was not found: {error.detail}. "
            f"Install it yourself with: ollama pull {model_id}"
        )
    return error


class ThinkTagFilter:
    """Hold back a leading `<think>…</think>` block that some models put in `content`."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.pending = ""
        self.done = False  # past any leading think block

    def feed(self, text: str) -> tuple[str, str]:
        """Return (answer text, reasoning text) that can be emitted now."""
        if self.done:
            return text, ""
        self.pending += text
        stripped = self.pending.lstrip()
        if self.OPEN.startswith(stripped):
            return "", ""  # could still become "<think>"; wait for more
        if not stripped.startswith(self.OPEN):
            self.done = True
            answer, self.pending = self.pending, ""
            return answer, ""
        end = stripped.find(self.CLOSE)
        if end < 0:
            return "", ""  # inside the think block
        self.done = True
        reasoning = stripped[len(self.OPEN):end].strip()
        answer = stripped[end + len(self.CLOSE):].lstrip()
        self.pending = ""
        return answer, reasoning

    def flush(self) -> str:
        """Answer text still held back when the stream ends.

        A partial tag such as "<th" was real text; an unclosed think block is
        thinking and is never returned as the answer.
        """
        text, self.pending = self.pending, ""
        return "" if text.lstrip().startswith(self.OPEN) else text


def nanoseconds(value: object) -> float | None:
    return value / 1e9 if isinstance(value, int) and value >= 0 else None


def chunk_usage(data: dict[str, Any]) -> TokenUsage | None:
    return parse_chat_response({"message": {"content": ""}, **data}).usage


def stream_chat_events(chunks: Iterable[bytes], model_id: str) -> Iterator[StreamEvent]:
    """Turn /api/chat NDJSON chunks into stream events."""
    answer: list[str] = []
    reasoning: list[str] = []
    think_filter = ThinkTagFilter()
    try:
        for data in iter_ndjson(chunks):
            if not isinstance(data, dict):
                raise invalid_response("stream line is not a JSON object")
            if isinstance(data.get("error"), str):
                yield StreamFailed(OllamaProviderError(f"Ollama error: {data['error']}"), "".join(answer))
                return
            message = data.get("message", {})
            if not isinstance(message, dict) or not isinstance(message.get("content", ""), str):
                raise invalid_response("stream line has no valid 'message.content'")
            thinking = message.get("thinking")
            if isinstance(thinking, str) and thinking:
                reasoning.append(thinking)
                yield ReasoningDelta(thinking)
            text, inline_reasoning = think_filter.feed(message.get("content", ""))
            if inline_reasoning:
                reasoning.append(inline_reasoning)
                yield ReasoningDelta(inline_reasoning)
            if text:
                answer.append(text)
                yield TextDelta(text)
            if data.get("done") is True:
                leftover = think_filter.flush()
                if leftover:
                    answer.append(leftover)
                    yield TextDelta(leftover)
                yield finish_event(data, "".join(answer), "".join(reasoning) or None, model_id)
                return
        yield StreamFailed(
            OllamaProviderError("Ollama stream ended before the response was complete.",
                                ErrorKind.CONNECTION),
            "".join(answer),
        )
    except OllamaHTTPError as error:
        yield StreamFailed(explain_http_error(error, model_id), "".join(answer))
    except ProviderError as error:
        yield StreamFailed(error, "".join(answer))


def finish_event(
    data: dict[str, Any], text: str, reasoning: str | None, model_id: str
) -> StreamCompleted | StreamIncomplete:
    usage = chunk_usage(data)
    if data.get("done_reason") == "length":
        return StreamIncomplete("the max output token limit was reached", text, usage)
    metadata: dict[str, object] = {"model": data.get("model", model_id)}
    for key in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration"):
        seconds = nanoseconds(data.get(key))
        if seconds is not None:
            metadata[key.replace("_duration", "_seconds")] = seconds
    if isinstance(data.get("done_reason"), str):
        metadata["done_reason"] = data["done_reason"]
    return StreamCompleted(GenerationResult(text, usage, reasoning), metadata)


def no_local_models_message() -> str:
    return (
        "No local Ollama models are installed (cloud models are not used). "
        "Install one with: ollama pull <model>"
    )
