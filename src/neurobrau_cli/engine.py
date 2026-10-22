"""The active provider, model and generation options for a chat."""

from collections.abc import Mapping, Sequence

from neurobrau_cli.conversation import Message
from neurobrau_cli.providers.base import GenerationOptions, GenerationResult, Provider
from neurobrau_cli.settings import DEFAULT_ANSWER_STYLE, STYLE_INSTRUCTIONS


class ChatEngine:
    """Routes replies to the selected provider. UIs switch providers through `select`."""

    def __init__(
        self,
        providers: Mapping[str, Provider],
        provider: str,
        model: str,
        reasoning_effort: str | None = None,
        max_output_tokens: int | None = None,
        answer_style: str = DEFAULT_ANSWER_STYLE,
        reasoning_explicit: bool = False,
    ) -> None:
        self.providers = dict(providers)
        self.provider = provider
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.reasoning_explicit = reasoning_explicit
        self.max_output_tokens = max_output_tokens
        self.answer_style = answer_style

    @property
    def current(self) -> Provider:
        return self.providers[self.provider]

    def options(self) -> GenerationOptions:
        """Settings for the next request; changing them never touches the conversation."""
        return GenerationOptions(
            self.model,
            self.reasoning_effort,
            self.max_output_tokens,
            instructions=STYLE_INSTRUCTIONS[self.answer_style],
            reasoning_explicit=self.reasoning_explicit,
        )

    def reply(self, messages: Sequence[Message]) -> GenerationResult:
        return self.current.generate(messages, self.options())

    def select(self, provider: str, model: str) -> None:
        """Switch model. Reasoning values are model-specific, so they reset here;
        `Preferences.apply` then sets the new model's values."""
        if provider not in self.providers:
            raise KeyError(provider)
        if (provider, model) != (self.provider, self.model):
            self.reasoning_effort, self.reasoning_explicit = None, False
        self.provider, self.model = provider, model
