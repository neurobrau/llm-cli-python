# NeuroBräu TUI Specification

A minimal, retro DOS-style fullscreen chat interface. This document is the
reference for the Python implementation (`src/neurobrau_cli/ui/tui.py`) and
for future Go and TypeScript ports. Ports should match it character for
character where practical.

## 1. When the TUI starts

| Invocation | Mode |
|---|---|
| `neurobrau` with stdin **and** stdout attached to a terminal, `TERM` set and not `dumb` | Fullscreen TUI |
| `neurobrau --plain` | Line-oriented chat (`YOU >` / `AI >`) |
| `neurobrau` with piped/redirected stdin or stdout | Line-oriented chat |
| `neurobrau "question"` | One-shot request, no chat |
| Platform without curses support | Line-oriented chat |

Flags for interactive mode (both chat styles unless noted):

| Flag | Effect |
|---|---|
| `--no-history` | do not restore or save conversations for this run (section 11) |
| `--transcript-on-exit` | TUI only: print the transcript after the terminal is restored (section 9) |

The API key is checked **before** entering fullscreen mode; a missing key
exits with code 3 without touching the terminal.

### Startup contents

When a saved conversation is restored (section 11), the conversation panel
starts with its messages as normal user/AI entries, followed by:

1. one error entry per startup warning: `Warning: <message>` (e.g. a corrupt
   file was skipped);
2. an info entry
   `Restored conversation from YYYY-MM-DD HH:MM (N messages). Type /reset to start a new one.`
   (local time of `updated_at`). Not shown for an empty conversation.

## 2. Layout

Baseline size is 80 columns × 25 rows. All regions stretch horizontally;
only the conversation panel and the LLM SETTINGS panel next to it stretch
vertically. Exact render at the baseline (input empty, Ollama selected):

```
row  0  +-[ NeuroBräu CLI 0.1.0 ]---------------------------------+-[ LLM SETTINGS ]---+
row  1  | YOU > What is a neural network?                         | F2 MODEL           |
row  2  |                                                         |  qwen3.5:4b        |
row  3  | AI > A neural network is a model made of layers of      | F3 PROVIDER        |
row  4  |      connected units that learn weights from data.      |  Ollama            |
row  5  |                                                         | F4 REASONING       |
row  6  |                                                         |  none              |
row  7  |                                                         | F5 MAX TOKENS      |
row  8  |                                                         |  default           |
row  9  |                                                         | F6 ANSWER STYLE    |
row 10  |                                                         |  default           |
row 11  |                                                         | TOKENS             |
row 12  |                                                         |  in 17 out 30      |
row 13  |                                                         |  total 47          |
 ...    |                                                         |                    |
row 18  |                                                         |                    |
row 19  +-[ Status ]----------------------------------------------+--------------------+
row 20  | Provider: ollama  Model: qwen3.5:4b  Tokens: in 17 out 30 total 47   [READY] |
row 21  +-[ Input ]--------------------------------------------------------------------+
row 22  | YOU >                                                                        |
row 23  +------------------------------------------------------------------------------+
row 24   /help /reset /exit | F2 model F3 provider | PgUp/PgDn scroll | Ctrl+C quit
```

For a terminal of `R` rows and `C` columns:

| Region | Rows | Notes |
|---|---|---|
| Title border | `0` | `+-[ NeuroBräu CLI <version> ]---…---+-[ LLM SETTINGS ]---+` |
| Conversation panel | `1` … `R-7` | height `R-7` (18 at baseline), columns `0` … `P` |
| LLM SETTINGS panel | `1` … `R-7` | columns `P` … `C-1`; see section 4a |
| Status separator | `R-6` | `+-[ Status ]---…---+`, with `+` at column `P` |
| Status panel | `R-5` | provider, model, tokens, state indicator |
| Input separator | `R-4` | `+-[ Input ]---…---+` |
| Input line | `R-3` | `YOU > ` label + editable text |
| Bottom border | `R-2` | `+---…---+` |
| Help line | `R-1` | key hints, no border, truncated to `C-1` columns |

