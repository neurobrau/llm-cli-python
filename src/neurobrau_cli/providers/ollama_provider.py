"""Ollama provider using the native HTTP API (`/api/chat`, `/api/tags`, `/api/show`).

Only models that run locally can be selected. Ollama also lists cloud models
(e.g. `glm-4.7:cloud`) that forward requests to ollama.com; those are
excluded. Models are never downloaded automatically.
"""

import ipaddress
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
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


def urllib_transport(method: str, url: str, payload: dict[str, Any] | None, timeout: float) -> Any:
    """Send one JSON request with the standard library and parse the JSON reply."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    # Never route requests to a local server through an HTTP proxy.
    handlers = [urllib.request.ProxyHandler({})] if is_loopback(url) else []
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read()
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
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError as error:
        raise invalid_response("reply is not valid JSON") from error


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
    return ModelInfo(
        id=model_id,
        remote=remote,
        capabilities=tuple(capabilities) if isinstance(capabilities, list) else None,
        details=summary,
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


def to_ollama_messages(messages: Sequence[Message]) -> list[dict[str, str]]:
    return [{"role": message.role, "content": message.content} for message in messages]


# ----- provider -----


class OllamaProvider:
    """`Provider` implementation for a local Ollama server."""

    name = "ollama"
    label = "Ollama"

    def __init__(
        self,
        base_url: str | None = None,
        transport: Transport | None = None,
        chat_timeout: float = CHAT_TIMEOUT,
        discovery_timeout: float = DISCOVERY_TIMEOUT,
    ) -> None:
        self.base_url = base_url or resolve_base_url()
        self.transport = transport or urllib_transport
        self.chat_timeout = chat_timeout
        self.discovery_timeout = discovery_timeout
        self._models: list[ModelInfo] | None = None  # everything /api/tags reported
        self._capabilities: dict[str, tuple[str, ...]] = {}  # from /api/show

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

    def capabilities(self, model: ModelInfo) -> tuple[str, ...]:
        """Capabilities from /api/tags, or one /api/show request if tags omit them."""
        if model.capabilities is not None:
            return model.capabilities
        if model.id not in self._capabilities:
            data = self.request("POST", "/api/show", {"model": model.id}, self.discovery_timeout)
            reported = data.get("capabilities") if isinstance(data, dict) else None
            self._capabilities[model.id] = tuple(reported) if isinstance(reported, list) else ()
        return self._capabilities[model.id]

    # ----- generation -----

    def generate(self, messages: Sequence[Message], options: GenerationOptions) -> GenerationResult:
        model = self.local_model(options.model)
        payload: dict[str, Any] = {
            "model": model.id,
            "messages": to_ollama_messages(messages),
            "stream": False,
        }
        if "thinking" in self.capabilities(model):
            # Thinking is off unless a reasoning effort other than "none" is requested.
            payload["think"] = options.reasoning_effort not in (None, "none")
        if options.max_output_tokens is not None:
            payload["options"] = {"num_predict": options.max_output_tokens}
        try:
            data = self.request("POST", "/api/chat", payload, self.chat_timeout)
        except OllamaHTTPError as error:
            if error.status == 404:
                raise OllamaProviderError(
                    f"Ollama model '{model.id}' was not found: {error.detail}. "
                    f"Install it yourself with: ollama pull {model.id}"
                ) from error
            raise
        return parse_chat_response(data)


def no_local_models_message() -> str:
    return (
        "No local Ollama models are installed (cloud models are not used). "
        "Install one with: ollama pull <model>"
    )
