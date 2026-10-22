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

The API key is checked **before** entering fullscreen mode; a missing key
exits with code 3 without touching the terminal.

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

Right-aligned state indicator: `[READY]`, or `[WAITING FOR RESPONSE...]`
while a request is in flight. The left text is truncated so it never
overlaps the indicator.

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
   | `/reset`, `\reset` | clear conversation history **and** the panel, reset token usage, show "Conversation history cleared." |
   | `/exit`, `\exit` | exit with code 0 |
   | anything else | error entry: `Unknown command: /<name>. Type /help for commands.` |

   Command names are case-insensitive.
3. Otherwise the text is a message:
   1. Append it to the panel as a user entry.
   2. Show `[WAITING FOR RESPONSE...]` and redraw **before** the blocking
      request starts.
   3. Send the committed history plus the new user message.
   4. On success: commit the user/assistant pair to history, append the
      reply as an AI entry, update token usage.
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

## 10. Porting notes

* Keep the pure logic (layout, wrapping, scrolling, key handling) separate
  from drawing so it can be tested with a fake screen.
* Use one redraw-everything function per event; terminal libraries diff the
  output.
* Python uses `textwrap`; ports must reproduce its behavior: greedy word
  wrap, collapse nothing inside a line, break over-long words at the width.
* Character widths are counted as one column per code point; wide (CJK,
  emoji) characters are a known limitation.