* Borders use plain ASCII: `+` corners, `-` horizontal, `|` vertical.
  Separator titles are written as `-[ Title ]` immediately after the left `+`.
* **Settings panel:** shown when `C >= 64`. Its separator column is
  `P = C - 22` (58 at baseline); the panel is 22 columns wide including that
  separator: separator, one space, **18 content columns** (`P+2` … `P+19`),
  one space, right border. Below 64 columns the panel is hidden, the
  conversation uses the full width, and F2/F3 still work.
* Conversation text runs from column `2` to `P-2` (or `C-3` without the
  panel): width `C-25` with the panel (55 at baseline), `C-4` without.
* Status and input lines always span the full width: columns `2` to `C-3`
  (`C-4` wide).
* The top border is two rules joined at `P`: `horizontal_rule(P+1, title)`
  followed by `horizontal_rule(C-P, "LLM SETTINGS")` without its first `+`.
* The bottom-right cell (`R-1`, `C-1`) is never written (writing it makes many
  terminals scroll).

### Minimum size

Minimum is **40 × 12** (conversation panel of 5 rows). Below that, the screen
is cleared and shows, in the error color, starting at the top-left:

```
Terminal too small.
Need 40x12, have <C>x<R>.
Resize or press Ctrl+C.
```

Lines are clipped to the terminal width. Input is still accepted, and the
normal layout returns as soon as the terminal is resized large enough.
Resizes are handled on every redraw; no state is lost.

## 3. Colors

All colors are drawn on a **black background** (the whole screen background
is black, not the terminal default).

| Role | Foreground | Attribute | Used for |
|---|---|---|---|
| border | green | bold | borders, region titles, help line |
| text | white / light gray | normal | message text, status text, input text |
| user | blue | bold | `YOU > ` labels (conversation and input line) |
| ai | green | bold | `AI > ` labels |
| info | green | normal | `/help` output, "Conversation history cleared." |
| error | red | bold | API errors, unknown commands, "Request cancelled.", too-small message |
| waiting | yellow | bold | busy/waiting/confirm indicators |
| selected | white / light gray | reverse | highlighted menu item |

Settings panel headings (`F2 MODEL`, …) use the border style; values use
the text style.

Monochrome terminals: bold for border/user/ai/error, reverse video for the
waiting indicator and the selected menu item, normal for everything else.

No animation, mouse support, images or other decoration.

## 4. Conversation panel

* Entries are shown in chronological order with **one blank line between
  entries**.
* User entries start with `YOU > `, assistant entries with `AI > `
  (label in its role color, text in the text color).
* Text is word-wrapped to `(conversation width) - len(label)` columns. Continuation lines are
  indented by the label width (hanging indent), so text aligns under the
  first character after the label.
* Explicit line breaks in the text are kept; blank lines are kept.
* Leading spaces of a line (e.g. code indentation) are kept on every wrapped
  continuation of that line, capped at half the available width.
  Tabs are expanded to 4 spaces.
* Words longer than the available width are broken at the width.
* Info and error entries have no label and wrap to the full content width.

### Scrolling

* The scroll position is stored as **lines scrolled up from the bottom**
  (`0` = newest content visible). This keeps the view stable across resizes.
* The view is clamped so it never scrolls past the oldest line.
* Up/Down scroll by 1 line; PgUp/PgDn scroll by `panel height - 1` lines.
* Submitting input (message or command) returns to the bottom.
* While scrolled up, the title border reads
  `[ NeuroBräu CLI 0.1.0 - scrolled up N (PgDn) ]`.

## 4a. LLM SETTINGS panel

Rows from the top of the panel (row 1), heading then value; values are
indented by one space and clipped to 18 columns with `~` marking a cut
(e.g. ` gpt-6-luna-2026-~`). Rows beyond the panel height are not drawn.

| Rows | Heading | Value |
|---|---|---|
| 1–2 | `F2 MODEL` | current model ID |
| 3–4 | `F3 PROVIDER` | `OpenAI` or `Ollama` |
| 5–6 | `F4 REASONING` | `--reasoning-effort` value (display only for now) |
| 7–8 | `F5 MAX TOKENS` | `--max-output-tokens` or `default` (display only) |
| 9–10 | `F6 ANSWER STYLE` | `default` (display only) |
| 11 | `TOKENS` | |
| 12–13 | | ` in N out N` / ` total N`, or a single ` -` |

