# NeuroBräu CLI — Python

Educational multi-provider LLM command-line client. Supports OpenAI (Responses
API) and local models through [Ollama](https://ollama.com)'s native HTTP API.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
export OPENAI_API_KEY="..."   # read from the environment only; not needed for Ollama
```

For Ollama, run the Ollama service locally (default `http://127.0.0.1:11434`,
override with `OLLAMA_HOST`) and install a model yourself, e.g.
`ollama pull qwen3.5:4b`. NeuroBräu never downloads models.

## Usage

One-shot prompt:

```bash
neurobrau "What is a neural network?"
neurobrau --model MODEL_ID --reasoning-effort low --max-output-tokens 200 --usage "Explain backprop"
neurobrau --provider ollama --model qwen3.5:4b "Hello"
neurobrau --provider ollama "Hello"     # first local model listed by Ollama
neurobrau --answer-style brief "Explain recursion"
neurobrau --provider ollama --stream "Explain neural networks."   # print text as it arrives
```

`--stream` prints the answer while it is generated, in one-shot mode, the
`--plain` chat and the fullscreen UI. With `--usage`, token counts
(and, for Ollama, durations) go to stderr after a complete answer. A
streamed answer that stops early, e.g. at the token limit, is reported on
stderr and exits with code 8. In the plain chat, only completed answers are
added to the conversation; a cut-off, failed or Ctrl+C-cancelled answer is
shown but not saved or sent as context. Ollama thinking is never printed.

Interactive chat (no prompt argument; model options still apply):

```bash
neurobrau                  # fullscreen DOS-style UI in a terminal
neurobrau --plain          # line-oriented chat
neurobrau --model MODEL_ID --usage
neurobrau --provider ollama
```

Without `--provider`/`--model`, an interactive chat continues with the
provider and model that were active last time, and their conversation. In
the fullscreen UI, the **LLM SETTINGS** panel shows the current selection;
**F2** picks a model and **F3** a provider (OpenAI or Ollama) from DOS-style
menus. Every provider/model combination keeps its own conversation:
switching brings back that combination's most recent conversation (or starts
an empty one), and messages are never sent to a different model. Switching
back returns to where you left off, also after a restart.

Generation settings change from the next message on and keep the conversation:

| Key | Setting | Choices |
|---|---|---|
| F4 | Reasoning | only values confirmed for the model: OpenAI efforts from a built-in table (unknown models: Default only); Ollama Off/On or named levels from `/api/show` |
| F5 | Max output tokens | Default, 256, 512, 1024, 2048, 4096, Custom (validated against the model's limit when known) |
| F6 | Answer style | Brief, Normal, Detailed (sent as a system/developer instruction, not stored in the conversation) |

Ollama thinking output is kept out of the answer.

## Token usage and estimated cost

Each API request's usage is stored with its conversation exactly as the
provider reports it: input, cached input, cache writes, output, reasoning
and total tokens (nothing is estimated from text). The LLM SETTINGS panel
shows the conversation totals, the input size of the latest request
(`context`), and an **ESTIMATED** cost; `/usage` prints the full breakdown.
Totals survive restarts. Because every request re-sends the whole history,
billed input grows faster than the conversation itself.

Costs are estimates, never billed amounts, calculated with `Decimal` from a
small versioned catalog of official OpenAI prices (`src/neurobrau_cli/pricing.py`,
version 2026-10-22; currently gpt-6-luna and gpt-5.1). Cached input, cache
writes, long prompts (> 272K input tokens) and processing tiers (Flex,
Batch, Fast) are priced when the API reports them. When the model, tier or
required counts are unknown, the estimate is shown as qualified (`~`), a
lower bound (`>=`) or `unavailable`, never invented. Local Ollama models
cost `$0`; Ollama cloud models are never shown as free. Details:
[docs/tui-spec.md](docs/tui-spec.md#15-token-accounting-and-estimated-cost).

## Settings

F4–F6 choices are saved in `$XDG_CONFIG_HOME/neurobrau/settings.json`
(default `~/.config/neurobrau/settings.json`): reasoning and max tokens per
provider/model, answer style globally. The file never contains API keys and
is separate from conversation history. CLI flags (`--reasoning-effort`,
`--max-output-tokens`, `--answer-style`) override saved values for one run
and are not saved. A saved value the model does not support is ignored with
a visible note. Format: [docs/tui-spec.md](docs/tui-spec.md#13-settings-file).

The bottom bar of the fullscreen UI shows runtime state only: `READY`,
`CONNECTING`, `GENERATING` (with elapsed time and `Ctrl+C Cancel`), `DONE`
(Ollama: total, load and generation time and tokens/s from the API; OpenAI:
client-observed latency), `CANCELLED`, `ERROR` and `WARNING`. Provider,
model, settings and session token totals are in the LLM SETTINGS panel.
With `--stream`, Ctrl+C cancels the answer being generated (it is not
saved) and the chat continues.

Ollama models are discovered via `/api/tags`. Cloud models that Ollama lists
(e.g. `glm-4.7:cloud`) run on a remote service and are excluded from
selection and never contacted; only local models are used.

The fullscreen UI starts only when stdin and stdout are terminals; otherwise
the line-oriented chat is used. Layout, colors and keys are specified in
[docs/tui-spec.md](docs/tui-spec.md).

Chat commands (backslash aliases such as `\help` also work):

| Command | Effect |
|---|---|
| `/help` | list commands and keys |
| `/reset` | start a new conversation; the previous one stays saved on disk |
| `/cleanup` | delete **all** saved conversations after you type `yes` to confirm |
| `/usage` | token usage and ESTIMATED cost of this conversation |
| `/exit` | exit; the conversation stays saved |

Ctrl+D (on empty input) or Ctrl+C while idle exits; Ctrl+C while waiting for
a response cancels that request. In the fullscreen UI, PgUp/PgDn and Up/Down
scroll the conversation.

## Conversation history

Interactive chats are saved automatically and the most recently updated
conversation is restored on the next launch (plain chat and fullscreen UI
alike). One-shot prompts are never saved.

- Location: `$XDG_STATE_HOME/neurobrau/conversations/`, or
  `~/.local/state/neurobrau/conversations/` when `XDG_STATE_HOME` is unset.
  One JSON file per conversation, named `<id>.json`.
- **Stored locally in plaintext.** Anyone who can read these files can read
  your conversations. Directories are created with mode `0700` and files with
  `0600` where the OS supports it. API keys are never written.
- Saved immediately after each successful response, by writing a temporary
  file and atomically renaming it, so a crash or Ctrl+C never leaves a
  half-written file. Failed or cancelled requests are not saved.
- Unreadable or corrupt files are reported at startup, skipped, and left
  untouched. Storage errors are reported as warnings; the chat keeps working.
- The JSON format is provider-independent and shared with the Go and
  TypeScript implementations; see
  [docs/tui-spec.md](docs/tui-spec.md#11-conversation-storage).

Flags:

```bash
neurobrau --no-history            # neither restore nor save for this run
neurobrau --transcript-on-exit    # print the conversation after the fullscreen UI closes
```

`--transcript-on-exit` applies to the fullscreen UI only (the plain chat is
already in your scrollback). The transcript includes restored messages.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | success |
| 1 | other API error |
| 2 | invalid arguments |
| 3 | missing `OPENAI_API_KEY` |
| 4 | authentication failed |
| 5 | rate limit or quota exceeded |
| 6 | timeout |
| 7 | connection error |
| 8 | streamed answer incomplete (`--stream`) |
| 130 | chat interrupted with Ctrl+C |

## Limitations

- OpenAI's model list hides obvious non-text models by name; other listed
  models may still be rejected by the Responses API (shown as an error).
- The pricing catalog only covers models whose prices were verified on
  OpenAI's official pages; prices change, so `PRICING_VERSION` must be
  updated with them. Cancelled requests may still be billed by the provider
  but report no usage, so they are not counted. Data-residency uplifts and
  non-default cache retention are not modelled.
- gpt-6-luna supports none, low, medium, high, xhigh and max; its documented
  default is medium, but without a saved or CLI choice this app still sends
  `none` (the project default since Task 002).
- OpenAI reasoning choices come from a small built-in table
  (`KNOWN_MODELS` in `providers/openai_provider.py`); other models offer
  only Default in F4. A model that rejects a built-in default is retried
  once without it; a value you chose is reported as an error instead.
- Reasoning returned by Ollama is kept separate and currently not shown.
- Discovery and availability checks block the UI briefly (bounded by 5 s for
  OpenAI and 3 s for Ollama). Ollama chat requests time out after 300 s.
- F-keys and arrows rely on the terminal matching its `TERM` setting; an
  unrecognized escape sequence may be read as Esc followed by typed text.
- Running two chats at once against the same history directory can lose
  messages: both continue the same conversation and the last save wins.
- Requests use `store=False`. Context is sent by replaying prior user and
  assistant **text** messages. Full Responses API output-item replay
  (reasoning items, tool calls) is not implemented yet.
- No streaming yet.
- The fullscreen UI counts one column per character; wide characters (CJK,
  emoji) may misalign.

## Version

The version is defined once, as `__version__` in
`src/neurobrau_cli/__init__.py`; `pyproject.toml` reads it at build time
(`dynamic = ["version"]`). After changing it, reinstall
(`.venv/bin/pip install -e '.[dev]'`) so the installed metadata matches.

## Tests

```bash
.venv/bin/python -m pytest
```

Tests mock the OpenAI SDK and never make real API calls.
