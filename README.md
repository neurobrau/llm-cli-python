# NeuroBräu CLI — Python

An educational multi-provider LLM client for the terminal: one-shot prompts,
a line-oriented chat, and a fullscreen DOS-style interface. It talks to
OpenAI (Responses API) and to local models through [Ollama](https://ollama.com).

**Features**

- OpenAI and local Ollama models; switch provider (F3) and model (F2) at runtime
- One-shot prompts, plain chat and a fullscreen curses UI
- Streaming output with Ctrl+C cancellation (`--stream`)
- Persistent conversations, one per provider/model, restored after restarts
- Reasoning effort, max output tokens and answer style per model (F4–F6)
- Token accounting as reported by the API, with ESTIMATED costs
- No credentials on disk: the OpenAI key is read from the environment only

## Requirements

- Linux (tested on Fedora 44); macOS should work but is untested
- Python 3.14 or later
- For OpenAI: an API key in `OPENAI_API_KEY`
- For local models: Ollama running locally with at least one model installed

## Installation

From a built wheel (recommended; see [Building](#building)):

```bash
sudo dnf install python3 python3-pip      # Fedora 44 ships Python 3.14
python3 -m venv ~/.local/share/neurobrau-venv
~/.local/share/neurobrau-venv/bin/pip install neurobrau_llm_cli-0.3.0-py3-none-any.whl
mkdir -p ~/.local/bin
ln -s ~/.local/share/neurobrau-venv/bin/neurobrau ~/.local/bin/neurobrau
neurobrau --version
```

From a source checkout (development):

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/neurobrau --version
```

`python -m neurobrau_cli` works as well.

## Ollama setup

```bash
curl -fsSL https://ollama.com/install.sh | sh    # installs the ollama systemd service
sudo systemctl enable --now ollama
ollama pull qwen3.5:4b                            # NeuroBräu never downloads models
neurobrau --provider ollama "Hello"
```

The default server is `http://127.0.0.1:11434`; set `OLLAMA_HOST` for another
one. Only local models are used: cloud models that Ollama lists (such as
`glm-4.7:cloud`) are never selected or contacted.

## OpenAI configuration

```bash
export OPENAI_API_KEY="sk-..."        # e.g. in ~/.bashrc; never commit it
neurobrau "What is a neural network?"
```

The key is read from the environment only and never written to any file.
The default model is `gpt-6-luna`.

## Usage

### One-shot

```bash
neurobrau "Explain backpropagation"
neurobrau --provider ollama --model qwen3.5:4b --stream "Explain neural networks"
neurobrau --answer-style brief --max-output-tokens 300 --usage "Summarize TCP"
neurobrau --reasoning-effort high "Prove that sqrt(2) is irrational"
```

The answer goes to stdout, `--usage` statistics to stderr. Piping works:
`neurobrau "List 10 primes" | head -n 3`.

### Interactive

```bash
neurobrau                    # fullscreen UI (when stdin and stdout are a terminal)
neurobrau --stream           # ... with live streaming
neurobrau --plain            # line-oriented chat
neurobrau --provider ollama  # start with Ollama
```

Without `--provider`/`--model`, the chat continues with the provider, model
and conversation that were active last time.

### Options

| Option | Effect |
|---|---|
| `--provider openai\|ollama` | provider (default: openai, or the last one used) |
| `--model ID` | model (default: `gpt-6-luna`; Ollama: most recently installed local model) |
| `--reasoning-effort E` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, or `model-default` |
| `--max-output-tokens N` | output limit for this run |
| `--answer-style S` | `brief`, `normal` or `detailed` |
| `--stream` | show text as it is generated (one-shot, plain chat and fullscreen) |
| `--usage` | print token usage to stderr |
| `--plain` | use the line-oriented chat |
| `--no-history` | neither restore nor save conversations |
| `--transcript-on-exit` | print the conversation after the fullscreen UI closes |

Command-line options override saved settings for one run and are not saved.

### Keyboard controls (fullscreen UI)

| Key | Action |
|---|---|
| F1 | help |
| F2 | choose model |
| F3 | choose provider (OpenAI, Ollama) |
| F4 | reasoning effort / thinking |
| F5 | max output tokens (presets or custom) |
| F6 | answer style (Brief, Normal, Detailed) |
| Enter | send |
| PgUp/PgDn, Up/Down | scroll the conversation |
| Ctrl+C | cancel the running answer; when idle, quit |
| Ctrl+D | quit (on empty input) |
| Esc | close a menu or the custom-token prompt |

In menus: Up/Down, PgUp/PgDn, Home/End, Enter, Esc. F2 and F3 switch
conversations; F4–F6 apply from the next message and keep the conversation.

### Chat commands

`/help`, `/reset` (new conversation; the old one stays saved), `/usage`
(token usage and estimated cost), `/cleanup` (delete all saved conversations
after typing `yes`), `/exit`. Backslash forms such as `\help` also work.

### Reasoning defaults

Without a saved or command-line choice, NeuroBräu sends reasoning effort
`none` where the model supports it (Ollama: thinking off). **Model default**
in F4, or `--reasoning-effort model-default`, sends no effort at all, so the
model uses its own default: `medium` for gpt-6-luna. Only values confirmed
for a model are offered; models whose support is unknown show only Model
default.

### Streaming and cancellation

With `--stream`, text appears as it arrives. Ctrl+C cancels the answer: the
partial text is shown but neither saved nor sent as context, and the chat
continues. Only completed answers become part of a conversation. A streamed
answer cut off by the token limit is reported (exit code 8 in one-shot mode).

## Persistent conversations

Interactive chats are saved after every completed answer and restored on the
next start. Each provider/model combination keeps its own conversation;
switching back with F2/F3 returns to it. One-shot prompts are not saved.
Files are written atomically with owner-only permissions, and corrupt files
are reported and skipped, never deleted.

## Configuration and file locations

| What | Location |
|---|---|
| Conversations | `$XDG_STATE_HOME/neurobrau/conversations/<id>.json` (default `~/.local/state/…`) |
| Active provider/model | `$XDG_STATE_HOME/neurobrau/active.json` |
| Settings (F4–F6 choices) | `$XDG_CONFIG_HOME/neurobrau/settings.json` (default `~/.config/…`) |
| OpenAI key | `OPENAI_API_KEY` environment variable only |
| Ollama server | `OLLAMA_HOST` (default `127.0.0.1:11434`) |

Directories are `0700`, files `0600`. Conversations are stored **in
plaintext**. The JSON formats are documented in
[docs/tui-spec.md](docs/tui-spec.md) (sections 11, 13 and 15).

## Usage and estimated pricing

Token usage is stored exactly as the provider reports it (input, cached
input, cache writes, output, reasoning, total) and shown in the LLM SETTINGS
panel and by `/usage`. Every request re-sends the whole conversation, so
billed input grows with each turn.

Costs are **ESTIMATED**, never billed amounts. They use a small, versioned
catalog of official OpenAI prices (`src/neurobrau_cli/pricing.py`, version
2026-10-22: gpt-6-luna and gpt-5.1), including cached input, cache writes,
long prompts and processing tiers. When the model, tier or a needed count is
unknown, the estimate is marked `~` (qualified), `>=` (lower bound) or
`unavailable`; nothing is invented. Local Ollama inference costs `$0`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `OPENAI_API_KEY is not set` (exit 3) | `export OPENAI_API_KEY=...`, or use `--provider ollama` |
| `Could not connect to Ollama` (exit 7) | `systemctl status ollama`; `sudo systemctl start ollama`; check `OLLAMA_HOST` |
| `No local Ollama models are installed` | `ollama pull qwen3.5:4b` |
| `... is an Ollama cloud model` | choose a local model (F2 or `--model`) |
| F-keys or arrows type odd characters | make sure `TERM` matches your terminal (e.g. `xterm-256color`) |
| No fullscreen UI | it needs a terminal on stdin and stdout; use `--plain` for pipes |
| `Terminal too small` | resize to at least 40×12 (80×25 recommended) |
| Settings ignored with a warning | the settings file is invalid; fix or delete it |
| Cost shows `unavailable` | the model is not in the pricing catalog |

Exit codes: 0 success, 1 API error, 2 invalid arguments, 3 missing API key,
4 authentication failed, 5 rate limit, 6 timeout, 7 connection error,
8 streamed answer incomplete, 130 interrupted, 141 output pipe closed.

## Known limitations

- Prices change: the pricing catalog covers only verified models and needs
  updating with OpenAI's price list. Cancelled requests may be billed by the
  provider but report no usage, so they are not counted.
- OpenAI reasoning choices come from a small built-in capability table.
- Ollama thinking is kept out of answers but not displayed.
- A cancelled stream that is waiting on the network closes when data arrives
  or the read times out (Ollama: 300 s).
- Wide characters (CJK, emoji) may misalign in the fullscreen UI.
- Two instances sharing the same history directory: the last save wins.
- Conversation context is replayed as text; reasoning items and tool calls
  are not replayed.

## Development

```bash
.venv/bin/python -m pytest                      # no real network requests
```

The version is defined once, as `__version__` in
`src/neurobrau_cli/__init__.py`; `pyproject.toml` reads it at build time.

### Building

```bash
python3 -m venv /tmp/build-tools && /tmp/build-tools/bin/pip install build
/tmp/build-tools/bin/python -m build            # dist/*.whl and dist/*.tar.gz
```

Optional standalone Linux executable (bundles Python and the dependencies;
built and tested on Fedora 44, x86-64; needs a compatible glibc):

```bash
python3 -m venv /tmp/pyi && /tmp/pyi/bin/pip install dist/*.whl pyinstaller
/tmp/pyi/bin/pyinstaller --onefile --name neurobrau \
    /tmp/pyi/lib64/python3.14/site-packages/neurobrau_cli/__main__.py
./dist/neurobrau --version
```

Build output (`dist/`, `build/`, `*.spec`) is git-ignored.

The behaviour shared with the planned Go and TypeScript versions is specified
in [docs/tui-spec.md](docs/tui-spec.md).
