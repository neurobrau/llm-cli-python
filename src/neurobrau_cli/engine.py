"""The active provider, model and generation options for a chat."""

from collections.abc import Mapping, Sequence

from neurobrau_cli.conversation import Message
from neurobrau_cli.providers.base import GenerationOptions, GenerationResult, Provider


class ChatEngine:
    """Routes replies to the selected provider. UIs switch providers through `select`."""

    def __init__(
        self,
        providers: Mapping[str, Provider],
        provider: str,
        model: str,
        reasoning_effort: str | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        self.providers = dict(providers)
        self.provider = provider
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens

    @property
    def current(self) -> Provider:
        return self.providers[self.provider]

    def options(self) -> GenerationOptions:
        return GenerationOptions(self.model, self.reasoning_effort, self.max_output_tokens)

    def reply(self, messages: Sequence[Message]) -> GenerationResult:
        return self.current.generate(messages, self.options())

    def select(self, provider: str, model: str) -> None:
        if provider not in self.providers:
            raise KeyError(provider)
        self.provider, self.model = provider, model
