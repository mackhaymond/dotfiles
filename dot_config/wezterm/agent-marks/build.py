#!/usr/bin/env python3
"""Build AgentMarks.ttf: agent-harness logos for the tmux agent sidebar.

    python3 build.py              # writes AgentMarks.ttf next to this file
    python3 build.py -o out.ttf   # somewhere else
    python3 build.py --verify     # also check cmap/metrics/GSUB (against
                                  # JetBrains Mono too, when it is installed)

Needs only fontTools. Reads svg/<name>.svg (every <path d> in the file is part
of the mark; per-path fill-rule="evenodd" is honoured) and fits each mark
three times: into one JetBrains Mono cell (the bare codepoint), and into a
two- and a three-cell glyph that the sequences <mark> U+10C1FF and
<mark> U+10C1FF U+10C1FF ligate to (GSUB liga + calt). Writes a monochrome
TrueType font. The build is deterministic (fixed head timestamps), so an
unchanged source rebuilds to the same bytes. See README.md for the codepoint
table, the sequence contract and attribution.
"""

import argparse
import os
import re
import sys

from fontTools.fontBuilder import FontBuilder
from fontTools.misc.timeTools import timestampFromString
from fontTools.pens.areaPen import AreaPen
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.reverseContourPen import ReverseContourPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.svgLib.path import parse_path
from fontTools.ttLib.tables.O_S_2f_2 import Panose

HERE = os.path.dirname(os.path.abspath(__file__))

# Codepoints are an interface: the sidebar (agent-ui) prints these. Append new
# marks at the next free codepoint; never renumber or reuse one. The glyph name
# is also the harness id the sidebar uses (README.md).
MARKS = [  # (codepoint, glyph name, svg file stem, optical scale)
    (0x10C100, "claude", "claude", 1.00),
    (0x10C101, "codex", "codex", 1.00),
    (0x10C102, "opencode", "opencode", 0.92),
    (0x10C103, "grok", "grok", 1.00),
    (0x10C104, "abby", "abby", 1.00),
    (0x10C105, "agent", "agent", 1.00),
    (0x10C106, "gemini", "gemini", 1.00),
    (0x10C107, "cursor", "cursor", 1.00),
    (0x10C108, "copilot", "copilot", 1.00),
    (0x10C109, "amp", "amp", 1.00),
    (0x10C10A, "cline", "cline", 1.00),
    (0x10C10B, "roo", "roo", 1.00),
    (0x10C10C, "kilo", "kilo", 1.00),
    (0x10C10D, "goose", "goose", 1.00),
    (0x10C10E, "qwen", "qwen", 1.00),
    (0x10C10F, "kimi", "kimi", 1.00),
    (0x10C110, "warp", "warp", 1.00),
    (0x10C111, "windsurf", "windsurf", 1.00),
    (0x10C112, "droid", "droid", 1.00),
    (0x10C113, "devin", "devin", 1.00),
    (0x10C114, "auggie", "auggie", 1.00),
    (0x10C115, "vibe", "vibe", 1.00),
    (0x10C116, "deepseek", "deepseek", 1.00),
    (0x10C117, "ollama", "ollama", 1.00),
]

# JetBrains Mono 2.x metrics (head.unitsPerEm, hmtx advance, hhea/OS2 typo
# ascender/descender, OS/2 cap height). Matching them makes WezTerm treat each
# mark as exactly one cell, sitting on the same baseline as the text.
UPM = 1000
ADVANCE = 600
ASCENT = 1020
DESCENT = -300
CAP_HEIGHT = 730
X_HEIGHT = 550

# The box a one-cell mark is fitted into (font units, y up): the cell width
# minus a side bearing, about cap height tall, centred on the middle of the caps.
SIDE_BEARING = 20
BOX_W = ADVANCE - 2 * SIDE_BEARING   # 560
BOX_H = 680
CENTER_Y = CAP_HEIGHT / 2           # 365