Token values follow the status panel (last response; `?` for a missing count).

## 4b. Selection menus (F2, F3)

A bordered box drawn over the conversation area:

```
|                                                         |
|             +-[ SELECT OLLAMA MODEL ]----+              |
|             | * qwen3.5:4b   4.2B Q4_K_M |              |
|             |   llama3.2:3b  3.2B Q4_K_M |              |
|             +-[ 1/2 Enter=OK Esc=cancel ]+              |
```

* Title: `SELECT PROVIDER` or `SELECT <PROVIDER> MODEL` (provider label in
  upper case). Footer: `<n>/<total> Enter=OK Esc=cancel`.
* Each item: `* ` for the current selection or two spaces, the label, and an
  optional note (Ollama: parameter size and quantization) right-aligned when
  it fits. The highlighted item is drawn in the selected style across the
  box's inner width.
* Width: at least 30 and wide enough for the longest item + 4, the title + 6
  and the footer + 6, capped at `C-2`. Centered over the conversation area
  (columns `0` … `P`) when it fits there, otherwise over the whole width.
  Vertically centered in the conversation rows.
* At most `(conversation height) - 2` items are visible; the list scrolls
  to keep the highlighted item visible. It opens with the current item
  highlighted.
* While a menu is open, keys go only to the menu (nothing is typed):

  | Key | Action |
  |---|---|
  | Up / Down | previous / next item |
  | PgUp / PgDn | move by `visible - 1` items |
  | Home / End | first / last item |
  | Enter | select |
  | Esc, Ctrl+D | close without changes |
  | Ctrl+C | exit (code 130) |

**F3 provider menu** lists OpenAI and Ollama without any network request.
Selecting the current provider does nothing. Selecting another provider:

1. shows `[CHECKING <PROVIDER>...]` and checks availability (OpenAI: API key
   present and the Models API answers; Ollama: `/api/tags` answers and at
   least one local model exists);
2. if unavailable, appends `<Label> is not available: <reason>` as an error
   entry and keeps the current provider;
3. otherwise switches to that provider's default model (OpenAI:
   `gpt-6-luna`; Ollama: first local model listed).

**F2 model menu** shows `[LOADING <PROVIDER> MODELS...]` while discovering
the current provider's models (cached for the rest of the session), then
opens the menu. Discovery errors become an error entry `Error: <message>`;
an empty list gives `No selectable <Label> models were found.`

**Switching** (provider or model) always starts a new conversation; the
previous messages are never sent to the new provider or model. The panel
is cleared, token usage reset, and an info entry is shown:
`Now using <Label> / <model>. Started a new conversation` followed by
`; the previous one stays saved.` (history enabled and the old
conversation had messages), `; the previous messages are not sent to the new
model.` (`--no-history`), or `.` (it was empty). See section 11 for storage.

**F4, F5, F6** append an info entry explaining that the setting is
display-only for now (set via `--reasoning-effort` / `--max-output-tokens`).
Ctrl+C during a check or discovery cancels it (`Cancelled.`).

## 5. Status panel

Left-aligned:

```
Provider: <provider>  Model: <model>  Tokens: in <in> out <out> total <total>
```

`Tokens: -` until the first successful response that reports usage, and
again after `/reset`. Missing individual counts are shown as `?`. Usage shows
the **last** response, not a session total. It is always visible; the
`--usage` flag only affects the plain and one-shot modes.

Right-aligned state indicator, in priority order:

| Indicator | When | Style |
|---|---|---|
| `[WAITING FOR RESPONSE...]` | a request is in flight | waiting |
| `[CHECKING OLLAMA...]`, `[LOADING OPENAI MODELS...]`, … | availability check or model discovery | waiting |
| `[CONFIRM: TYPE YES]` | `/cleanup` is waiting for confirmation | waiting |
| `[READY]` | otherwise | text |

