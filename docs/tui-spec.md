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
vertically. Exact render at the baseline (input empty, Ollama selected,
right after a completed answer):

```
row  0  +-[ NeuroBräu CLI 0.2.0 ]---------------------------------+-[ LLM SETTINGS ]---+
row  1  | YOU > What is a neural network?                         | F2 MODEL           |
row  2  |                                                         |  qwen3.5:4b        |
row  3  | AI > A neural network is a model made of layers of      | F3 PROVIDER        |
row  4  |      connected units that learn weights from data.      |  Ollama            |
row  5  |                                                         | F4 REASONING       |
row  6  |                                                         |  off               |
row  7  |                                                         | F5 MAX TOKENS      |
row  8  |                                                         |  default           |
row  9  |                                                         | F6 ANSWER STYLE    |
row 10  |                                                         |  normal            |
row 11  |                                                         | TOKENS             |
row 12  |                                                         |  in 17 out 30      |
row 13  |                                                         |  total 47          |
 ...    |                                                         |                    |
row 18  |                                                         |                    |
row 19  +-[ Status ]----------------------------------------------+--------------------+
row 20  | DONE | 7.17s total | load 0.20s | generation 1.88s | 15.96 tok/s             |
row 21  +-[ Input ]--------------------------------------------------------------------+
row 22  | YOU >                                                                        |
row 23  +------------------------------------------------------------------------------+
row 24   /help /reset /exit | F2-F6 settings | PgUp/PgDn scroll | Ctrl+C quit
```

For a terminal of `R` rows and `C` columns:

| Region | Rows | Notes |
|---|---|---|
| Title border | `0` | `+-[ NeuroBräu CLI <version> ]---…---+-[ LLM SETTINGS ]---+` |
| Conversation panel | `1` … `R-7` | height `R-7` (18 at baseline), columns `0` … `P` |
| LLM SETTINGS panel | `1` … `R-7` | columns `P` … `C-1`; see section 4a |
| Status separator | `R-6` | `+-[ Status ]---…---+`, with `+` at column `P` |
| Runtime status bar | `R-5` | dynamic state only (section 5) |
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
  `[ NeuroBräu CLI 0.2.0 - scrolled up N (PgDn) ]`.

## 4a. LLM SETTINGS panel

Rows from the top of the panel (row 1), heading then value; values are
indented by one space and clipped to 18 columns with `~` marking a cut
(e.g. ` gpt-6-luna-2026-~`). Rows beyond the panel height are not drawn.

| Rows | Heading | Value |
|---|---|---|
| 1–2 | `F2 MODEL` | current model ID |
| 3–4 | `F3 PROVIDER` | `OpenAI` or `Ollama` |
| 5–6 | `F4 REASONING` | effective value in provider vocabulary (`none`, `low`, `off`, `on`, …) or `default` (no parameter sent) |
| 7–8 | `F5 MAX TOKENS` | effective limit or `default` |
| 9–10 | `F6 ANSWER STYLE` | `brief`, `normal` or `detailed` |
| 11 | `TOKENS` | |
| 12–13 | | ` in N out N` / ` total N`, or a single ` -` |

`TOKENS` shows **session totals**: the sum over the answers received for
the current conversation in this run (`?` for a count never reported).
`/reset`, `/cleanup` and switching provider/model clear it. The settings
panel is the only place provider, model, settings and tokens are shown.

## 4b. Selection menus (F2–F6)

A bordered box drawn over the conversation area:

```
|                                                         |
|             +-[ SELECT OLLAMA MODEL ]----+              |
|             | * qwen3.5:4b   4.2B Q4_K_M |              |
|             |   llama3.2:3b  3.2B Q4_K_M |              |
|             +-[ 1/2 Enter=OK Esc=cancel ]+              |
```

* Title: `SELECT PROVIDER`, `SELECT <PROVIDER> MODEL` (provider label in
  upper case), `REASONING`, `MAX OUTPUT TOKENS` or `ANSWER STYLE`.
  Footer: `<n>/<total> Enter=OK Esc=cancel`.
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

1. shows `CONNECTING | Checking <Label>` and checks availability (OpenAI: API key
   present and the Models API answers; Ollama: `/api/tags` answers and at
   least one local model exists);
2. if unavailable, appends `<Label> is not available: <reason>` as an error
   entry and keeps the current provider;