# Multi-cell forms, through GSUB ligatures (liga + calt, DFLT + latn):
#   <mark> CONT CONT -> "<name>.wide": one glyph three cells wide (the sidebar's
#                       ` k ` key-cap width), logo at most two cells wide;
#   <mark> CONT      -> "<name>.mid": one glyph two cells wide, logo at most
#                       1100 units wide.
# The longer rule comes first in each ligature set, so it wins when both match.
# Both take most of the row height (descender to ascender minus a margin top
# and bottom, so the logo never touches the next row and reads a little
# lighter than the text: Mack asked for "slightly shorter", 2026-11-10, margin
# 40 -> 130), aspect kept, centred vertically on the row and horizontally in
# their cells.
CONT_CP = 0x10C1FF   # "AgentMarks continuation": blank, one cell
CONT_NAME = "cont"
MID_CELLS, WIDE_CELLS = 2, 3
MID_ADVANCE = MID_CELLS * ADVANCE    # 1200
WIDE_ADVANCE = WIDE_CELLS * ADVANCE  # 1800
MID_BOX_W = MID_ADVANCE - 100        # 1100
WIDE_BOX_W = 2 * ADVANCE             # 1200
WIDE_MARGIN_Y = 130
WIDE_BOX_H = (ASCENT - DESCENT) - 2 * WIDE_MARGIN_Y  # 1060 (.mid and .wide)
WIDE_CENTER_Y = (ASCENT + DESCENT) / 2               # 360

# Glyph-name suffix -> (cells, advance, logo box w, box h, centre y).
FORMS = {
    "": (1, ADVANCE, BOX_W, BOX_H, CENTER_Y),
    ".mid": (MID_CELLS, MID_ADVANCE, MID_BOX_W, WIDE_BOX_H, WIDE_CENTER_Y),
    ".wide": (WIDE_CELLS, WIDE_ADVANCE, WIDE_BOX_W, WIDE_BOX_H, WIDE_CENTER_Y),
}

CU2QU_MAX_ERR = 1.0  # font units


def read_svg(path):
    """Return (viewBox tuple, [(d, evenodd?)]) from a cleaned mark SVG."""
    text = open(path, encoding="utf-8").read()
    vb = tuple(float(v) for v in re.search(r'viewBox="([^"]+)"', text).group(1).split())
    paths = []
    for m in re.finditer(r"<path\b([^>]*)/?>", text):
        attrs = m.group(1)
        d = re.search(r'\sd="([^"]+)"', attrs).group(1)
        rule = re.search(r'fill-rule="([^"]+)"', attrs)
        paths.append((d, bool(rule and rule.group(1) == "evenodd")))
    if not paths:
        sys.exit(f"{path}: no <path d> found")
    return vb, paths


def split_contours(recording):
    """RecordingPen ops -> list of contours (each a list of ops, closed)."""
    contours, cur = [], []
    for op, args in recording:
        if op == "moveTo" and cur:
            contours.append(cur)
            cur = []
        if op in ("closePath", "endPath"):
            # SVG fills open subpaths as if closed; so do we.
            cur.append(("closePath", ()))
            contours.append(cur)
            cur = []
            continue
        cur.append((op, args))
    if cur:
        cur.append(("closePath", ()))
        contours.append(cur)
    return [c for c in contours if len(c) > 2]


def replay(ops, pen):
    for op, args in ops:
        getattr(pen, op)(*args)


def flatten(ops, steps=16):
    """Polygon approximation of a contour (for point-in-polygon tests)."""
    pts, cur = [], None
    for op, args in ops:
        if op == "moveTo" or op == "lineTo":
            cur = args[0]
            pts.append(cur)
        elif op == "curveTo":
            p0, (p1, p2, p3) = cur, args
            for i in range(1, steps + 1):
                t = i / steps
                mt = 1 - t
                pts.append((
                    mt**3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t**3 * p3[0],
                    mt**3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t**3 * p3[1],
                ))
            cur = p3
        elif op == "qCurveTo":
            p0, (p1, p2) = cur, args[-2:]  # svgLib emits single-offcurve quads
            for i in range(1, steps + 1):
                t = i / steps
                mt = 1 - t
                pts.append((
                    mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0],
                    mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1],
                ))
            cur = p2
    return pts


