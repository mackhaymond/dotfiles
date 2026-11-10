# AgentMarks

A tiny monochrome font of logo marks, one per agent harness, so the tmux agent
sidebar can print each agent's harness as a glyph in its brand colour (colour
comes from the terminal foreground; the outlines are plain monochrome). Every
mark comes in three widths: one cell, and two- and three-cell forms reached
through ligatures (see "Sequences" below).

WezTerm picks it up as the last entry of `font_with_fallback` in
`dot_config/wezterm/wezterm.lua.tmpl`. The codepoints are in Supplementary
Private Use Area-B (plane 16), so no Nerd Font glyph can collide with them.

## Codepoints

The sidebar codes against these. Append new marks at the next free codepoint;
never renumber or reuse one. The id (the glyph name too) is the harness string
the sidebar should map to the mark, e.g. as the window's `@agent_kind`.

| Codepoint | id         | Harness (process)              | Mark                                      | Source                                  | Licence |
|-----------|------------|--------------------------------|-------------------------------------------|-----------------------------------------|---------|
| U+10C100  | `claude`   | Claude Code (`claude`)         | Anthropic's Claude spark                  | Simple Icons `claude`                   | CC0     |
| U+10C101  | `codex`    | Codex CLI (`codex`)            | OpenAI knot                               | lobe-icons `openai`                     | MIT     |
| U+10C102  | `opencode` | OpenCode (`opencode`)          | OpenCode block frame                      | Simple Icons `opencode`                 | CC0     |
| U+10C103  | `grok`     | Grok CLI (`grok`)              | Grok ring and slash                       | lobe-icons `grok`                       | MIT     |
| U+10C104  | `abby`     | abby, abliteration.ai          | three bars, one upright and two slanted   | abliteration.ai `icon.svg`              | vendor  |
| U+10C105  | `agent`    | any other / unknown harness    | generic robot head                        | drawn for this repo                     | public domain |
| U+10C106  | `gemini`   | Gemini CLI (`gemini`)          | Gemini sparkle                            | Simple Icons `googlegemini`             | CC0     |
| U+10C107  | `cursor`   | Cursor CLI (`cursor-agent`)    | Cursor cube                               | Simple Icons `cursor`                   | CC0     |
| U+10C108  | `copilot`  | GitHub Copilot CLI (`copilot`) | Copilot goggles                           | Simple Icons `githubcopilot`            | CC0     |
| U+10C109  | `amp`      | Amp, Sourcegraph (`amp`)       | Amp chevrons                              | lobe-icons `amp`                        | MIT     |
| U+10C10A  | `cline`    | Cline (`cline`)                | Cline robot head                          | Simple Icons `cline`                    | CC0     |
| U+10C10B  | `roo`      | Roo Code                       | Roo kangaroo                              | lobe-icons `roocode`                    | MIT     |
| U+10C10C  | `kilo`     | Kilo Code (`kilocode`)         | Kilo boxed "KILO"                         | lobe-icons `kilocode`                   | MIT     |
| U+10C10D  | `goose`    | Goose, Block (`goose`)         | Goose in flight                           | lobe-icons `goose`                      | MIT     |
| U+10C10E  | `qwen`     | Qwen Code (`qwen`)             | Qwen hexagram                             | Simple Icons `qwen`                     | CC0     |
| U+10C10F  | `kimi`     | Kimi CLI, Moonshot (`kimi`)    | Kimi "K" with dot                         | Simple Icons `kimi`                     | CC0     |
| U+10C110  | `warp`     | Warp (agent mode)              | Warp double panel                         | Simple Icons `warp`                     | CC0     |
| U+10C111  | `windsurf` | Windsurf                       | Windsurf wave "W"                         | Simple Icons `windsurf`                 | CC0     |
| U+10C112  | `droid`    | Factory Droid (`droid`)        | Factory pinwheel flower                   | factory.ai `favicon.svg`                | vendor  |
| U+10C113  | `devin`    | Devin, Cognition               | Devin hexagon cluster                     | lobe-icons `devin`                      | MIT     |
| U+10C114  | `auggie`   | Augment Code CLI (`auggie`)    | Augment `{ : }` face                      | augmentcode.com `favicon.svg`           | vendor  |
| U+10C115  | `vibe`     | Mistral Vibe (`vibe`)          | Mistral pixel "M"                         | Simple Icons `mistralai`                | CC0     |
| U+10C116  | `deepseek` | DeepSeek                       | DeepSeek whale                            | Simple Icons `deepseek`                 | CC0     |
| U+10C117  | `ollama`   | Ollama (`ollama`)              | Ollama llama                              | Simple Icons `ollama`                   | CC0     |
| U+10C1FF  | `cont`     | (continuation)                 | blank, one cell; only used in sequences   | none                                    |         |

The process names are best-effort detection hints for harnesses that ship a terminal CLI; verify against `ps` before relying on one. They are not part of the font. Simple Icons
is pinned at 16.36.0 and lobe-icons (`@lobehub/icons-static-svg`) at 1.100.0;
the "vendor" marks are the vendor's own site icon. Each `svg/<id>.svg` says
where it came from.

Not included, for want of a clean vector mark: Aider (its logo is the word
"aider" set in a font), Crush by Charm (no Crush mark as SVG; Charm's site
icon is a raster picture). Use `agent` for them.

In a string: `"\u{10C100}"` (Rust/JS), `"\U0010C100"` (Python),
`utf8.char(0x10C100)` (Lua). Each is one UTF-8 4-byte sequence and one
terminal column (plane-16 PUA has ambiguous East Asian width, i.e. narrow;
macOS `wcwidth`, which tmux uses, returns 1).

