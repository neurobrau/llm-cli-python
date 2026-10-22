"""Provider-independent types shared by all LLM providers.

A provider sends text messages and returns text, reports token usage,
checks whether it can be used, lists its models, and raises `ProviderError`
with a normalized `ErrorKind` for every failure.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from neurobrau_cli.conversation import Message


class ErrorKind(Enum):
    """Categories of provider failures, so callers can react to each one."""

    MISSING_API_KEY = "missing_api_key"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    API = "api"


class ProviderError(Exception):
    """A user-facing, credential-free error raised by any provider."""

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
    """Final answer text, kept separate from any reasoning the model returned."""

    text: str
    usage: TokenUsage | None = None
    reasoning: str | None = None


@dataclass(frozen=True)
class GenerationOptions:
    """Per-request settings.

    `reasoning_effort` uses the provider's vocabulary (see ReasoningSupport);
    None means the provider's default policy. When `reasoning_explicit` is
    set, the user chose the value: providers must report it as unsupported
    instead of silently dropping it. `instructions` is a system/developer
    instruction for this request only; it is not part of the conversation.
    """

    model: str
    reasoning_effort: str | None = None
    max_output_tokens: int | None = None
    instructions: str | None = None
    reasoning_explicit: bool = False


@dataclass(frozen=True)
class ModelInfo:
    """A model reported by a provider's discovery API."""

    id: str
    remote: bool = False  # runs on a remote service although listed locally
    capabilities: tuple[str, ...] | None = None  # None: not reported
    details: str = ""
    context_length: int | None = None


@dataclass(frozen=True)
class ReasoningSupport:
    """Which reasoning values a model accepts, as far as the provider can confirm.

    `values` are selectable values in the provider's vocabulary (OpenAI:
    efforts such as "low"; Ollama: "off"/"on" or named levels). `known=False`
    means support could not be confirmed. `default` is what is used when the
    user chose nothing; None sends no reasoning parameter. `allows_default`
    offers "Default" (omit the parameter) as a choice.
    """

    values: tuple[str, ...] = ()
    known: bool = False
    default: str | None = None
    allows_default: bool = True

    @property
    def supported(self) -> bool:
        return bool(self.values)


@dataclass(frozen=True)
class Availability:
    available: bool
    message: str = ""


class Provider(Protocol):
    """What the chat needs from an LLM provider."""

    name: str  # CLI value, e.g. "ollama"
    label: str  # display name, e.g. "Ollama"

    def generate(self, messages: Sequence[Message], options: GenerationOptions) -> GenerationResult:
        """Send the conversation, oldest message first. Raises ProviderError."""
        ...

    def list_models(self, refresh: bool = False) -> list[ModelInfo]:
        """Models that can be selected for chat. Cached unless `refresh`. Raises ProviderError."""
        ...

    def check_availability(self) -> Availability:
        """Whether the provider can be used right now (credentials, service reachable)."""
        ...

    def default_model(self) -> str:
        """Model used when none is chosen. Raises ProviderError if none can be determined."""
        ...

    def reasoning_support(self, model: str) -> ReasoningSupport:
        """Confirmed reasoning values for a model. May make one cached request."""
        ...

    def max_output_limit(self, model: str) -> int | None:
        """Largest allowed max-output-tokens value, if known. May use cached discovery."""
        ...