3. otherwise switches to that provider's default model (OpenAI:
   `gpt-6-luna`; Ollama: first local model listed).

**F2 model menu** shows `CONNECTING | Loading <Label> models` while discovering
the current provider's models (cached for the rest of the session), then
opens the menu. Discovery errors become an error entry `Error: <message>`;
an empty list gives `No selectable <Label> models were found.`

**Switching** (provider or model): every provider/model combination has its
own conversation, and messages are never sent to a different combination.
The panel is cleared and token usage reset. Then:

* **The target has a conversation** (from earlier in this run, or the most
  recent one on disk): its messages are shown as normal entries, followed by
  the info entry `Now using <Label> / <model>. Restored its conversation (<n> messages).`
* **Otherwise** a new empty conversation starts, with the info entry
  `Now using <Label> / <model>. Started a new conversation` followed by
  `; the previous one stays saved.` (history enabled and the old
  conversation had messages), `; the previous messages are not sent to the new
  model.` (`--no-history`), or `.` (it was empty).

Switching back and forth therefore returns to each conversation where it was
left. See section 11 for storage.

Ctrl+C during a check or discovery cancels it (`Cancelled.`).

## 4c. Generation settings (F4, F5, F6)

These change how the **next** request is made. They never clear the panel,
never start a new conversation, and are saved per model (section 13). Each
confirmed change appends an info entry ending in `Applies from the next
message.` Menu navigation itself makes no requests.

**F4 REASONING.** Shows `CONNECTING | Checking reasoning support` while asking the provider
which values the current model supports (OpenAI: built-in table, no request;
Ollama: one cached `/api/show`). Items:

| Support | Items |
|---|---|
| unknown (OpenAI model not in the table) | `Default (support unknown)` only, plus an info entry: `Reasoning support for <model> is not confirmed, so only the model's default is offered. --reasoning-effort sends a value explicitly.` |
| none | no menu; info entry `<model> does not support reasoning.` |
| values | `Default` (note `model default`; only where omitting the parameter is allowed) followed by the values with labels `None`, `Minimal`, `Low`, `Medium`, `High`, `XHigh`, `Max`, `Off`, `On` or the level name capitalized |

`Default` sends no reasoning parameter. Ollama On/Off models offer no
`Default`: thinking is always sent, `false` unless On is chosen.

**F5 MAX OUTPUT TOKENS.** Shows `CONNECTING | Checking model limits` while reading
the model's limit (OpenAI table; Ollama `context_length` from the cached
`/api/tags`). Items: `Default` (note `model default`), `256`, `512`, `1024`,
`2048`, `4096` (presets above a known limit are left out), then
`Custom...` (note `up to <limit>` or `any positive number`; shown as
`Custom (<n>)` when the current value is not a preset).

`Custom...` turns the input line into a prompt: label `TOKENS > `, indicator
`READY | Type a token limit and press Enter | Esc cancels`, and the info entry
`Type the max output tokens (1-<limit>) and press Enter; Esc cancels.` Any
text typed before is set aside and restored afterwards. On Enter:
* not digits: error `'<text>' is not a positive whole number. Try again or press Esc.`
* `0`: error `Max output tokens must be a positive whole number, not 0. Try again or press Esc.`
* above the limit: error `<n> exceeds the limit of <limit> output tokens for <model>. Try again or press Esc.`
* otherwise the value is applied and the prompt closes.

The prompt stays open after an error; Esc closes it with
`Max output tokens unchanged.` F-keys are ignored while it is open.

**F6 ANSWER STYLE.** `Brief` (`short, concise answers`), `Normal`
(`standard answers`), `Detailed` (`thorough, with examples`). The style is
sent as an instruction with each request (section 12) and is never stored
in the conversation.

**After switching** provider or model (F2/F3), the new model's saved
reasoning and token choices are applied (`CONNECTING | Checking model settings`);
unsupported saved values fall back with an info entry (section 13). The
answer style is global and stays.

## 5. Runtime status bar

Row `R-5` shows **only dynamic runtime state**; provider, model, settings
and token totals are never repeated here (they are in the settings panel).
Format, left-aligned and clipped to the content width:

```
STATE | detail | detail ...
```

The state word is drawn in its style, the rest in the text style; parts
are joined with ` | `.

