"""LLM provider integrations."""

PROVIDER_LABELS = {"openai": "OpenAI", "ollama": "Ollama"}


def provider_label(name: str | None) -> str:
    return PROVIDER_LABELS.get(name or "", name or "-")
