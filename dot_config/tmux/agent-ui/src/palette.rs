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
pub const UI: [(&str, &str); 6] = [
    ("base", "#1e1e2e"), ("mantle", "#181825"), ("surface2", "#585b70"),
    ("mauve", "#cba6f7"), ("lavender", "#b4befe"), ("sel", "#2a2b3d"),
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