The left text is truncated so it never overlaps the indicator.

## 6. Input line

* `YOU > ` label followed by the input text and the visible terminal cursor.
* When the text is wider than the field, it scrolls horizontally so the
  cursor is always visible.
* Non-ASCII input (e.g. `Grüß Gott`) is supported.

## 7. Keyboard controls

| Key | Action |
|---|---|
| printable characters | insert at cursor |
| Enter | submit the input line |
| Backspace | delete character before cursor |
| Delete | delete character at cursor |
| Left / Right | move cursor |
| Home / Ctrl+A | cursor to start |
| End / Ctrl+E | cursor to end |
| Ctrl+U | clear input line |
| Up / Down | scroll conversation by one line |
| PgUp / PgDn | scroll conversation by one page |
| Ctrl+D | exit (only when the input line is empty) |
| Ctrl+C (idle) | exit with code 130 |
| Ctrl+C (waiting) | cancel the request, keep running |
| F2 | model menu (section 4b) |
| F3 | provider menu (section 4b) |
| F4 / F5 / F6 | explain that the setting is display-only for now |

Other keys are ignored, including Esc outside a menu. The help line reads
` /help /reset /exit | F2 model F3 provider | PgUp/PgDn scroll | Ctrl+C quit`.
Esc is recognized after 25 ms (curses `set_escdelay`).

## 8. Submitting input

1. The input line is trimmed and cleared. Empty input does nothing.
2. A single word starting with `/` or `\` is a **command** and is never sent
   to the model:

   | Command | Effect |
   |---|---|
   | `/help`, `\help` | append the help text (commands and keys) as an info entry |
   | `/reset`, `\reset` | start a new empty conversation, clear the panel and token usage, show a notice (below). The previous conversation stays on disk. |
   | `/cleanup`, `\cleanup` | ask for confirmation, then delete all saved conversations (below) |
   | `/exit`, `\exit` | exit with code 0; the saved conversation is kept |
   | anything else | error entry: `Unknown command: /<name>. Type /help for commands.` |

   Command names are case-insensitive.

   `/reset` notice: `Started a new conversation. The previous one stays saved.`
   with history enabled, `Conversation history cleared.` with `--no-history`.
   Resetting an already empty conversation reuses it.

   `/cleanup` flow:
   * History disabled: info `Conversation history is disabled for this session (--no-history).`
   * No files: info `No saved conversations.`
   * Otherwise: info entry
     `Delete N saved conversation(s) in <dir>? This cannot be undone. Type 'yes' to confirm: `
     and the indicator switches to `[CONFIRM: TYPE YES]`. The **next submitted
     line** is the answer and is never sent to the model or run as a command.
     Only `yes` (case-insensitive, surrounding spaces ignored) confirms.
     Confirmed: delete, clear the panel, show
     `Deleted N saved conversation(s). Started a new conversation.`
     Anything else: `Cleanup cancelled. Nothing was deleted.`
3. Otherwise the text is a message:
   1. Append it to the panel as a user entry.
   2. Show `[WAITING FOR RESPONSE...]` and redraw **before** the blocking
      request starts.
   3. Send the committed history plus the new user message.
   4. On success: commit the user/assistant pair to history, append the
      reply as an AI entry, update token usage, then save the conversation
      (section 11). If saving fails, append `Warning: <message>` as an error
      entry; the exchange stays in memory and the chat continues.
   5. On failure: append `Error: <message>` as an error entry; history is
      unchanged (the user entry stays visible but is not part of the context).
   6. Return to `[READY]`.

Responses are not streamed.

## 9. Exit and terminal restoration

| Exit | Code |
|---|---|
| `/exit`, `\exit`, Ctrl+D on empty input | 0 |
| Ctrl+C while idle | 130 |
| Missing API key (before the TUI starts) | 3 |

The terminal (alternate screen, echo, cursor mode, colors) is always
restored on exit, including on exceptions; in Python this is guaranteed by
`curses.wrapper`. No traceback is printed for Ctrl+C.

With `--transcript-on-exit`, **after** the terminal is restored (so the text
stays in the normal scrollback) and for every exit path above, the complete
conversation, including restored messages, is printed to stdout:

```
YOU > first message

