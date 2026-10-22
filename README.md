# NeuroBräu CLI — Python

Educational multi-provider LLM command-line client. Currently supports OpenAI
via the Responses API.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
export OPENAI_API_KEY="..."   # read from the environment only
```

## Usage

One-shot prompt:

```bash
neurobrau "What is a neural network?"
neurobrau --model MODEL_ID --reasoning-effort low --max-output-tokens 200 --usage "Explain backprop"
```

Interactive chat (no prompt argument; model options still apply):

```bash
neurobrau                  # fullscreen DOS-style UI in a terminal
neurobrau --plain          # line-oriented chat
neurobrau --model MODEL_ID --usage
```

The fullscreen UI starts only when stdin and stdout are terminals; otherwise
the line-oriented chat is used. Layout, colors and keys are specified in
[docs/tui-spec.md](docs/tui-spec.md).

Chat commands (backslash aliases such as `\help` also work):

| Command | Effect |
|---|---|
| `/help` | list commands and keys |
| `/reset` | start a new conversation; the previous one stays saved on disk |
| `/cleanup` | delete **all** saved conversations after you type `yes` to confirm |
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
| 130 | chat interrupted with Ctrl+C |

## Limitations

- Running two chats at once against the same history directory can lose
  messages: both continue the same conversation and the last save wins.
- Requests use `store=False`. Context is sent by replaying prior user and
  assistant **text** messages. Full Responses API output-item replay
  (reasoning items, tool calls) is not implemented yet.
- No streaming yet.
- The fullscreen UI counts one column per character; wide characters (CJK,
  emoji) may misalign.

## Tests

```bash
.venv/bin/python -m pytest
```

Tests mock the OpenAI SDK and never make real API calls.