| State | Style | Shown |
|---|---|---|
| `READY` | text | idle; optional detail (e.g. `Conversation saved`, `New conversation started`, `Saved conversations deleted`, `Type a token limit and press Enter \| Esc cancels`) |
| `CONNECTING` | waiting | a stream started, nothing received yet: `CONNECTING \| 0.4s elapsed \| Ctrl+C Cancel`; also blocking checks: `CONNECTING \| Loading Ollama models` |
| `GENERATING` | waiting | text is arriving: `GENERATING \| 4.2s elapsed \| Ctrl+C Cancel`; thinking but no text yet: `GENERATING \| Thinking \| 1.3s elapsed \| Ctrl+C Cancel`; non-streaming: `GENERATING \| Waiting for the complete response \| Ctrl+C Cancel` |
| `DONE` | ai (green) | answer completed (metrics below) |
| `CANCELLED` | waiting | Ctrl+C: `CANCELLED \| Partial response not saved` (or just `CANCELLED` if no text had arrived) |
| `ERROR` | error | `ERROR \| <message>` |
| `WARNING` | waiting | `WARNING \| Response incomplete: <reason>`, `WARNING \| Conversation not saved`, the `/cleanup` confirmation, refused actions |

Elapsed time is client-observed, one decimal, redrawn every 100 ms while a
request runs.

**DONE metrics** (never estimated; missing values are left out):

* Provider reports durations (Ollama): `DONE | <total>s total | load <l>s | generation <g>s | <r> tok/s`
  with `r = output tokens / generation seconds`, all from the API's final
  chunk (`total_duration`, `load_duration`, `eval_duration`, `eval_count`).
* Otherwise (OpenAI, and any non-streaming answer): client-observed latency only,
  `DONE | <s>s observed | first text after <f>s` (the second part only when
  streaming). No tokens-per-second figure is derived from client time.

Seconds have two decimals here.

**Lifetime.** `DONE`, `CANCELLED`, `ERROR` and `WARNING` stay until the next
key press other than a terminal resize; the bar then returns to `READY`
(after a saved answer: `READY | Conversation saved`). Live activity outranks
stored states; a warning raised *during* a request (e.g. pressing Enter or an
F-key while streaming) is shown over the live state for 2 seconds.

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
| Ctrl+C (request running) | cancel the request, keep running (streaming: section 14) |
| F2 | model menu (section 4b) |
| F3 | provider menu (section 4b) |
| F4 | reasoning menu (section 4c) |
| F5 | max output tokens menu (section 4c) |
| F6 | answer style menu (section 4c) |
| Esc | close the F5 custom-value prompt |

Other keys are ignored, including Esc outside a menu or prompt. The help line reads
` /help /reset /exit | F2-F6 settings | PgUp/PgDn scroll | Ctrl+C quit`.
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
     and the status shows `WARNING | Type yes to delete saved conversations; anything else cancels`. The **next submitted
     line** is the answer and is never sent to the model or run as a command.
     Only `yes` (case-insensitive, surrounding spaces ignored) confirms.
     Confirmed: delete, clear the panel, show
     `Deleted N saved conversation(s). Started a new conversation.`
     Anything else: `Cleanup cancelled. Nothing was deleted.`
3. Otherwise the text is a message. With `--stream` it is streamed
   (section 14, "In the fullscreen UI"); without it:
   1. Append it to the panel as a user entry.
   2. Show `GENERATING | Waiting for the complete response | Ctrl+C Cancel`
      and redraw **before** the blocking request starts.
   3. Send the committed history plus the new user message.
   4. On success: commit the user/assistant pair to history, append the
      reply as an AI entry, add its usage to the session totals, then save
      the conversation (section 11); status `DONE | <s>s observed`. If
      saving fails, append `Warning: <message>` as an error entry; the
      exchange stays in memory and the chat continues.
   5. On failure: append `Error: <message>` as an error entry and show
      `ERROR | <message>`; history is unchanged (the user entry stays visible
      but is not part of the context). Ctrl+C: `Request cancelled.`, `CANCELLED`.

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
  * no `--provider` and no `--model`: the provider/model in `active.json`
    (below); if that file is missing or invalid (warning
    `Ignoring invalid <path>.`), any conversation, whose `provider` and
    `model` then become the active selection;
  * `--provider` only: conversations of that provider; its `model` is adopted;
  * `--model` (with or without `--provider`, which defaults to `openai`):
    conversations with exactly that provider and model.
  * Files without `provider` count as `openai`; without `model` they match
    any model of that provider and adopt the requested or default model.

  No match means a new conversation for the requested (or default)
  selection, written at its first successful exchange.
