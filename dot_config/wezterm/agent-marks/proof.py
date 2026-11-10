#!/usr/bin/env python3
"""Render a PNG proof of AgentMarks.ttf the way WezTerm lays it out.

    python3 proof.py [AgentMarks.ttf] [-o proof.png]

Needs Pillow built with raqm (so the GSUB ligatures are really shaped, as
WezTerm's HarfBuzz does) and an installed JetBrains Mono. White on the sidebar
canvas (#181825):

1. every mark in all three forms at 36 px/em, each inside a faint outline of
   its cells: the bare codepoint (1 cell), <mark> U+10C1FF (2 cells) and
   <mark> U+10C1FF U+10C1FF (3 cells);
2. sidebar-like rows at 12 pt @2x (24 px/em), stacked at the real line height:
   a ` k ` key-cap (letter on a tinted three-cell cap; the tint approximates
   the sidebar's), the 3-cell mark, its id; then the 2-cell and 1-cell forms;
3. shaping checks on a cell grid: text around a 3-cell sequence, a 2-cell
   sequence before a space, two 2-cell sequences back to back, and one-cell
   marks mixed with text (baseline and size).
"""

import argparse
import os
import sys

from PIL import Image, ImageDraw, ImageFont, features

sys.dont_write_bytecode = True  # no __pycache__ in the chezmoi source tree
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build import ADVANCE, ASCENT, CONT_CP, DESCENT, MARKS, UPM  # noqa: E402

BG, FG, DIM, GRID = "#181825", "#ffffff", "#9399b2", "#45475a"
CAP_BG, CAP_FG = "#3a2f52", "#cba6f7"  # mauve key-cap, roughly as the sidebar tints it
CONT = chr(CONT_CP)


def seq(cp, cells):
    """The string that shows mark `cp` across `cells` (1, 2 or 3) cells."""
    return chr(cp) + CONT * (cells - 1)


def find_jbm():
    for d in ("~/Library/Fonts", "/Library/Fonts"):
        p = os.path.join(os.path.expanduser(d), "JetBrainsMono[wght].ttf")
        if os.path.exists(p):
            return p
    sys.exit("JetBrains Mono not found")


def cell(px):
    """Cell (width, height, baseline-from-top) in pixels for a px/em size."""
    return ADVANCE * px / UPM, (ASCENT - DESCENT) * px / UPM, ASCENT * px / UPM


class Fonts:
    def __init__(self, jbm_path, marks_path):
        self.jbm_path, self.marks_path, self.cache = jbm_path, marks_path, {}

    def get(self, px):
        if px not in self.cache:
            eng = ImageFont.Layout.RAQM
            self.cache[px] = (ImageFont.truetype(self.jbm_path, px, layout_engine=eng),
                              ImageFont.truetype(self.marks_path, px, layout_engine=eng))
        return self.cache[px]


def put(draw, x, y, runs, px, fonts, grid=False):
    """Draw runs from cell position (x, y); returns the next x.

    runs: [(text, kind)]. kind "text": JetBrains Mono, one char per cell.
    kind N (an int): one shaped AgentMarks run that covers N cells.
    """
    jbm, marks = fonts.get(px)
    cw, ch, base = cell(px)
    for s, kind in runs:
        if isinstance(kind, int):
            if grid:
                draw.rectangle([x, y, x + kind * cw - 1, y + ch - 1], outline=GRID)
            draw.text((x, y + base), s, font=marks, fill=FG, anchor="ls", features=["liga"])
            x += kind * cw
            continue
        for c in s:
            if grid:
                draw.rectangle([x, y, x + cw - 1, y + ch - 1], outline=GRID)
            draw.text((x, y + base), c, font=jbm, fill=FG, anchor="ls")
            x += cw
    return x


def key_cap(draw, x, y, letter, px, fonts):
    jbm, _ = fonts.get(px)
    cw, ch, base = cell(px)
    draw.rectangle([x, y, x + 3 * cw - 1, y + ch - 1], fill=CAP_BG)
    draw.text((x + cw, y + base), letter, font=jbm, fill=CAP_FG, anchor="ls")
    return x + 3 * cw


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("font", nargs="?", default=os.path.join(here, "AgentMarks.ttf"))
    ap.add_argument("-o", "--output", default="agent-marks-proof.png")
    a = ap.parse_args()
    if not features.check("raqm"):
        sys.exit("Pillow lacks raqm: the ligature forms would not be shaped")
    fonts = Fonts(find_jbm(), a.font)
    label = ImageFont.truetype(fonts.jbm_path, 18)

    half = (len(MARKS) + 1) // 2
    G = 36                                  # px/em of the form grid
    _, gch, _ = cell(G)
    row_g = int(gch) + 10
    S = 24                                  # px/em of the sidebar rows
    scw, sch, _ = cell(S)
    W = 1100
    H = 60 + row_g * half + 30 + int(sch) * half + 30 + 3 * (int(cell(24)[1]) + 12) + 20
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # 1. Form grid, two columns of marks.
    colx = [24, 24 + W // 2]
    for cx in colx:
        d.text((cx + 230, 24), "1 cell  2 cells   3 cells", font=label, fill=DIM)
    y0 = 60
    for i, (cp, name, _, _) in enumerate(MARKS):
        cx, y = colx[i // half], y0 + (i % half) * row_g
        d.text((cx, y + gch / 2), f"U+{cp:X} {name}", font=label, fill=FG, anchor="lm")
        x = cx + 230
        for cells in (1, 2, 3):
            x = put(d, x, y, [(seq(cp, cells), cells)], G, fonts, grid=True) + 22

    # 2. Sidebar-like rows: cap + 3-cell + id, then the 2-cell and 1-cell forms.
    y = y0 + row_g * half + 30
    letters = "abcdefghijklmnopqrstuvwxyz"
    for i, (cp, name, _, _) in enumerate(MARKS):
        cx, yy = colx[i // half], y + (i % half) * int(sch)
        x = key_cap(d, cx, yy, letters[i % 26], S, fonts)
        x = put(d, x + scw, yy, [(seq(cp, 3), 3)], S, fonts)
        x = put(d, x + scw, yy, [(f"{name:9s}", "text")], S, fonts)
        x = put(d, x, yy, [(seq(cp, 2), 2)], S, fonts)
        put(d, x + scw, yy, [(seq(cp, 1), 1)], S, fonts)
    y += int(sch) * half + 30

    # 3. Shaping checks on a cell grid.
    cps = [cp for cp, *_ in MARKS]
    checks = [
        [("x", "text"), (seq(cps[0], 3), 3), (" title", "text")],
        [(seq(cps[1], 2), 2), (" ", "text"), (seq(cps[2], 2), 2), (seq(cps[3], 2), 2),
         (" ", "text"), (seq(cps[4], 3), 3), (seq(cps[5], 3), 3), ("|", "text")],
        [("HxgÉ|", "text")] + [(seq(cp, 1), 1) for cp in cps] + [("|gxH", "text")],
    ]
    for runs in checks:
        put(d, 24, y, runs, 24, fonts, grid=True)
        y += int(cell(24)[1]) + 12

    img.save(a.output)
    print("wrote", a.output)


if __name__ == "__main__":
    main()
