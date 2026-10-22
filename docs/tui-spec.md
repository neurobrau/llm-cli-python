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
only the conversation panel stretches vertically. Exact render at the
baseline (input empty):

```
row  0  +-[ NeuroBräu CLI 0.1.0 ]------------------------------------------------------+
row  1  | YOU > What is a neural network?                                              |
row  2  |                                                                              |
row  3  | AI > A neural network is a model made of layers of connected units that      |
row  4  |      learn weights from data.                                                |
row  5  |                                                                              |
 ...    |                                                                              |
row 18  |                                                                              |
row 19  +-[ Status ]-------------------------------------------------------------------+
row 20  | Provider: openai  Model: gpt-6-luna  Tokens: in 12 out 30 total 42   [READY] |
row 21  +-[ Input ]--------------------------------------------------------------------+
row 22  | YOU >                                                                        |
row 23  +------------------------------------------------------------------------------+
row 24   /help  /reset  /exit | PgUp/PgDn Up/Down: scroll | Ctrl+C: quit
```

For a terminal of `R` rows and `C` columns:

| Region | Rows | Notes |
|---|---|---|
| Title border | `0` | `+-[ NeuroBräu CLI <version> ]---…---+` |
| Conversation panel | `1` … `R-7` | height `R-7` (18 at baseline) |
| Status separator | `R-6` | `+-[ Status ]---…---+` |
| Status panel | `R-5` | provider, model, tokens, state indicator |
| Input separator | `R-4` | `+-[ Input ]---…---+` |
| Input line | `R-3` | `YOU > ` label + editable text |
| Bottom border | `R-2` | `+---…---+` |
| Help line | `R-1` | key hints, no border, truncated to `C-1` columns |

* Borders use plain ASCII: `+` corners, `-` horizontal, `|` vertical.
  Separator titles are written as `-[ Title ]` immediately after the left `+`.
* Content columns run from `2` to `C-3` (one space padding inside each
  border), so the usable content width is `C-4` (76 at baseline).
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
| waiting | yellow | bold | `[WAITING FOR RESPONSE...]` indicator |

Monochrome terminals: bold for border/user/ai/error, reverse video for the
waiting indicator, normal for everything else.

No animation, mouse support, images or other decoration.

## 4. Conversation panel

* Entries are shown in chronological order with **one blank line between
  entries**.
* User entries start with `YOU > `, assistant entries with `AI > `
  (label in its role color, text in the text color).
* Text is word-wrapped to `C-4 - len(label)` columns. Continuation lines are
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
  `[ NeuroBräu CLI 0.1.0 - scrolled up N (PgDn for newer) ]`.

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

Other keys are ignored.

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
| `messages` | array | chronological; each `{"role": "user" \| "assistant", "content": string}` |

Readers must accept any RFC 3339 offset (e.g. `+02:00`) and ignore unknown
fields at the top level and in messages, so newer writers stay compatible.
Messages are provider-independent text; no model, token or provider data is
stored.

### Behavior

* **Restore:** at interactive startup, load every `*.json` file and continue
  the valid one with the greatest `updated_at` (ties: newest file
  modification time). No valid file means a new conversation, which is not
  written until its first successful exchange.
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