* **Switching provider/model** (TUI F2/F3): the current conversation is set
  aside (it is already saved after each exchange). The target selection's
  conversation is restored: first one set aside earlier in this run, then the
  most recent matching file (same rules as Restore). A restored file is
  **not rewritten**; only `active.json` is updated. Without a match, a new
  conversation starts and is saved immediately (empty `messages`) so the
  selection is restored next time. An empty current conversation that is not
  yet on disk is reused for it; one that is on disk (e.g. the empty
  conversation `/reset` saved) stays with its own selection, so `/reset`
  followed by switching away and back returns to the empty post-reset
  conversation while the archived one is kept.
* **`--no-history`:** the same per-selection behavior, in memory for the
  current run only.
* **`active.json`:** `<state dir>/neurobrau/active.json` (next to
  `conversations/`, so `/cleanup` does not remove it) records the active
  selection; written atomically with mode `0600` whenever it changes (after
  a save or a switch):

  ```json
  {"schema_version": 1, "provider": "ollama", "model": "qwen3.5:4b"}
  ```

  An unknown provider in it is ignored at startup (defaults are used).
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
  directory, forget conversations set aside in this run, then start a new
  unsaved conversation.
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
| Max tokens limit | built-in table (unknown models: none) | `details.context_length` from `/api/tags` |
| Answer style | `instructions` parameter (system/developer message) | leading `{"role": "system"}` message |
| Reasoning values | built-in table of confirmed efforts per model (exact ID or dated snapshot `-YYYY-MM-DD`); unknown models: none confirmed | `/api/show` `thinking.values`: `false`/`true` → `off`/`on`, strings → named levels; `thinking` capability without metadata → `off`/`on`; otherwise none |
| Reasoning parameter | `reasoning.effort`, omitted for `Default` | `think`: sent only if the model has the `thinking` capability: `false` for none/`off`, `true` for `on` or an effort name on an On/Off model, the level string for named levels |
| Rejected reasoning | value chosen by the user (CLI, F4, saved): error `<model> does not accept reasoning effort '<v>' (…)`, no retry. Built-in default: retry once without it and remember the model for the session | user-chosen thinking on a model without the capability, or an unknown level: error before any chat request |
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

## 13. Settings file

Nonsecret preferences, separate from conversation history and credentials.

### Location

`$XDG_CONFIG_HOME/neurobrau/settings.json` if `XDG_CONFIG_HOME` is an
absolute path, otherwise `~/.config/neurobrau/settings.json`. Directory
`0700`, file `0600` where supported; written atomically like conversation
files. API keys are never written.

### Format (schema version 1)

```json
{
  "schema_version": 1,
  "answer_style": "brief",
  "providers": {
    "ollama": {"models": {
      "qwen3.5:4b": {"reasoning": "on", "max_output_tokens": 600}
    }},
    "openai": {"models": {
      "gpt-5": {"reasoning": "low"},
      "gpt-6-luna": {"max_output_tokens": null}
    }}
  }
}
```

| Field | Rules |
|---|---|
| `schema_version` | must be `1` |
| `answer_style` | `brief`, `normal` or `detailed`; default `normal` |
| `providers.<p>.models.<model>` | per provider and exact model ID (may contain `/` and `:`) |
| `reasoning` | provider value (`none`, `low`, `off`, `on`, a level, …) or `"default"` = send no parameter; absent = not set |
| `max_output_tokens` | positive integer, or `null` = explicitly "Default"; absent = not set |

Unknown fields are ignored. Writers sort providers and models by name and
omit empty entries. Invalid individual values are skipped with a warning;
an unreadable or invalid file is reported, defaults are used, and the file
is **never overwritten** in that session (changes show
`Settings not saved: <path> could not be read at startup.`).

### Effective values

For the active provider/model, each setting is resolved in this order:

1. **CLI flag** (`--reasoning-effort`, `--max-output-tokens`,
   `--answer-style`) for this invocation only; never saved. A value the model
   is known not to support is an error at startup (exit code 2). After the
   user changes a setting interactively, that choice replaces the flag.
