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

Chat commands: `/help`, `/reset` (clear history), `/exit`. Backslash aliases
`\help`, `\reset`, `\exit` also work. Ctrl+D (on empty input) or Ctrl+C
while idle exits; Ctrl+C while waiting for a response cancels that request.
In the fullscreen UI, PgUp/PgDn and Up/Down scroll the conversation.

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

- Conversation history lives in process memory and is lost on exit.
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