## Sequences: one, two or three cells

| Print                          | Columns | Glyph (shaped)  | Logo                                     |
|--------------------------------|---------|-----------------|------------------------------------------|
| `<mark>`                       | 1       | `<id>`          | about cap height, on the text baseline   |
| `<mark> U+10C1FF`              | 2       | `<id>.mid`      | full row height, at most 1100 units wide |
| `<mark> U+10C1FF U+10C1FF`     | 3       | `<id>.wide`     | full row height, at most 1200 units wide (the sidebar's ` k ` key-cap width) |

`<mark>` is any codepoint in the table above except U+10C1FF. The two- and
three-cell forms are GSUB ligatures (features `liga` and `calt`, scripts
`DFLT` and `latn`); each is ONE glyph whose advance is 1200 or 1800 units (2
or 3 cells). "Full row height" is y -260..980 of the -300..1020 line: a
40-unit margin so the logo never touches the next row. Aspect is kept and the
logo is centred in its cells.

- **The longest match wins.** In each ligature set the three-cell rule comes
  first, so `<mark> cont cont` is always `.wide`, never `.mid` + a stray cont.
  A third cont after that is just a blank cell.
- **The column count never depends on shaping.** Unshaped (ligatures off, or
  the characters split across runs), a sequence degrades to the one-cell mark
  plus blank cells: still 1, 2 or 3 columns.
- **Same attributes for the whole sequence.** WezTerm shapes each attribute
  run (colour, bold, ...) separately, so a colour change inside a sequence
  breaks the ligature.

Checked with WezTerm 20260117 `wezterm ls-fonts --text` (a shaped cluster
reports its `cells`): for every mark, `mark cont cont` is one `.wide` cluster
with `cells=3` and `mark cont` one `.mid` cluster with `cells=2`. Also
`mark cont` followed by a space gives `.mid` then `space`; `mark cont mark cont`
gives two `.mid`s; `mark cont cont cont` gives `.wide` then `cont`; and
`x` + a three-cell sequence + ` ti` gives `x`, `.wide`, then the text.

## Layout

| Path            | What                                                         |
|-----------------|--------------------------------------------------------------|
| `svg/*.svg`     | cleaned sources: only path data, one mark per file           |
| `build.py`      | SVGs -> `AgentMarks.ttf` (fontTools only, deterministic)     |
| `proof.py`      | PNG proof the way WezTerm lays it out (Pillow with raqm)     |
| `AgentMarks.ttf`| the built font, committed so `chezmoi apply` needs no Python |

This directory is in `.chezmoiignore`, so none of it lands in `~/.config`.
`.chezmoiscripts/run_onchange_before_install-agent-marks-font.sh.tmpl` copies
`AgentMarks.ttf` to `~/.local/share/wezterm/fonts/AgentMarks.ttf` (listed in
wezterm.lua's `config.font_dirs`) and re-runs whenever the committed font's
hash changes. WezTerm re-reads `font_dirs` on every config reload, so a
rebuilt font shows up without restarting WezTerm.

## Rebuilding / adding a mark

```sh
cd ~/.local/share/chezmoi/dot_config/wezterm/agent-marks
# add svg/<id>.svg (path data only) and a row to MARKS in build.py at the next
# free codepoint; add it to the table above with its source and licence
python3 build.py --verify     # rebuilds AgentMarks.ttf; checks the cmap, every
                              # form's advance and bounds, and the GSUB rules
python3 proof.py -o /tmp/agent-marks-proof.png   # look at it
chezmoi apply                 # installs the new font
wezterm ls-fonts --text $'\U10C100\U10C1FF\U10C1FF'   # one *.wide glyph, cells=3
```

Metrics match JetBrains Mono 2.x (1000 upm, 600 advance, ascent 1020, descent
-300, cap height 730): every one-cell mark is exactly one cell wide and sits
on the text baseline. A one-cell mark is scaled to fit a 560 x 680 unit box
(20-unit side bearings), centred on the middle of the cap height; `.mid`
fits 1100 x 1240 and `.wide` 1200 x 1240, centred in their advance and the
line. `MARKS` has a per-mark optical scale used by all three forms. Contour
direction is recomputed from nesting depth, so sources may use either fill
rule, but contours must not cross each other.

## Licences and attribution

The logos are trademarks of their owners and are used here only to identify
which product an agent runs on; this font is for personal use in this
dotfiles setup and is not affiliated with or endorsed by any of them.

- [Simple Icons](https://simpleicons.org) 16.36.0, CC0-1.0 path data:
  `claude`, `opencode`, `gemini` (`googlegemini`), `cursor`, `copilot`
  (`githubcopilot`), `cline`, `qwen`, `kimi`, `warp`, `windsurf`, `vibe`
  (`mistralai`), `deepseek`, `ollama`. Simple Icons no longer carries OpenAI,
  xAI or Grok. The OpenCode frame matches opencode.ai's `favicon-v3.svg`,
  whose grey inner fill is dropped for a monochrome glyph.
- [@lobehub/icons-static-svg](https://github.com/lobehub/lobe-icons) 1.100.0,
  MIT, (c) LobeHub: `codex` (`openai`), `grok`, `amp`, `roo` (`roocode`),
  `kilo` (`kilocode`), `goose`, `devin`.
- The vendor's own site icon: `abby` (`https://abliteration.ai/icon.svg`, the
  three bars without the background square), `droid`
  (`https://factory.ai/favicon.svg`, the light mark without the black
  square), `auggie` (`https://augmentcode.com/favicon.svg`, both paths, its
  colour style dropped).
- `agent.svg`: drawn for this repo; public domain.
- `build.py`, `proof.py`: same licence as this repository.