2. **Saved preference** for the provider/model (answer style is global). A
   saved value the model does not support (or no longer supports) is
   ignored with a visible note, e.g.
   `Saved reasoning 'High' is not available for m (m supports reasoning: Off, On); using Off.`
   or `Saved setting ignored: 5000 exceeds the limit of 4096 output tokens for m. Using the default.`
3. **Built-in default:** reasoning `none` where the model's confirmed efforts
   include it (e.g. `gpt-6-luna`), Ollama `off` for thinking models,
   otherwise no parameter; max tokens: model default; style: `normal`.

CLI effort names apply to Ollama too: `none` → `off`, any other effort → `on`
on On/Off models, or the same-named level. Explicit values for OpenAI
models with unknown support are passed through (the API may reject them,
which is reported). Notes are printed as `neurobrau: note: …` (one-shot)
or `warning: …` (plain chat), or shown as entries in the TUI.

Changing F4/F5 saves `reasoning` / `max_output_tokens` for the current
provider/model; F6 saves `answer_style`.

## 14. Streaming (`--stream`)

The shared engine below is used by one-shot, plain chat and the fullscreen
UI (subsection "In the fullscreen UI").

### Events

A provider stream yields, in order:

| Event | Fields | Meaning |
|---|---|---|
| `TextDelta` | `text` | a piece of the answer (empty pieces are dropped) |
| `ReasoningDelta` | `text` | a piece of thinking; never printed or saved as the answer |
| `StreamCompleted` | `result` (`text`, `usage`, `reasoning`), `metadata` | finished; `text` is the concatenated deltas |
| `StreamIncomplete` | `reason`, `partial_text`, `usage` | stopped early (token limit, content filter) |
| `StreamFailed` | `error` (with kind/exit code), `partial_text` | request or stream failed |

Exactly one of the last three ends every stream; failures are events, not
exceptions. Closing the stream early (Ctrl+C, consumer stops) closes the
HTTP connection.

### OpenAI

`responses.create(..., stream=True)` with the same request as
non-streaming (`store=false`, instructions, reasoning, max output tokens).

| SDK event | Result |
|---|---|
| `response.output_text.delta` | `TextDelta(delta)`; a non-string delta → `StreamFailed("Invalid stream event from OpenAI: …")` |
| `response.completed` | `StreamCompleted`; text = deltas (or `response.output_text` if none arrived); usage from `response.usage`; metadata `response_id`, `model` |
| `response.incomplete` | `StreamIncomplete`; reason `max_output_tokens` → `the max output token limit was reached`, `content_filter` → `the content filter stopped the response`, otherwise the raw reason |
| `response.failed` | `StreamFailed("OpenAI response failed: <response.error.message>")` |
| `error` | `StreamFailed("OpenAI stream error: <message>")` |
| anything else | ignored |
| end without a terminal event | `StreamFailed("OpenAI stream ended before the response was complete.")` |

SDK exceptions (before or during the stream) map to the usual error kinds.
The rejected-reasoning rule from section 12 applies when the rejection
arrives before any text.

### Ollama

`POST /api/chat` with `"stream": true` and otherwise the same body as
non-streaming (same model checks; cloud models refused before any request).
The body is newline-delimited JSON, read in raw byte chunks: bytes are
buffered until a newline, so lines and multi-byte UTF-8 characters split
across chunks are decoded intact; blank lines are skipped; the read timeout
(300 s) applies to each read.

| Line | Result |
|---|---|
| `message.thinking` (non-empty) | `ReasoningDelta` |
| `message.content` (non-empty) | `TextDelta`, except a leading `<think>…</think>` block, which becomes reasoning (held back until it is clear whether the text starts such a block) |
| `{"error": "…"}` | `StreamFailed("Ollama error: …")` |
| `"done": true`, `done_reason` `length` | `StreamIncomplete("the max output token limit was reached")` with usage |
| `"done": true`, otherwise | `StreamCompleted`; usage from `prompt_eval_count` / `eval_count`; metadata `model`, `done_reason`, and `total_seconds`, `load_seconds`, `prompt_eval_seconds`, `eval_seconds` (from the `*_duration` nanoseconds) |
| not JSON / not an object / `message.content` not a string | `StreamFailed("Invalid response from Ollama: …")` |
| end without `done` | `StreamFailed("Ollama stream ended before the response was complete.")` (connection kind) |