AI > first reply

YOU > second message
```

Each message is `YOU > ` or `AI > ` followed by its content unchanged (no
wrapping; the terminal wraps). Messages are separated by one blank line and
the output ends with a single newline. Nothing is printed for an empty
conversation. Only committed messages appear (no errors or notices).

## 10. Porting notes

* Keep the pure logic (layout, wrapping, scrolling, key handling) separate
  from drawing so it can be tested with a fake screen.
* Use one redraw-everything function per event; terminal libraries diff the
  output.
* Python uses `textwrap`; ports must reproduce its behavior: greedy word
  wrap, collapse nothing inside a line, break over-long words at the width.
* Character widths are counted as one column per code point; wide (CJK,
  emoji) characters are a known limitation.

## 11. Conversation storage

Shared by the plain chat and the TUI, and by all three implementations, so a
conversation saved by one can be continued by another.

### Location

`$XDG_STATE_HOME/neurobrau/conversations/` if `XDG_STATE_HOME` is set to an
**absolute** path; otherwise `~/.local/state/neurobrau/conversations/`.
One file per conversation: `<id>.json`. Other files in the directory are
ignored, and left alone by `/cleanup`.

Files are **plaintext**. Create the `neurobrau` and `conversations`
directories with mode `0700` and files with `0600` where supported, and
tighten both directories to `0700` at startup if they already exist.
Never change directories above `neurobrau`. Never store credentials.

### File format (schema version 1)

UTF-8 JSON (non-ASCII written as-is, 2-space indent, trailing newline):

```json
{
  "schema_version": 1,
  "id": "4b8ba3bd-5f42-4302-a01e-2d6ed0eb4905",
  "created_at": "2026-10-21T12:00:00.000Z",
  "updated_at": "2026-10-21T12:03:41.512Z",
  "provider": "ollama",
  "model": "qwen3.5:4b",
  "messages": [
    {"role": "user", "content": "What is a neural network?"},
    {"role": "assistant", "content": "A neural network is ..."}
  ]
}
```

| Field | Type | Rules |
|---|---|---|
| `schema_version` | integer | must be `1`; other values are unsupported (reported, file left alone) |
| `id` | string | canonical lowercase UUID (version 4 when generated); also the file name |
| `created_at` | string | RFC 3339 with time zone; written as UTC, millisecond precision, `Z` suffix |
| `updated_at` | string | same format; set on every save |
| `provider` | string, optional | `openai` or `ollama`; written whenever known. Absent in older files, which count as `openai` |
| `model` | string, optional | model ID the conversation was held with |
| `messages` | array | chronological; each `{"role": "user" \| "assistant", "content": string}` |

Readers must accept any RFC 3339 offset (e.g. `+02:00`) and ignore unknown
fields at the top level and in messages, so newer writers stay compatible.
Messages are provider-independent text; no reasoning output or token data
is stored. `provider` and `model` identify who the conversation belongs to,
so it is never continued with a different one. Writers emit keys in the
order shown and omit `provider`/`model` when unknown.

### Behavior

* **Restore:** at interactive startup, load every `*.json` file and continue
  the valid matching one with the greatest `updated_at` (ties: newest file
  modification time). Matching:
  * no `--provider` and no `--model`: any conversation; its `provider` and
    `model` become the active selection (so the last conversation continues
    with the same model, even across providers);
  * `--provider` only: conversations of that provider; its `model` is adopted;
  * `--model` (with or without `--provider`, which defaults to `openai`):
    conversations with exactly that provider and model.
  * Files without `provider` count as `openai`; without `model` they match
    any model of that provider and adopt the requested or default model.

  No match means a new conversation for the requested (or default)
  selection, written at its first successful exchange.
* **Switching provider/model** (TUI F2/F3): a non-empty conversation stays
  on disk and a new one (new `id`, `created_at`) is started; an empty one is
  reused. The (possibly empty) conversation is saved immediately with the new
  `provider`/`model`, so it is the one restored next time.
* **Invalid files** (unreadable, not UTF-8, not JSON, failing the rules
  above) produce a startup warning
  `Ignoring unreadable conversation file <path>: <reason>. The file was left unchanged.`
  and are skipped; they are never modified or deleted except by a confirmed
  `/cleanup`.
* **Save:** after each successful exchange, write the whole file atomically:
  write a temp file in the same directory (`.<id>.<random>.tmp`, mode
  `0600`), flush and fsync it, rename it over `<id>.json`, then fsync the
  directory where supported. Remove the temp file if anything fails,
  including Ctrl+C.
* **Failed requests** (API errors, Ctrl+C while waiting) save nothing.
* **`/reset`:** if the conversation is non-empty, start a new one with a new
  `id` and `created_at`, and save it immediately (empty `messages`) so it is
  the one restored next time. The old file is untouched.
* **`/cleanup`:** after confirmation, delete every `*.json` file in the
  directory, then start a new unsaved conversation.
* **`--no-history`:** no reads, writes or permission changes at all.
* **Errors** (permissions, disk full, path is a file, …) are shown as
  `Could not <read|save|delete> conversation history at <path>: <reason>.`
  and never as a stack trace.
* Concurrent instances are not coordinated; the last save of a file wins.

## 12. Providers and model discovery

Both providers implement the same small interface: send text messages and
get text back (reasoning kept separate), token usage, an availability check,
model discovery, and errors normalized to these kinds (exit codes in
one-shot/startup failures): API 1, missing API key 3, authentication 4,
rate limit 5, timeout 6, connection 7.

| | OpenAI | Ollama |
|---|---|---|
| Endpoint | Responses API (`store=false`) | `POST /api/chat`, `stream=false` |
| Credentials | `OPENAI_API_KEY` environment variable | none |
| Server | SDK default | `OLLAMA_HOST` (`host`, `host:port` or URL), default `http://127.0.0.1:11434`; `0.0.0.0` means local |
| Max tokens | `max_output_tokens` | `options.num_predict` |
| Reasoning | `reasoning.effort`; if the API rejects that parameter for a model (HTTP 400, `param` starting with `reasoning`), retry once without it and remember the model for the session | `think`: sent only if the model reports the `thinking` capability; `false` unless an effort other than `none` is chosen |
| Reasoning output | not requested | `message.thinking`, or a leading `<think>…</think>` block, is moved out of the answer |
| Usage | input / output / total tokens | `prompt_eval_count` / `eval_count` / their sum |
| Discovery | Models API, 5 s timeout, no retries; IDs containing `embedding`, `tts`, `whisper`, `dall-e`, `moderation`, `transcribe`, `realtime`, `audio`, `image` are hidden (heuristic) | `GET /api/tags`, 3 s timeout |
| Availability | key present and the Models API answers | `/api/tags` answers and at least one local model |
| Default model | `gpt-6-luna` | first selectable model in `/api/tags` order (Ollama lists the most recently modified first) |
| Chat timeout | SDK default | 300 s (the first request may load the model) |

### Ollama local vs. cloud models

`/api/tags` also lists **cloud** models that run on ollama.com. A model is
remote if its entry has `remote_host` or `remote_model`, or its tag is
`cloud` or ends in `-cloud` (`glm-4.7:cloud`, `gemma4:31b-cloud`). Remote
models are never selectable, never sent chat requests, and never queried
with `/api/show` (which would contact the remote service). Models whose
`capabilities` lack `completion` (e.g. embedding models) are hidden; models
without reported capabilities (older Ollama) are shown.

Before the first chat with a model, it must be found in `/api/tags` (one
refresh if it is missing, in case it was just installed). If capabilities are
missing from the tags entry, one `POST /api/show` per model is made and
cached. Missing models produce
`Ollama model '<m>' is not installed locally. Install it yourself with: ollama pull <m>`;
nothing is ever downloaded automatically. Requests to a loopback server
bypass HTTP proxies.

Discovery results are cached per provider for the session; nothing is
requested until a provider is used, F2/F3 is selected, or (Ollama without
`--model`) a default model must be determined.
