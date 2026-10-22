"""Runtime status bar: dynamic state only (provider, model and tokens live in the settings panel)."""

from collections.abc import Mapping
from dataclasses import dataclass

from neurobrau_cli.providers.base import TokenUsage

READY = "READY"
CONNECTING = "CONNECTING"
GENERATING = "GENERATING"
DONE = "DONE"
CANCELLED = "CANCELLED"
ERROR = "ERROR"
WARNING = "WARNING"

# States shown until the next user action, then back to READY.
TRANSIENT = (DONE, CANCELLED, ERROR, WARNING)

# Style name (see tui.Styles) for each state word.
STATE_STYLES = {
    READY: "text",
    CONNECTING: "waiting",
    GENERATING: "waiting",
    DONE: "ai",
    CANCELLED: "waiting",
    ERROR: "error",
    WARNING: "waiting",
}

SEPARATOR = " | "
CANCEL_HINT = "Ctrl+C Cancel"


@dataclass(frozen=True)
class RuntimeStatus:
    state: str = READY
    parts: tuple[str, ...] = ()

    def text(self) -> str:
        return SEPARATOR.join((self.state, *self.parts))


def ready(*parts: str) -> RuntimeStatus:
    return RuntimeStatus(READY, parts)


def seconds(value: float) -> str:
    return f"{value:.2f}s"


def active_status(state: str, elapsed: float, detail: str | None = None) -> RuntimeStatus:
    """CONNECTING / GENERATING while a request runs (elapsed time is client-observed)."""
    parts = [detail] if detail else []
    return RuntimeStatus(state, (*parts, f"{elapsed:.1f}s elapsed", CANCEL_HINT))


def done_status(
    metadata: Mapping[str, object],
    usage: TokenUsage | None,
    observed: float,
    first_text: float | None,
) -> RuntimeStatus:
    """DONE with server metrics when the provider reports them, else client-observed timing.

    Ollama reports durations: total, model load and generation time, and
    generation speed = output tokens / generation time. OpenAI reports none,
    so only the latency measured by this client is shown, labelled as such.
    Missing values are left out, never estimated.
    """
    total = metadata.get("total_seconds")
    if isinstance(total, float):
        parts = [f"{seconds(total)} total"]
        load = metadata.get("load_seconds")
        generation = metadata.get("eval_seconds")
        if isinstance(load, float):
            parts.append(f"load {seconds(load)}")
        if isinstance(generation, float):
            parts.append(f"generation {seconds(generation)}")
            output = usage.output_tokens if usage else None
            if output is not None and generation > 0:
                parts.append(f"{output / generation:.2f} tok/s")
        return RuntimeStatus(DONE, tuple(parts))
    parts = [f"{seconds(observed)} observed"]
    if first_text is not None:
        parts.append(f"first text after {seconds(first_text)}")
    return RuntimeStatus(DONE, tuple(parts))
