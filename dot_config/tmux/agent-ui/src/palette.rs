//! The agent colour language: the THIRD (now fourth) surface of one palette.
//!
//! [`HEX`] must stay byte-identical to `HEX` in `scripts/agent-roster.py`, the
//! `@catppuccin_window_*` formats in `tmux.conf.tmpl` and cua-notch
//! `Sources/Constants.swift`; cua-notch `dev/check-invariants` section 65
//! fails if they drift. Keep it as ONE literal table, one `("name", "#rrggbb")`
//! pair per entry, so that check can be pointed at this file with a regex.
//!
//! Colours are only ever named here; every other module refers to them by
//! name (`"red"`, `"overlay"`, ...), exactly like the Python `fg(name)`.

use ratatui::style::Color;

/// name → `#rrggbb`. Same 16 entries, same values as agent-roster.py `HEX`.
pub const HEX: [(&str, &str); 16] = [
    ("red", "#f38ba8"), ("yellow", "#f9e2af"), ("green", "#a6e3a1"), ("pink", "#f5c2e7"),
    ("blue", "#89b4fa"), ("teal", "#94e2d5"), ("dimteal", "#659a91"), ("dimblue", "#5d7aaa"),
    ("peach", "#fab387"), ("text", "#cdd6f4"), ("sub", "#a6adc8"), ("overlay", "#6c7086"),
    ("surface0", "#313244"), ("surface1", "#45475a"), ("crust", "#11111b"), ("sky", "#89dceb"),
];

/// Chrome-only colours the sidebar and menu draw with (backgrounds, the
/// focused branch, selection fill). Kept OUT of [`HEX`] on purpose: they
/// carry no agent state, so the cross-surface check has nothing to compare
/// them with. Never give a state its colour from this table.
///
/// `overlay2` is the dim text for a SURFACE (a pill, a card tint, a lifted
/// row): `overlay` there is under 3:1, overlay2 about 4-4.5:1, and still
/// below `sub`, so the hierarchy holds.
pub const UI: [(&str, &str); 7] = [
    ("base", "#1e1e2e"), ("mantle", "#181825"), ("surface2", "#585b70"),
    ("mauve", "#cba6f7"), ("lavender", "#b4befe"), ("sel", "#2a2b3d"),
    ("overlay2", "#9399b2"),
];

/// The `#rrggbb` for a palette name ([`HEX`] first, then [`UI`]). Panics on
/// an unknown name: a typo in a colour name is a programming error, as
/// `HEX[name]` is in the Python.
pub fn hex(name: &str) -> &'static str {
    HEX.iter()
        .chain(UI.iter())
        .find(|(n, _)| *n == name)
        .map(|(_, h)| *h)
        .unwrap_or_else(|| panic!("unknown palette colour {name:?}"))
}

/// The (r, g, b) for a palette name.
pub fn rgb(name: &str) -> (u8, u8, u8) {
    let h = hex(name);
    let p = |i: usize| u8::from_str_radix(&h[i..i + 2], 16).expect("palette hex");
    (p(1), p(3), p(5))
}

/// A ratatui truecolour for a palette name.
pub fn color(name: &str) -> Color {
    let (r, g, b) = rgb(name);
    Color::Rgb(r, g, b)
}

/// The canvas the sidebar's tints are mixed over: WezTerm's (and tmux's)
/// catppuccin-mocha background, which the sidebar leaves as the terminal's
/// default everywhere it does not tint.
pub const CANVAS: &str = "base";

/// `name` at `pct`% over [`CANVAS`]: the subtle surface a card or a
/// highlighted row sits on (a NEEDS YOU card in its state's colour, the
/// stash selection's mauve).
pub fn mix_rgb(name: &str, pct: u8) -> (u8, u8, u8) {
    let (a, b) = (rgb(name), rgb(CANVAS));
    let p = f64::from(pct.min(100)) / 100.0;
    let m = |x: u8, y: u8| (f64::from(x) * p + f64::from(y) * (1.0 - p)).round() as u8;
    (m(a.0, b.0), m(a.1, b.1), m(a.2, b.2))
}

/// [`mix_rgb`] as a ratatui colour.
pub fn mix(name: &str, pct: u8) -> Color {
    let (r, g, b) = mix_rgb(name, pct);
    Color::Rgb(r, g, b)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table_is_the_pythons() {
        // A copy of agent-roster.py's HEX literal: changing one side without
        // the other must fail here as well as in check-invariants.
        let py = r##"{"red": "#f38ba8", "yellow": "#f9e2af", "green": "#a6e3a1", "pink": "#f5c2e7",
    "blue": "#89b4fa", "teal": "#94e2d5", "dimteal": "#659a91", "dimblue": "#5d7aaa",
    "peach": "#fab387", "text": "#cdd6f4", "sub": "#a6adc8", "overlay": "#6c7086",
    "surface0": "#313244", "surface1": "#45475a", "crust": "#11111b", "sky": "#89dceb","##;
        for (n, h) in HEX {
            assert!(py.contains(&format!("\"{n}\": \"{h}\"")), "{n} {h}");
        }
        assert_eq!(rgb("red"), (0xf3, 0x8b, 0xa8));
        assert_eq!(color("crust"), Color::Rgb(0x11, 0x11, 0x1b));
        // Tints: the ends are the canvas and the colour itself.
        assert_eq!((mix_rgb("red", 0), mix_rgb("red", 100)), (rgb("base"), rgb("red")));
        assert_eq!(mix("yellow", 50), Color::Rgb(0x8c, 0x80, 0x6f));
    }

    /// WCAG contrast of `fg` on `bg`.
    fn contrast(fg: (u8, u8, u8), bg: (u8, u8, u8)) -> f64 {
        let lum = |(r, g, b): (u8, u8, u8)| {
            let c = |v: u8| {
                let s = f64::from(v) / 255.0;
                if s <= 0.03928 {
                    s / 12.92
                } else {
                    ((s + 0.055) / 1.055).powf(2.4)
                }
            };
            0.2126 * c(r) + 0.7152 * c(g) + 0.0722 * c(b)
        };
        let (a, b) = (lum(fg), lum(bg));
        (a.max(b) + 0.05) / (a.min(b) + 0.05)
    }

    /// Dim text on the sidebar's surfaces reads (about 4:1 or better) and
    /// stays below `sub` (the hierarchy); `overlay` there did not.
    #[test]
    fn dim_text_on_surfaces_reads() {
        let bgs = [rgb("surface0"), mix_rgb("yellow", 14), mix_rgb("red", 14), mix_rgb("green", 14), mix_rgb("mauve", 18)];
        for bg in bgs {
            let c = contrast(rgb("overlay2"), bg);
            assert!(c >= 3.9, "{bg:?}: {c:.2}");
            assert!(contrast(rgb("sub"), bg) > c);
            assert!(contrast(rgb("overlay"), bg) < 3.0, "{bg:?}");
        }
    }

    #[test]
    fn matches_the_installed_python_when_present() {
        let src = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../scripts/executable_agent-roster.py");
        let Ok(text) = std::fs::read_to_string(src) else { return };
        for (n, h) in HEX {
            assert!(text.contains(&format!("\"{n}\": \"{h}\"")), "agent-roster.py lost {n} {h}");
        }
    }
}