HTTP errors map as in section 12 (404 → install hint).

### Output

One-shot: deltas are written to stdout and flushed immediately; a final
newline follows any text (flushed before anything goes to stderr).

| Outcome | stderr | Exit |
|---|---|---|
| completed | with `--usage`: `Token usage: …` then, if durations are known, `Duration: <total> s (load <l> s, generation <g> s)` | 0 |
| incomplete | `neurobrau: warning: response incomplete: <reason>.` (+ usage with `--usage`) | 8 |
| failed | `neurobrau: error: <message>` | error kind code |
| Ctrl+C | `neurobrau: interrupted` | 130 |

Plain chat: `AI > ` is printed with the first piece of text (an empty
completed answer still prints it), then the text as it arrives. Incomplete:
`warning: Response incomplete: <reason>. The partial response was not saved.`;
failed: `error: <message>` plus the same suffix when text had been shown;
Ctrl+C: `Request cancelled. The partial response was not saved.` and the
chat continues.

### Conversation state

The user/assistant pair is committed, and the conversation saved, only after
`StreamCompleted`, using the assembled text. Incomplete, failed and
interrupted answers leave the conversation and its file unchanged, and are
not sent as context later.

### In the fullscreen UI

With `--stream`, a message is sent like this:

1. The user entry is added and the view jumps to the bottom. The request is
   built on the UI thread from the committed history plus the new message.
2. A **worker thread** iterates the provider stream and puts
   `(request id, event)` pairs on a **bounded queue** (64 items; when full
   the worker waits, re-checking cancellation every 0.1 s). The worker never
   touches the screen.
3. The **UI thread** owns all curses calls. While a request runs it reads
   keys with a 100 ms timeout; each loop iteration applies up to 256 queued
   events, then redraws once. Typing, editing, scrolling and resizing keep
   working during the request.
4. `TextDelta`: the first one creates the AI entry; later ones extend it,
   re-wrapped with the normal rules. `ReasoningDelta` only switches the
   status to `Thinking`; the text is never shown.
5. **Scrolling:** at the bottom (scroll 0) the view follows new text. When the
   user has scrolled up, the scroll offset grows by the number of lines the
   update added, so the visible lines do not move.
6. `StreamCompleted`: the AI entry gets the final assembled text, the
   user/assistant pair is committed and saved (section 11), usage is added
   to the session totals, and the status shows `DONE` with metrics.
7. `StreamIncomplete`: error entry `Incomplete: <reason>.` and
   `WARNING | Response incomplete: <reason>`. `StreamFailed`: error entry
   `Error: <message>` and `ERROR | <message>`. In both cases the entry ends
   with ` The partial answer above was not saved.` if text had been shown;
   nothing is committed or saved.

While a request runs, only one exists: Enter shows the flashed warning
`WARNING | A response is being generated; wait or press Ctrl+C` and keeps
the typed text; F2–F6 show `WARNING | Settings cannot change while a
response is generated`; Ctrl+D does nothing. `/exit` cancels and exits;
`/help` works.

**Cancellation (Ctrl+C while a request runs).** The UI immediately stops
showing the request: its id is retired so later events are dropped, the
error entry `Cancelled.` (plus ` The partial answer above was not saved.`
if text was shown) is added, and the status shows `CANCELLED`. The partial
text stays visible but is never committed or saved. The worker is told to
stop; it notices after the next event arrives (or its read times out),
closes the stream, which closes the HTTP connection, and exits. The SIGINT
handler only records the request while a stream runs, so the cancellation
happens at a safe point in the loop. When no stream is running, Ctrl+C
behaves as before (exit, or interrupt a blocking call).

**Limitation:** a read that is blocked waiting for the network cannot be
interrupted from another thread. A cancelled stream closes when the next
data arrives or the provider's read timeout expires (Ollama: 300 s). Until
then a new message waits up to 1 s and, if the old stream is still open,
is refused with `WARNING | The cancelled response is still closing; try
again shortly` (the typed text is kept). On exit the app waits at most
0.5 s per stream; worker threads are daemons and never block exit. The
terminal is restored by `curses.wrapper` on every exit path.