def inside(pt, poly):
    x, y = pt
    hit = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


def signed_area(ops):
    pen = AreaPen()
    replay(ops, pen)
    return pen.value  # > 0: counter-clockwise (y up)


def orient(contours):
    """Return contours oriented for TrueType's nonzero fill.

    A contour nested inside an even number of others is ink (clockwise), odd is
    a hole (counter-clockwise). That reproduces the even-odd look of the sources
    and is equally right for the nonzero ones, provided contours do not cross
    (true for every mark here; svg/agent.svg is drawn to keep it so).
    """
    polys = [flatten(c) for c in contours]
    out = []
    for i, c in enumerate(contours):
        probe = polys[i][len(polys[i]) // 2]
        depth = sum(1 for j, p in enumerate(polys) if j != i and inside(probe, p))
        want_cw = depth % 2 == 0
        is_cw = signed_area(c) < 0
        if want_cw != is_cw:
            rec = RecordingPen()
            replay(c, ReverseContourPen(rec))
            c = rec.value
        out.append(c)
    return out


def build_glyph(svg_path, optical_scale, box_w, box_h, center_x, center_y):
    """Fit the SVG's ink into box_w x box_h (aspect kept) centred on center_x/y."""
    vb, paths = read_svg(svg_path)
    vx, vy, vw, vh = vb
    raw = RecordingPen()
    for d, _evenodd in paths:
        # Flip y (SVG is y-down) into a provisional y-up space.
        parse_path(d, TransformPen(raw, (1, 0, 0, -1, 0, vy + vh)))
    bp = BoundsPen(None)
    raw.replay(bp)
    xmin, ymin, xmax, ymax = bp.bounds
    w, h = xmax - xmin, ymax - ymin
    s = min(box_w / w, box_h / h) * optical_scale
    dx = center_x - s * (xmin + xmax) / 2
    dy = center_y - s * (ymin + ymax) / 2
    placed = RecordingPen()
    raw.replay(TransformPen(placed, (s, 0, 0, s, dx, dy)))
    contours = orient(split_contours(placed.value))
    tt = TTGlyphPen(None)
    q = Cu2QuPen(tt, CU2QU_MAX_ERR, reverse_direction=False)
    for c in contours:
        replay(c, q)
    return tt.glyph()


def notdef_glyph():
    pen = TTGlyphPen(None)
    x0, x1, y0, y1, t = 60, ADVANCE - 60, 0, CAP_HEIGHT, 60
    for pts in (
        [(x0, y0), (x0, y1), (x1, y1), (x1, y0)],                    # outer, cw
        [(x0 + t, y0 + t), (x1 - t, y0 + t), (x1 - t, y1 - t), (x0 + t, y1 - t)],  # hole
    ):
        pen.moveTo(pts[0])
        for p in pts[1:]:
            pen.lineTo(p)
        pen.closePath()
    return pen.glyph()


def ligature_rules():
    """[(components, ligature glyph)], per mark the three-cell rule first."""
    rules = []
    for _, n, _, _ in MARKS:
        rules.append(((n, CONT_NAME, CONT_NAME), n + ".wide"))
        rules.append(((n, CONT_NAME), n + ".mid"))
    return rules


def feature_code():
    """GSUB: <mark> cont cont -> .wide, <mark> cont -> .mid; liga + calt, DFLT + latn."""
    rules = "\n".join(f"    sub {' '.join(comp)} by {lig};" for comp, lig in ligature_rules())
    return (
        "languagesystem DFLT dflt;\nlanguagesystem latn dflt;\n\n"
        f"lookup MARKCELLS {{\n{rules}\n}} MARKCELLS;\n\n"
        "feature liga { lookup MARKCELLS; } liga;\n"
        "feature calt { lookup MARKCELLS; } calt;\n"
    )


def build(out_path):
    names = [name for _, name, _, _ in MARKS]
    # Glyph order: the one-cell marks, cont, then each ligature form.
    order = [".notdef"] + names + [CONT_NAME]
    order += [n + sfx for sfx in FORMS if sfx for n in names]
    glyphs = {".notdef": notdef_glyph(), CONT_NAME: TTGlyphPen(None).glyph()}
    advances = {".notdef": ADVANCE, CONT_NAME: ADVANCE}
    for _, name, stem, scale in MARKS:
        svg = os.path.join(HERE, "svg", stem + ".svg")
        for sfx, (_cells, adv, bw, bh, cy) in FORMS.items():
            glyphs[name + sfx] = build_glyph(svg, scale, bw, bh, adv / 2, cy)
            advances[name + sfx] = adv

    fb = FontBuilder(UPM, isTTF=True)
    fb.setupGlyphOrder(order)
    cmap = {cp: name for cp, name, _, _ in MARKS}
    cmap[CONT_CP] = CONT_NAME
    fb.setupCharacterMap(cmap)
    fb.setupGlyf(glyphs)
    metrics = {}
    for name in order:
        g = glyphs[name]
        g.recalcBounds(fb.font["glyf"])
        metrics[name] = (advances[name], getattr(g, "xMin", 0))
    fb.setupHorizontalMetrics(metrics)
    fb.addOpenTypeFeatures(feature_code())
    fb.setupHorizontalHeader(ascent=ASCENT, descent=DESCENT, lineGap=0)
    fb.setupNameTable({
        "familyName": "AgentMarks",
        "styleName": "Regular",
        "uniqueFontIdentifier": "AgentMarks-Regular-1.0",
        "fullName": "AgentMarks Regular",
        "psName": "AgentMarks-Regular",
        "version": "Version 1.000",
        "description": "AI agent harness marks for a terminal sidebar: one cell, or "
                       "two / three cells via the <mark> U+10C1FF [U+10C1FF] ligatures. "
                       "Logos are trademarks of their owners.",
    })
    panose = Panose()
    panose.bFamilyType, panose.bProportion = 2, 9  # Latin text, monospaced
    fb.setupOS2(
        version=4,
        sTypoAscender=ASCENT, sTypoDescender=DESCENT, sTypoLineGap=0,
        usWinAscent=ASCENT, usWinDescent=-DESCENT,
        sCapHeight=CAP_HEIGHT, sxHeight=X_HEIGHT, xAvgCharWidth=ADVANCE,
        fsSelection=0x40 | 0x80,          # REGULAR | USE_TYPO_METRICS
        achVendID="NONE",
        ulUnicodeRange3=1 << (90 - 64),   # bit 90: Private Use (planes 15/16)
        panose=panose,
    )
    fb.setupPost(isFixedPitch=1)
    stamp = timestampFromString("Mon Nov  9 00:00:00 2026")  # fixed: deterministic bytes
    fb.setupHead(fontRevision=1.0, created=stamp, modified=stamp)
    fb.font.recalcTimestamp = False
    fb.save(out_path)
    return out_path


def verify(path):
    from fontTools.ttLib import TTFont

    f = TTFont(path)
    ok = True

    def fail(msg):
        nonlocal ok
        print("FAIL", msg)
        ok = False

    # cmap: exactly the marks + cont.
    want = {cp: name for cp, name, _, _ in MARKS}
    want[CONT_CP] = CONT_NAME
    cmap = f.getBestCmap()
    if cmap != want:
        fail(f"cmap {sorted(hex(c) for c in cmap)} != {sorted(hex(c) for c in want)}")
    if len({cp for cp, *_ in MARKS}) != len(MARKS) or len({n for _, n, _, _ in MARKS}) != len(MARKS):
        fail("duplicate codepoint or name in MARKS")

    # Glyphs: every form present, advance per form, ink inside its cells.
    adv = {n: f["hmtx"][n][0] for n in f.getGlyphOrder()}
    if adv.get(CONT_NAME) != ADVANCE or f["glyf"][CONT_NAME].numberOfContours != 0:
        fail(f"{CONT_NAME} must be blank with advance {ADVANCE}")
    for cp, name, _, _ in MARKS:
        cols = []
        for sfx, (cells, advance, *_rest) in FORMS.items():
            gname = name + sfx
            if gname not in adv:
                fail(f"missing glyph {gname}")
                continue
            g = f["glyf"][gname]
            if adv[gname] != advance:
                fail(f"advance {gname} {adv[gname]} != {advance}")
            if not g.numberOfContours:
                fail(f"{gname} is blank")
                continue
            if g.xMin < 0 or g.xMax > advance or g.yMin < DESCENT or g.yMax > ASCENT:
                fail(f"{gname} outside its {cells} cell(s)")
            cols.append(f"{cells}:{g.xMin}..{g.xMax}x{g.yMin}..{g.yMax}")
        print(f"U+{cp:X} {name:9s} " + "  ".join(cols))
    extra = set(adv) - {".notdef", CONT_NAME} - {n + s for _, n, _, _ in MARKS for s in FORMS}
    if extra:
        fail(f"unexpected glyphs {sorted(extra)}")

    # GSUB: liga + calt under DFLT and latn, exactly the rule set, and in each
    # ligature set the three-cell rule before the two-cell one.
    gsub = f["GSUB"].table
    feats = {fr.FeatureTag: fr.Feature.LookupListIndex for fr in gsub.FeatureList.FeatureRecord}
    scripts = {sr.ScriptTag for sr in gsub.ScriptList.ScriptRecord}
    if not {"liga", "calt"} <= set(feats) or not {"DFLT", "latn"} <= scripts:
        fail(f"GSUB features {sorted(feats)} scripts {sorted(scripts)}")
    ligs = {}
    for idx in set(feats.get("liga", [])) | set(feats.get("calt", [])):
        for st in gsub.LookupList.Lookup[idx].SubTable:
            for first, lst in st.ligatures.items():
                lens = [len(lig.Component) for lig in lst]
                if lens != sorted(lens, reverse=True):
                    fail(f"ligature set for {first} not longest-first: {lens}")
                for lig in lst:
                    ligs[(first, *lig.Component)] = lig.LigGlyph
    want_ligs = dict(ligature_rules())
    if ligs != want_ligs:
        missing = {k: v for k, v in want_ligs.items() if ligs.get(k) != v}
        fail(f"ligatures differ; missing/wrong {missing}; extra {set(ligs) - set(want_ligs)}")
    else:
        print(f"GSUB liga+calt (DFLT, latn): {len(ligs)} rules, "
              f"<mark> {CONT_NAME} {CONT_NAME} -> .wide before <mark> {CONT_NAME} -> .mid")

    for d in ("~/Library/Fonts", "/Library/Fonts"):
        jb = os.path.join(os.path.expanduser(d), "JetBrainsMono[wght].ttf")
        if os.path.exists(jb):
            j = TTFont(jb)
            ref = (j["head"].unitsPerEm, j["hmtx"]["H"][0], j["hhea"].ascent, j["hhea"].descent)
            mine = (f["head"].unitsPerEm, ADVANCE, f["hhea"].ascent, f["hhea"].descent)
            print(f"JetBrains Mono (upm, advance, ascent, descent) {ref}; AgentMarks {mine}")
            if ref != mine:
                fail("metrics differ from JetBrains Mono")
            break
    print("verify:", "OK" if ok else "FAILED")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "AgentMarks.ttf"))
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    print("wrote", build(a.output))
    if a.verify and not verify(a.output):
        sys.exit(1)


if __name__ == "__main__":
    main()
