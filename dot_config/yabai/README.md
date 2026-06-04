# Yabai + Skhd + Karabiner-Elements + WezTerm: Complete Setup Reference

## 1. Overview & Mental Model

This is a single-laptop-first tiling window manager setup optimized for seamless occasional multi-display work. The system treats the built-in MacBook display as the permanent "master" home for all work, with an optional external display as a temporary "external" work surface that comes and goes via dock.

**Core Mental Model:**

- **Master display (laptop)**: Always present, hosts all 10 canonical labeled workspaces (terminal, main, school, todo, schedule, mail, calendar, messages, chatgpt, codex). Stable reference point.
- **External display**: Optional. Comes up empty-and-ready when docked; user manually pushes workspaces there (or pulls them back home). Automatically healed on undock.
- **Labeled spaces, not indexed**: Spaces are identified by stable **labels** (not fragile array indices), because macOS renumbers indices whenever Mission Control is touched or displays change. All bindings, rules, and scripts use labels as the canonical reference.
- **Stack layout**: Only one window visible at a time per space; other windows are hidden in a z-order stack. Navigate with `hyper+z` (next) / `hyper+x` (prev).
- **Hyper modifier**: Caps Lock remapped (via Karabiner) to Cmd+Ctrl+Opt+Shift; this unified modifier powers nearly all window-manager shortcuts.

## 2. Where Everything Lives

### File Map: Configuration Sources & Targets

| Source (Chezmoi) | Target (Deployed To) | Type | Purpose |
|---|---|---|---|
| `/Users/mackhaymond/.local/share/chezmoi/dot_config/yabai/executable_yabairc` | `~/.config/yabai/yabairc` | Executable shell script | Window manager core: signals, rules, app pinning, layout |
| `/Users/mackhaymond/.local/share/chezmoi/dot_config/skhd/skhdrc.tmpl` | `~/.config/skhd/skhdrc` | Template config | Hotkey daemon: all keybindings (hyper+key → yabai commands) |
| `/Users/mackhaymond/.local/share/chezmoi/dot_config/wezterm/wezterm.lua.tmpl` | `~/.config/wezterm/wezterm.lua` | Template config | Terminal emulator: startup, keybindings, tmux integration |
| `/Users/mackhaymond/.local/share/chezmoi/dot_config/private_karabiner/private_karabiner.json` | `~/.config/karabiner/karabiner.json` | JSON config (git-ignored) | Key remapping: caps_lock→hyper, F-key aliases (F18/F19/F13/F14) |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_workspace.sh` | `~/code/various_scripts/yabai_workspace.sh` | Executable script | Focus workspace by label |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_send_window.sh` | `~/code/various_scripts/yabai_send_window.sh` | Executable script | Move focused window to space (respects pinned homes) |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_workspace_refresh.sh` | `~/code/various_scripts/yabai_workspace_refresh.sh` | Executable script | Reconcile canonical labels, refresh display topology cache |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_display.sh` | `~/code/various_scripts/yabai_display.sh` | Executable script | Focus display by name (master/external) |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_space_move.sh` | `~/code/various_scripts/yabai_space_move.sh` | Executable script | Push/pull spaces between displays |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_displays.sh` | `~/code/various_scripts/yabai_displays.sh` | Executable script | Hotplug handler: dock/undock logic |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_skhd_mode.sh` | `~/code/various_scripts/yabai_skhd_mode.sh` | Executable script | Toggle space layout (bsp ↔ stack) |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_skhd_stack_next.sh` | `~/code/various_scripts/yabai_skhd_stack_next.sh` | Executable script | Focus next window in stack |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_skhd_stack_prev.sh` | `~/code/various_scripts/yabai_skhd_stack_prev.sh` | Executable script | Focus previous window in stack |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_mouse_follow.sh` | `~/code/various_scripts/yabai_mouse_follow.sh` | Executable script | Warp cursor to newly focused display |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_screen_flash.sh` | `~/code/various_scripts/yabai_screen_flash.sh` | Executable script | Visual border flash on external display focus |
| `…/code/various_scripts/yabai_screen_flash.js` | `~/code/various_scripts/yabai_screen_flash.js` | JXA helper | Draws/fades the border overlay for `yabai_screen_flash.sh` |
| `…/code/various_scripts/executable_yabai_reorder_spaces.sh` | `~/code/various_scripts/yabai_reorder_spaces.sh` | Executable script | Keep labeled spaces in canonical order per display |
| `…/code/various_scripts/executable_yabai_fullscreen_focus.sh` | `~/code/various_scripts/yabai_fullscreen_focus.sh` | Executable script | Focus the Nth native-fullscreen app (`hyper+3-9`), WezTerm excluded |
| `…/code/various_scripts/executable_yabai_terminal_follow.sh` | `~/code/various_scripts/yabai_terminal_follow.sh` | Executable script | Keep `terminal` label on WezTerm in/out of fullscreen; sweep husk spaces |
| `…/dot_hammerspoon/init.lua` | `~/.hammerspoon/init.lua` | Lua config | **Hammerspoon**: classify Arc windows via AXIdentifier; pin the two main windows to main/school (Little Arc left managed). Required dependency, launches at login |

### Shared State: Display Topology Cache

**Path:** `~/.cache/yabai/workspace_cache.env` (default; override via `$YABAI_WORKSPACE_CACHE` env var)

**Canonical Contents:**
```bash
DISPLAY_COUNT=<0, 1, or 2>
MASTER_DISPLAY_INDEX=<usually 1>
EXTERNAL_DISPLAY_INDEX=<index or empty>
MASTER_DISPLAY_UUID=37D8832A-2D66-02CA-B9F7-8F30A301B230
```

**Purpose:** Sourced by all workspace scripts to avoid expensive repeated `yabai -m query --displays` calls. Written atomically by `yabai_workspace_refresh.sh` and `yabai_displays.sh`.

## 3. Component Deep-Dives

### 3.1 Yabai Core Configuration (yabairc)

**File:** `~/.config/yabai/yabairc`

#### Layout & Global Knobs

| Setting | Value | Purpose |
|---------|-------|---------|
| `layout` | `stack` | Single window visible; others stacked and hidden. Navigate with hyper+z/x. |
| `window_origin_display` | `focused` | New windows inherit the focused display (not the app's launch display). |
| `display_arrangement_order` | `horizontal` | External display to the right of laptop. |
| `window_shadow` | `off` | No drop shadow on borders. |
| `top_padding` | `0` | No padding above first window; macOS menu bar handles offset. |

#### Window Rules: Unmanaged Apps

These applications do not participate in yabai's tiling; they float freely:

**System utilities:** System Settings, Calculator, BetterTouchTool, Karabiner-Elements  
**Monitoring:** Activity Monitor, DaisyDisk, iStat Menus (also disables border)  
**Other:** Mail, Finder, Steam, BetterZip, Python REPL windows, DevPod, Setapp, Permute, TI-Nspire, LiveMath Maker, Antidote

#### App-to-Space Pinning Rules

These apps automatically appear on their designated space when launched:

| App | Target Space |
|-----|--------------|
| wezterm-gui, WezTerm | terminal |
| Todoist | todo |
| Granola | schedule |
| Spark Mail | mail |
| Notion Calendar | calendar |
| Messages | messages |
| ChatGPT | chatgpt |
| Codex | codex |

**Arc (browser):** *not* a yabai rule (the two main windows go to two different
spaces, and Little Arc is indistinguishable to yabai). The two main Arc windows
are pinned to `main`/`school` by **Hammerspoon** (`dot_hammerspoon/init.lua`) via
AXIdentifier; Little Arc stays managed. See the "Arc window pinning" design note.

**Why label-based queries?** When yabai queries spaces, it uses labels (e.g., `yabai -m query --spaces --space terminal`) rather than indices. This makes all rules robust to index renumbering caused by Mission Control or display hotplug.

#### Signal Handlers

**1. `dock_did_restart`** → `sudo yabai --load-sa`. Reloads the scripting addition (needed for native-fullscreen, space create/destroy, etc.) after the Dock restarts.

**2. `window_created`**
- **WezTerm:** if a *normal* WezTerm lands on the wrong space, move it to terminal. A *fullscreen* WezTerm is left alone (guarded by `is-native-fullscreen`) so it isn't yanked out of fullscreen.
- **Arc:** call `hs -c "arcSync()"` (Hammerspoon re-pins the Arc main windows; Little Arc untouched).
- **Other non-WezTerm on terminal:** bounce to the nearest labeled non-terminal space on the same display, keeping terminal pure.

**3. `space_changed`** → `yabai_terminal_follow.sh` then re-activate WezTerm. The follow hook keeps the `terminal` label pinned to WezTerm wherever it roams (including in/out of a native-fullscreen Space), reorders, and sweeps surplus empty husk spaces. Cheap no-op when WezTerm hasn't moved.

**4. `application_launched`** → `yabai -m rule --apply` (re-pins Todoist/Messages/etc.) **and** `hs -c "arcSync()"` (re-pins the Arc main windows) — one consistent "snap" moment.

**5. `display_added` (label `workspace_display_added`)** → `yabai_displays.sh added`: debounced hotplug; settles display count and refreshes cache (non-destructive; external comes up empty).

**6. `display_removed` (label `workspace_display_removed`)** → `yabai_displays.sh removed`: pulls all labeled spaces home to the master display.

**7. `display_changed` (label `mouse_follow_display`)** → `yabai_mouse_follow.sh`: warps cursor to the newly focused display.

**8. `display_changed` (label `flash_external_display`)** → `yabai_screen_flash.sh`: flashes a border if focus moved to the external display.

*(Also: a one-shot menu-bar/startup sync runs at the end of yabairc. Specific line numbers are intentionally omitted here — they drift; grep the signal name in `yabairc`.)*

#### Environment Variables Exported

| Variable | Value | Consumed By |
|----------|-------|-------------|
| `YABAI_WORKSPACE_REFRESH` | `${HOME}/code/various_scripts/yabai_workspace_refresh.sh` | Startup, hotplug handlers, rules |
| `YABAI_DISPLAYS` | `${HOME}/code/various_scripts/yabai_displays.sh` | `display_added` / `display_removed` signals |
| `YABAI_MOUSE_FOLLOW` | `${HOME}/code/various_scripts/yabai_mouse_follow.sh` | `display_changed` signal |
| `YABAI_SCREEN_FLASH` | `${HOME}/code/various_scripts/yabai_screen_flash.sh` | `display_changed` signal |

### 3.2 Skhd Hotkey Daemon (skhdrc)

**File:** `~/.config/skhd/skhdrc`

Skhd binds keyboard events (from Karabiner) to yabai commands. All paths are templated during chezmoi apply; `{{ .chezmoi.homeDir }}` becomes `/Users/mackhaymond`.

**Hex Key Codes:**
- `0x32` = Backtick (`)
- `0x2A` = Backslash (\)

All other keys referenced by character (e.g., `hyper - 1`, `hyper - z`).

#### Focus Workspace (Hyper Layer)

Move focus to a labeled space without moving it. Focus stays on the current display.

| Keybinding | Key | Script | Workspace |
|---|---|---|---|
| `hyper - 0x32` | Backtick | `yabai_workspace.sh focus terminal` | terminal |
| `hyper - 1` | 1 | `yabai_workspace.sh focus main` | main |
| `hyper - 2` | 2 | `yabai_workspace.sh focus school` | school |
| `hyper - tab` | Tab | `yabai_workspace.sh focus todo` | todo |
| `hyper - q` | Q | `yabai_workspace.sh focus schedule` | schedule |
| `hyper - w` | W | `yabai_workspace.sh focus mail` | mail |
| `hyper - e` | E | `yabai_workspace.sh focus calendar` | calendar |
| `hyper - d` | D | `yabai_workspace.sh focus messages` | messages |
| `hyper - f` | F | `yabai_workspace.sh focus chatgpt` | chatgpt |
| `f18` | Caps+Esc | `yabai_workspace.sh focus codex` | codex |

#### Send Window to Workspace (Hyper+Fn Layer)

Move the focused window to a target space (unless pinned to home space). Focus stays in current space.

| Keybinding | Key | Script | Destination |
|---|---|---|---|
| `hyper + fn - 0x32` | Fn+Backtick | `yabai_send_window.sh terminal` | terminal |
| `hyper + fn - 1` | Fn+1 | `yabai_send_window.sh main` | main |
| `hyper + fn - 2` | Fn+2 | `yabai_send_window.sh school` | school |
| `hyper + fn - tab` | Fn+Tab | `yabai_send_window.sh todo` | todo |
| `hyper + fn - q` | Fn+Q | `yabai_send_window.sh schedule` | schedule |
| `hyper + fn - w` | Fn+W | `yabai_send_window.sh mail` | mail |
| `hyper + fn - e` | Fn+E | `yabai_send_window.sh calendar` | calendar |
| `hyper + fn - d` | Fn+D | `yabai_send_window.sh messages` | messages |
| `hyper + fn - f` | Fn+F | `yabai_send_window.sh chatgpt` | chatgpt |
| `f19` | Fn+Caps+Esc | `yabai_send_window.sh codex` | codex |

**Pinned Apps (cannot be sent off their home spaces):**
- wezterm → terminal
- Todoist → todo
- Granola → schedule
- Spark Mail → mail
- Notion Calendar → calendar
- Messages → messages
- ChatGPT → chatgpt
- Codex → codex
- Arc → protected on `main`/`school` (any Arc window on either is shielded; rare Little-Arc-on-home included)

#### Window Navigation & Layout (Hyper+Fn)

| Keybinding | Key | Command |
|---|---|---|
| `hyper + fn - j` | Fn+J | `yabai -m window --swap south` |
| `hyper + fn - k` | Fn+K | `yabai -m window --swap north` |
| `hyper + fn - h` | Fn+H | `yabai -m window --swap west` |
| `hyper + fn - l` | Fn+L | `yabai -m window --swap east` |
| `hyper + fn - a` | Fn+A | `yabai -m window --space prev` |
| `hyper + fn - s` | Fn+S | `yabai -m window --space next` |
| `hyper + fn - x` | Fn+X | `yabai -m space --mirror x-axis` |

#### Display & Space Movement

| Keybinding | Key | Script/Command | Action |
|---|---|---|---|
| `hyper - 0x2A` | Backslash | `yabai_space_move.sh push` | Move focused space to other display; follow |
| `hyper - 0` | 0 | `yabai_space_move.sh home-all` | Pull all labeled spaces to laptop |
| `f13` | Hyper+F1 | `yabai_display.sh master` | Focus laptop display |
| `f14` | Hyper+F2 | `yabai_display.sh external` | Focus external display |

#### Window Focus & Stack Navigation (Hyper)

| Keybinding | Key | Script/Command | Action |
|---|---|---|---|
| `hyper - z` | Z | `yabai_skhd_stack_next.sh` | Focus next window in stack (wrap to first) |
| `hyper - x` | X | `yabai_skhd_stack_prev.sh` | Focus previous window in stack (wrap to last) |
| `hyper - m` | M | `yabai -m window --toggle float --grid 6:6:1:1:4:4` | Toggle float; if floating, center at 4×4 grid cell |
| `hyper + fn - m` | Fn+M | `yabai -m window --toggle native-fullscreen` | Toggle native fullscreen |
| `hyper - b` | B | `yabai_skhd_mode.sh` | Toggle space layout (bsp ↔ stack) |

#### Native-Fullscreen App Access (Hyper)

Reach apps put into macOS native fullscreen — they live in their own Spaces outside the labeled model, so the focus-workspace keys can't reach them. Ordinal = mission-control order (display, then space index); **WezTerm is excluded** (it's the terminal, reached with `hyper+\``).

| Keybinding | Key | Script | Action |
|---|---|---|---|
| `hyper - 3` … `hyper - 9` | 3–9 | `yabai_fullscreen_focus.sh 1…7` | Focus the 1st…7th native-fullscreen app (no-op if that many aren't open) |

*(`hyper - 1`/`- 2` = focus main/school; `hyper - 0` = pull-home. So 3–9 were free.)*

### 3.3 Karabiner-Elements Key Remapping

**File:** `~/.config/karabiner/karabiner.json`

Karabiner preprocesses keyboard input at the OS level, creating the hyper modifier and translating raw key presses to F-keys that skhd can bind.

#### Core Remapping: Caps Lock → Hyper

**From:** `caps_lock` (any modifiers optional)  
**To:** `left_shift + left_command + left_control + left_option` (the "hyper" modifier)

This single remapping enables nearly every downstream binding.

#### Complex Modifications: F-Key Chords

| From | To | Skhd Binding | Purpose |
|------|----|----|---------|
| F1 + hyper | F13 | `f13 : yabai_display.sh master` | Focus master (laptop) display |
| F2 + hyper | F14 | `f14 : yabai_display.sh external` | Focus external display |
| Escape + hyper (no fn) | F18 | `f18 : yabai_workspace.sh focus codex` | Focus codex workspace |
| Escape + hyper + fn | F19 | `f19 : yabai_send_window.sh codex` | Send window to codex workspace |
| Caps_Lock + Escape (simultaneous) | F18 | `f18 : yabai_workspace.sh focus codex` | Alt ergonomic path to focus codex |
| Fn + Caps_Lock + Escape (simultaneous) | F19 | `f19 : yabai_send_window.sh codex` | Alt ergonomic path to send to codex |

#### System FN Row Preservation

F1–F5 remain mapped to macOS functions (brightness, Mission Control, Launchpad) to preserve system functionality.

#### Ignored Device

External mechanical keyboard (vendor_id 1452) is ignored, so Karabiner only processes the built-in keyboard.

### 3.4 WezTerm Terminal Configuration

**File:** `~/.config/wezterm/wezterm.lua`

WezTerm is the primary terminal, pinned to the `terminal` space and fully managed by yabai for resizing across displays.

#### Window Integration with Yabai

**Critical setting: `window_decorations = "RESIZE"`**

- Maintains borderless, edge-to-edge aesthetics.
- Reports window as resizable to macOS, allowing yabai to apply any dimensions.
- Avoids native fullscreen locking that would prevent adaptive cross-display resizing.

#### Startup & Tmux

**Default program:**
```lua
config.default_prog = { "/opt/homebrew/bin/tmux", "new-session", "-A", "-s", "main" }
```
Every new WezTerm window attaches or creates a tmux session named `main`.

**GUI startup event:**
```lua
wezterm.on('gui-startup', function(cmd)
    local _, _, window = mux.spawn_window(cmd or {})
    window:gui_window():toggle_fullscreen()
end)
```
Starts WezTerm in **macOS native fullscreen by default** (needs `native_macos_fullscreen_mode = true`). yabai/​`yabai_terminal_follow.sh` keep the `terminal` label on it and reorder it to position 1. See the "WezTerm native fullscreen (default)" design note.

#### Display & UI Settings

- `enable_tab_bar = false` — tabs managed by tmux, not WezTerm.
- `enable_kitty_graphics = true` — inline images/SVG support.
- `scrollback_lines = 0` — scrollback via tmux history, not terminal buffer.
- `native_macos_fullscreen_mode = true` — WezTerm uses real macOS native fullscreen, so a fullscreened terminal becomes a proper fullscreen Space that yabai can label/reorder/focus (see "WezTerm native fullscreen" below). With this `false`, the window drops the macOS fullscreen capability entirely and yabai cannot fullscreen it.
- `macos_fullscreen_extend_behind_notch = true` — extends rendering behind notch.

#### Performance

- `front_end = "WebGpu"` — GPU-accelerated rendering.
- `max_fps = 120` — matches ProMotion external display.
- `animation_fps = 1`, `cursor_blink_rate = 0`, `use_ime = false` — minimal overhead.

#### Keybindings

All WezTerm keybindings forward to tmux prefix (`Ctrl+S`) chords, delegating window/pane management to tmux:

| WezTerm | Sends | Tmux Command | Result |
|---------|-------|------------|--------|
| Cmd+T | Ctrl+S, C | `bind c` | New window |
| Cmd+Shift+T | Ctrl+S, Ctrl+T | `bind C-t` | New window at end (HOME cwd) |
| Cmd+Shift+R | Ctrl+S, Shift+R | `bind R` | Recreate window in place (same cwd) |
| Cmd+W | Ctrl+S, X | `bind x` | Kill pane/window |
| Cmd+Shift+W | Ctrl+S, Ctrl+L | `bind C-l` | Kill pane |
| Cmd+1 through Cmd+9 | Ctrl+S, N | `bind N` | Select window N |

## 4. The Scripts: Purpose & Interconnections

### Script Reference Table

| Script | Arguments | Purpose |
|--------|-----------|---------|
| `yabai_workspace.sh` | `focus <label>` \| `master <label>` | Focus workspace by label (wherever it lives) or bring home then focus |
| `yabai_send_window.sh` | `<label>` | Move focused window to space; blocked if window is pinned and already on home space |
| `yabai_display.sh` | `master` \| `external` | Focus the laptop or external display; no-op on single display |
| `yabai_space_move.sh` | `push` \| `home-all` | Cross-display space movement: push focused space to other display (with follow), or pull all labels home |
| `yabai_displays.sh` | `added` \| `removed` | Hotplug handler: dock = refresh cache (non-destructive); undock = pull home safety net |
| `yabai_workspace_refresh.sh` | (none; on-demand) | Reconcile canonical labels on all displays; refresh display topology cache |
| `yabai_skhd_mode.sh` | (none) | Toggle focused space layout (bsp ↔ stack) |
| `yabai_skhd_stack_next.sh` | (none) | Focus next window in current stack layer; wrap to first |
| `yabai_skhd_stack_prev.sh` | (none) | Focus previous window in current stack layer; wrap to last |
| `yabai_mouse_follow.sh` | (none; signal handler) | Warp mouse cursor to focused display center (if not already there) |
| `yabai_screen_flash.sh` | (none; signal handler) | Flash orange border around external display when focus moves there |
| `yabai_reorder_spaces.sh` | (none) | Slide labeled spaces into canonical order per display (reserves non-master's first space as scratch); handles fullscreen spaces |
| `yabai_fullscreen_focus.sh` | `<ordinal>` | Focus the Nth native-fullscreen app in mission-control order (`hyper+3-9`); excludes WezTerm |
| `yabai_terminal_follow.sh` | (none; space_changed hook) | Re-pin `terminal` label onto WezTerm's space (incl. fullscreen) + reorder; sweep surplus empty husk spaces |

### How It All Connects

#### Data Flow: Cache-Driven Architecture

```
yabai_workspace_refresh.sh (cache writer)
    ├── Queries: yabai -m query --displays / --spaces
    ├── Resolves: MASTER_DISPLAY_UUID match → MASTER_DISPLAY_INDEX
    ├── Fallback: smallest-area display (laptop)
    ├── Writes atomically: ~/.cache/yabai/workspace_cache.env
    └── Contents: DISPLAY_COUNT, MASTER_DISPLAY_INDEX, EXTERNAL_DISPLAY_INDEX, MASTER_DISPLAY_UUID

Readers (all workspace-aware scripts):
    ├── yabai_workspace.sh
    ├── yabai_send_window.sh
    ├── yabai_display.sh
    ├── yabai_space_move.sh
    └── yabai_display.sh
    
    Load pattern:
        [ -r "$CACHE_FILE" ] && . "$CACHE_FILE"
        [ -n "$DISPLAY_COUNT" ] || call yabai_workspace_refresh.sh && retry
```

**Key insight:** All reader scripts source the cache to avoid expensive repeated `yabai -m query --displays` calls. If the cache is missing or stale, they invoke `yabai_workspace_refresh.sh` to heal it.

#### Labeled Space Stability

**Core principle:** Spaces are identified by **labels** (terminal, main, school, etc.), not array indices.

When yabai queries spaces, it uses label-based lookups:
```bash
# Query a space by label (returns live index, even after Mission Control renumbering)
yabai -m query --spaces --space terminal

# Move a space by label (works regardless of current index)
yabai -m space terminal --display "$target_idx"

# Focus a space by label (survives space reordering)
yabai -m space --focus main
```

**Why?** macOS renumbers space indices whenever:
- User opens/closes Mission Control
- Displays plug/unplug
- Workspaces are created/destroyed

Labels persist through all these events, making the entire system stable and predictable.

#### Dock/Undock Flow

**On Plug (External Monitor Connected):**
1. macOS emits `display_added` signal.
2. yabai's signal handler calls `yabai_displays.sh added`.
3. Script acquires lock (coalesce duplicate signals).
4. Polls display count until stable.
5. Calls `yabai_workspace_refresh.sh`:
   - Queries new display topology.
   - Resolves MASTER_DISPLAY_INDEX and new EXTERNAL_DISPLAY_INDEX.
   - Ensures all 10 canonical labels exist on master.
   - **Does NOT move any spaces** (external comes up empty-and-ready).
   - Writes cache.
6. User manually pushes workspaces via `hyper+\` (skhd → yabai_space_move.sh push) or pulls master workspaces to external display.

**On Unplug (External Monitor Disconnected):**
1. macOS emits `display_removed` signal.
2. yabai's signal handler calls `yabai_displays.sh removed`.
3. Script acquires lock and settles display count.
4. Calls `yabai_workspace_refresh.sh` (refresh cache; see topology change).
5. **Pull-home safety net:**
   - For each of the 10 canonical labels:
     - If label lives on non-master display, move it: `yabai -m space <label> --display <master>`
   - Resolves master by UUID, then area, then cache, then default 1.
6. Applies rules and refreshes again (final settle).
7. Result: all labeled workspaces are back on the laptop, ready for the next dock.

**Why non-destructive on dock, destructive on undock?**
- **Dock:** External is transient; keep laptop layout untouched; external comes up clean for fresh work.
- **Undock:** Prevent orphaned windows on non-existent display; safety net pulls everything home.

#### Mouse Follow & Screen Flash

**When cross-display focus changes** (via F13, F14, or any focus binding that jumps displays):

1. yabai's `display_changed` signal fires.
2. **First handler:** `yabai_mouse_follow.sh`
   - Queries focused display and display under cursor.
   - If they differ, warps cursor to focused window/display center.
   - Single-display guard: no-op on laptops without external.
3. **Second handler:** `yabai_screen_flash.sh`
   - Identifies focused display (UUID match for master vs. external).
   - If external, flashes orange border for ~0.4s (hold 0.18s, fade 0.22s).
   - If master, no flash.
   - Single-display guard: no-op on single display.

**Tunables for screen flash (env vars):**
- `YABAI_FLASH_BORDER` [8] — border width in pixels
- `YABAI_FLASH_R/G/B` [1.0/0.6/0.0] — sRGB color (default orange)
- `YABAI_FLASH_HOLD` [0.18] — solid border duration (seconds)
- `YABAI_FLASH_FADE` [0.22] — fade-out duration (seconds)
- `YABAI_FLASH_RADIUS` [13] — corner radius in pixels

#### Terminal Space Purity

Terminal space is reserved for WezTerm only. Yabai enforces this via a `window_created` signal:

1. When a new window appears:
   - If it's a WezTerm window, ensure it lands on the terminal space (compensate for racy rule application).
   - If it's non-WezTerm **and** it landed on the terminal space, bounce it to the nearest labeled non-terminal space on the same display.
2. Bounce target: first labeled space (excluding terminal) on the same display.
3. If no labeled space exists (terminal is alone), leave the intruder in place.
4. Focus follows the window to the destination space.

**Why?** Terminal is a special single-window-focused workspace. The bounce ensures CLI stays uncluttered while preventing accidental window loss on the external display (where the first space is scratch and destroyed on disconnect).

#### Canonical Space Ordering

The 10 spaces are always maintained in the order: terminal, main, school, todo, schedule, mail, calendar, messages, chatgpt, codex.

**Responsibility:** `yabai_reorder_spaces.sh` (called at the end of `yabai_workspace_refresh.sh` and after `yabai_space_move.sh` operations).

**Per-display logic:**
- **Master (laptop):** Labels start at the first space (index 1, 2, 3, ...).
- **External:** First space reserved as scratch (macOS destroys it on disconnect). Labels start at the second space.

**Algorithm:**
1. For each display, find minimum space index (`lo`).
2. Set `pos = lo` for master, `pos = lo + 1` for external.
3. For each label in canonical order:
   - If on this display and index ≠ `pos`, move it: `yabai -m space <label> --move <pos>`.
   - Increment `pos`.

**Result:** Wherever labels roam, they maintain their stable sequence, making the layout predictable and recoverable.

## 5. Common Workflows & Runbook

### Focus a Workspace

**Goal:** Switch focus to a labeled workspace without moving it.

**Action:**
```bash
# From anywhere, press hyper+<key> for the workspace
hyper - 1              # Focus "main" workspace
hyper - 0x32 (`)       # Focus "terminal" workspace
f18                    # Focus "codex" workspace
```

**What happens:**
1. skhd captures the keybinding.
2. skhd invokes `yabai_workspace.sh focus <label>`.
3. Script loads display cache (single-display fast-path or multi-display topology).
4. Script queries space's live index by label: `yabai -m query --spaces --space <label>`.
5. Script focuses that index: `yabai -m space --focus <index>`.
6. Focus changes to the space (wherever it lives—laptop or external).
7. If the space is on the external display, `display_changed` signal fires → mouse follows + border flashes.

### Send a Window to a Workspace

**Goal:** Move the focused window to a target workspace. Focus stays where you are.

**Action:**
```bash
# From anywhere, press hyper+fn+<key> for the destination
hyper + fn - 1         # Send focused window to "main"
hyper + fn - 0x32 (`)  # Send to "terminal" (if not pinned)
f19                    # Send to "codex"
```

**What happens:**
1. skhd captures the keybinding.
2. skhd invokes `yabai_send_window.sh <label>`.
3. Script checks if the window is pinned to a home space (wezterm → terminal, Todoist → todo, etc.).
4. If pinned and already on home space, script exits (bound window cannot move).
5. Otherwise, script queries target space's index by label.
6. Script moves window: `yabai -m window --space <label>`.
7. Window departs; focus stays on current space.

### Push a Workspace to the External Display

**Goal:** Move the focused workspace (and all its windows) to the other display.

**Prerequisites:** External display is connected.

**Action:**
```bash
# Press the push binding
hyper - 0x2A (\)       # Push focused workspace to other display
```

**What happens:**
1. skhd captures the keybinding.
2. skhd invokes `yabai_space_move.sh push`.
3. Script loads cache; resolves MASTER_DISPLAY_INDEX and EXTERNAL_DISPLAY_INDEX.
4. Script queries focused space (snapshot id, index, display).
5. Script determines target: if on master → external; if on external → master.
6. Script moves the space: `yabai -m space <index> --display <target>`.
7. yabai resizes all windows in the space to fill the new display.
8. Script follows: resolves the space's new index (indices renumber after move), focuses it (with retry on race condition).
9. Script reorders spaces to restore canonical order.
10. Result: Workspace and all windows are now on the other display, with focus following.

### Pull All Workspaces Home to Laptop

**Goal:** Move all labeled workspaces from the external display back to the laptop (manual alternative to undock safety net).

**Prerequisites:** External display is connected.

**Action:**
```bash
# Press the home-all binding
hyper - 0              # Pull all labeled workspaces to laptop
```

**What happens:**
1. skhd invokes `yabai_space_move.sh home-all`.
2. Script loads cache.
3. For each of the 10 canonical labels:
   - Script queries the space's current display.
   - If on external, moves it: `yabai -m space <label> --display <master>`.
4. Script focuses master display.
5. Script reorders spaces to restore canonical order.
6. Result: All workspaces are back on the laptop.

### Focus the External Display

**Goal:** Shift active display focus (and mouse) to the external monitor.

**Prerequisites:** External display is connected.

**Action:**
```bash
# Press F14 (hyper+F2, remapped by Karabiner)
f14                    # Focus external display
```

**What happens:**
1. skhd invokes `yabai_display.sh external`.
2. Script loads cache; resolves EXTERNAL_DISPLAY_INDEX.
3. Script focuses the display: `yabai -m display --focus <external_idx>`.
4. yabai's `display_changed` signal fires.
5. `yabai_mouse_follow.sh` warps cursor to the external display's center.
6. `yabai_screen_flash.sh` flashes orange border around the external display.
7. Result: Focus is now on the external display.

### What Happens on Dock (External Monitor Plug)

1. **macOS hotplug event** → yabai `display_added` signal.
2. **yabai_displays.sh added**:
   - Acquires lock (coalesce 2–3 duplicate signals).
   - Polls display count until stable.
   - Calls `yabai_workspace_refresh.sh`:
     - Queries new topology (DISPLAY_COUNT=2, resolves EXTERNAL_DISPLAY_INDEX).
     - Ensures all 10 labels exist on master.
     - **Does NOT move any workspaces.**
     - Writes cache.
   - Applies rules.
3. **User action:** Manually push workspaces to external (hyper+\) or leave them on laptop.
4. **Result:** External display comes up empty-and-ready; user fills it on demand.

### What Happens on Undock (External Monitor Unplug)

1. **macOS hotplug event** → yabai `display_removed` signal.
2. **yabai_displays.sh removed**:
   - Acquires lock.
   - Polls display count until stable.
   - Calls `yabai_workspace_refresh.sh` (refresh cache; topology is now single-display).
   - **Pull-home safety net:** For each of the 10 labels:
     - If on non-master display, move it home: `yabai -m space <label> --display <master>`.
   - Applies rules; refreshes again (final settle).
3. **Result:** All workspaces are safely back on the laptop.

### Safely Edit Configuration: Complete Runbook

#### Step 1: Edit the Source File

**Location:** `/Users/mackhaymond/.local/share/chezmoi/` (never edit deployed configs directly).

**Example: Add a keybinding to skhd**
```bash
nano /Users/mackhaymond/.local/share/chezmoi/dot_config/skhd/skhdrc.tmpl

# Add a line like:
# hyper - n : /Users/mackhaymond/code/various_scripts/my_script.sh

# (Use {{ .chezmoi.homeDir }} for templates; yabairc uses $HOME at runtime)
```

**Example: Modify yabai rules or signals**
```bash
nano /Users/mackhaymond/.local/share/chezmoi/dot_config/yabai/executable_yabairc
```

**Example: Change Karabiner key mapping**
```bash
nano /Users/mackhaymond/.local/share/chezmoi/dot_config/private_karabiner/private_karabiner.json
# Be careful with JSON syntax!
```

**Example: Add a new shell script**
```bash
# Create the source with executable_ prefix
nano /Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_my_new_script.sh

# Add #!/bin/bash at top; chezmoi will set +x on deployment
```

#### Step 2: Preview Changes

```bash
# Review all pending diffs
chezmoi diff

# Or dry-run the full apply
chezmoi apply --dry-run

# Or review a specific file
chezmoi diff ~/.config/skhd/skhdrc
```

#### Step 3: Apply Changes to Home Directory

```bash
chezmoi apply
```

**What happens:**
1. Chezmoi renders all `.tmpl` files (substitutes `{{ .chezmoi.homeDir }}`, `{{ .homebrew_prefix }}`, etc.).
2. Converts `dot_` prefixes to `.`.
3. Sets `executable_` files to mode +x.
4. Deploys to target locations (~/.config/yabai/yabairc, ~/.config/skhd/skhdrc, ~/code/various_scripts/, etc.).
5. Preserves file permissions and attributes.

#### Step 4: Reload the Service(s)

**For skhd:**
```bash
skhd --reload
```

**For yabai (if changes affect signal handlers, rules, or layout):**
```bash
# Soft restart (keeps windows, reloads config)
yabai --restart-service

# Or hard restart (if soft fails)
launchctl kickstart -k gui/$(id -u)/com.koekeishiya.yabai
```

**For Karabiner:**
- Auto-reloads (watch the Karabiner menu for confirmation).
- Manual reload: Preferences → Reload JSON

**For WezTerm:**
- Config is watched; changes apply on next tab/window open.
- Or close all WezTerm windows and reopen.

#### Step 5: Commit & Push

```bash
cd /Users/mackhaymond/.local/share/chezmoi

# Stage the modified source file(s)
git add dot_config/skhd/skhdrc.tmpl

# Commit (pre-commit hook runs gitleaks to detect secrets)
git commit -m "Update skhd keybindings: add hyper-n binding for..."

# If gitleaks blocks (found hardcoded secrets):
# 1. Remove the secret from the file
# 2. Re-stage: git add <file>
# 3. Recommit: git commit -m "..."

# Push to remote
git push origin main
```

**Pre-commit Hook Behavior:**
- Runs gitleaks to scan staged files for hardcoded secrets (API keys, AWS creds, etc.).
- **Blocks commit if secrets are found** (must remove them first).
- Redacts secrets in error output for safety.

## 6. Complete Keybinding Cheat Sheet

| Keybinding | Physical Key | Action | Notes |
|---|---|---|---|
| **Focus Workspace** |
| `hyper - 0x32` | Backtick | Focus terminal | |
| `hyper - 1` | 1 | Focus main | |
| `hyper - 2` | 2 | Focus school | |
| `hyper - tab` | Tab | Focus todo | |
| `hyper - q` | Q | Focus schedule | |
| `hyper - w` | W | Focus mail | |
| `hyper - e` | E | Focus calendar | |
| `hyper - d` | D | Focus messages | |
| `hyper - f` | F | Focus chatgpt | |
| `f18` | Caps Lock+Escape | Focus codex | Karabiner-mapped |
| **Send Window to Workspace** |
| `hyper + fn - 0x32` | Fn+Backtick | Send to terminal | Respects pinned homes |
| `hyper + fn - 1` | Fn+1 | Send to main | Respects pinned homes |
| `hyper + fn - 2` | Fn+2 | Send to school | Respects pinned homes |
| `hyper + fn - tab` | Fn+Tab | Send to todo | Respects pinned homes |
| `hyper + fn - q` | Fn+Q | Send to schedule | Respects pinned homes |
| `hyper + fn - w` | Fn+W | Send to mail | Respects pinned homes |
| `hyper + fn - e` | Fn+E | Send to calendar | Respects pinned homes |
| `hyper + fn - d` | Fn+D | Send to messages | Respects pinned homes |
| `hyper + fn - f` | Fn+F | Send to chatgpt | Respects pinned homes |
| `f19` | Fn+Caps Lock+Escape | Send to codex | Karabiner-mapped |
| **Window Layout & Navigation** |
| `hyper - z` | Z | Focus next window in stack | Wraps to first |
| `hyper - x` | X | Focus previous window in stack | Wraps to last |
| `hyper + fn - j` | Fn+J | Swap focused window south | |
| `hyper + fn - k` | Fn+K | Swap focused window north | |
| `hyper + fn - h` | Fn+H | Swap focused window west | |
| `hyper + fn - l` | Fn+L | Swap focused window east | |
| `hyper + fn - a` | Fn+A | Move window to previous space | |
| `hyper + fn - s` | Fn+S | Move window to next space | |
| `hyper + fn - x` | Fn+X | Mirror space layout (x-axis) | |
| `hyper - m` | M | Toggle float; center if floating | 6×6 grid, cell 1,1 to 4,4 |
| `hyper + fn - m` | Fn+M | Toggle native fullscreen | |
| `hyper - b` | B | Toggle space layout (bsp ↔ stack) | |
| **Display & Cross-Display Movement** |
| `f13` | Hyper+F1 | Focus master (laptop) display | Karabiner-mapped |
| `f14` | Hyper+F2 | Focus external display | Karabiner-mapped |
| `hyper - 0x2A` | Backslash | Push focused space to other display | Moves all windows; follows |
| `hyper - 0` | 0 | Pull all workspaces home to laptop | Safety net for undock |
| **Native-Fullscreen App Access** |
| `hyper - 3` | 3 | Focus 1st native-fullscreen app | `yabai_fullscreen_focus.sh 1` |
| `hyper - 4` | 4 | Focus 2nd native-fullscreen app | ordinal 2 |
| `hyper - 5` … `hyper - 9` | 5–9 | Focus 3rd … 7th native-fullscreen app | ordinals 3–7; no-op if absent |

## Notes & Key Design Decisions

- **Single-laptop-first:** All 10 canonical workspaces live on the master (laptop) display by default. External display is optional and transient.
- **Label-based stability:** Spaces are identified by labels, not indices, surviving Mission Control renumbering and display hotplug.
- **Non-destructive dock:** External monitor comes up empty; user manually pushes workspaces.
- **Pull-home on undock:** Automatic safety net prevents orphaned windows on non-existent displays.
- **Terminal purity:** Terminal space is WezTerm-only; intruders are bounced to the next labeled space.
- **Pinned apps:** Certain apps (Todoist, Granola, Spark Mail, etc.) are sticky and cannot be moved off their home spaces.
- **Stack layout:** Only one window visible at a time; navigate with hyper+z/x to cycle through stacked layers.
- **Mouse follow:** Cursor automatically warps to newly focused display (reduced need for manual positioning).
- **Screen flash:** Visual orange border confirms focus jumped to external display.
- **Native-fullscreen access:** Apps put into macOS native fullscreen (non-pinned apps or the browser) live in their own Spaces *outside* the labeled model, so the `hyper+<label>` keys can't reach them. `hyper+3`…`hyper+9` focus the 1st…7th fullscreen app in mission-control order (display, then index) via `yabai_fullscreen_focus.sh`. Mapping is dynamic by position, not pinned per-app. **WezTerm is excluded** from these ordinals (it's the `terminal` workspace, reached with `hyper+\``, even when fullscreen). Note: yabai *can* label, `--move` (reorder), focus, and move fullscreen Spaces between displays — they are not as locked-down as commonly assumed.
- **WezTerm native fullscreen (default):** WezTerm **starts in macOS native fullscreen by default** — `gui-startup` in `wezterm.lua` calls `toggle_fullscreen()` after spawning (needs `native_macos_fullscreen_mode = true`). You can also toggle it with `hyper+fn+m`. Native fullscreen carries WezTerm into a new fullscreen Space and leaves the old `terminal` space behind empty. The `window_created` signal is guarded so it never drags a fullscreen WezTerm onto a fixed space. The `space_changed` hook `yabai_terminal_follow.sh` re-pins the `terminal` label onto WezTerm's current Space (fullscreen or not) and reorders, so `hyper+\`` still reaches it and it stays at canonical position 1. It's a cheap no-op on ordinary space switches (only acts when WezTerm actually changed Spaces). **Husk sweep:** entering/exiting fullscreen leaves the vacated Space behind as an empty desktop; when the hook relabels it also destroys the surplus empty, unlabeled, non-fullscreen spaces on the master display, keeping exactly **one** as a safe landing pad for fullscreen-exit (never touches labeled/fullscreen Spaces). Net result on the master is the 10 labeled spaces + at most one empty desktop, regardless of how many times you fullscreen/exit.
- **Arc window pinning (Hammerspoon, `dot_hammerspoon/init.lua`):** The two main Arc browser windows are pinned one-to-`main`, one-to-`school` (title-independent — it doesn't matter which goes where). Everything else about Arc, **including Little Arc popups, is left fully MANAGED** (tiled, in the stack, cyclable with `hyper+z/x`) — Little Arc is *not* floated and *not* pinned; it just lives wherever it opens.
  - The hard part: a **Little Arc** popup is byte-identical to a main window in *every* yabai field (subrole, floating, even title is the page title), so yabai cannot tell them apart. The only reliable discriminator is `AXIdentifier` (`bigBrowserWindow-*` vs `littleBrowserWindow-*`), which **yabai cannot read but Hammerspoon can**. macOS AX only exposes *current-Space* windows, so Hammerspoon reads the identifier off whatever Arc windows are on-screen and remembers which window ids are "main" (`mainSet`), accumulated as Spaces are visited.
  - It then pins **only** the remembered main ids to `main`/`school` via yabai (which *can* report any window's Space by id, cross-Space): **stably** (never disturbs a correctly-placed window; only fills an empty target; recovers drift) and **fullscreen-safe** (a fullscreen Arc window — e.g. video — is left alone, reachable via `hyper+3-9`, and returns to its space on exit). Because only `mainSet` ids are ever moved, Little Arc (never in the set) is never touched.
  - **Trigger (consistent with the other pinned apps):** yabai calls `hs -c "arcSync()"` from the **same signals that re-apply the other apps' `space=` rules** — `application_launched` (every app launch, right after `rule --apply`) and Arc `window_created` — so the Arc main windows "snap" to their spaces on the exact same cadence as Todoist/Messages/etc. **No polling/timer** (Hammerspoon's own window/space events proved unreliable with yabai's switching, so the reliable yabai signals drive it). A one-shot pass also runs on Hammerspoon load. `arcSync` self-prunes closed windows. Consequence: like the other rule-pinned apps, a manually-moved main window snaps back on the next app launch / window creation rather than instantly.
  - **Performance:** classification reads Arc's windows straight off the **application's AX element** (`hs.axuielement.applicationElement(arc):attributeValue("AXWindows")`), NOT `hs.window.allWindows()` — the latter enumerates every app's windows via AX and measured **~1.9 s** on this machine (the old 2 s timer therefore ran at ~91% of a core continuously, which is why it was removed). The app-element path makes `arcSync` ~85 ms.
  - **Force-move guard:** `yabai_send_window.sh` (`hyper+fn+<space>`) protects an Arc window sitting on `main`/`school` from being force-moved off it — the same home-space guard the other pinned apps get. It's a pure-yabai space check (no AXIdentifier), so it's reliable; the trade-off is it also shields a Little Arc that happens to be on main/school (rare). A main-only guard would need Hammerspoon's `arcFocusedKind` (kept in `init.lua`), but the AX "focused window" races with yabai's focus, so the simple space check is preferred.
  - **Dependency:** Hammerspoon must be running (set to launch at login via `hs.autoLaunch(true)`). If it's not running, the two main windows simply won't auto-pin and Little Arc behaves like any managed window — graceful degradation, not breakage. This replaced the old fragile title-based pinning in `yabai_workspace_refresh.sh`.
- **Chezmoi for everything:** All configs are templated sources in chezmoi; never edit deployed files directly. Always edit source, preview, apply, reload, commit.

## ⚠️ Still Needs Testing (Unverified)

These behaviors are believed correct from the code/logic but have **not** been verified live (no external display has been attached). Test them next time the external display is connected and update this section.

**✅ Now verified (no longer needs testing):**
- **WezTerm auto-fullscreen on startup** — confirmed on a real launch: quit+reopen WezTerm and it comes up in native fullscreen, labeled `terminal`, at index 1. (The `gui_window()` timing edge case did not occur.)

**Accessing a fullscreen window on the EXTERNAL display.** Getting it there works either way — `hyper+\` push moves a fullscreen Space to the external (empirically confirmed with Preview), and you can also just fullscreen an app already on the external. The question is reaching it again. Traced by code (`yabai_fullscreen_focus.sh`, `yabai_space_move.sh`, `yabai_workspace.sh`); two cases:

- **Non-WezTerm app fullscreen on the external (Preview, browser) → `hyper+3`…`hyper+9`. *High confidence, not live-verified.*** `yabai_fullscreen_focus.sh` lists *all* native-fullscreen windows across *all* displays (`sort_by(.display, .space)`), so an external one is in the ordinal list (after any laptop fullscreen apps — e.g. laptop reMarkable = `hyper+3`, external Preview = `hyper+4`). It then calls `yabai -m space --focus <idx>`. Confidence comes from: that same cross-display `space --focus` is *already* how `push` follows a space onto the external (`yabai_space_move.sh` line ~82), focusing a *fullscreen* Space is separately verified, and the reorder ignores it (only reorders *labeled* spaces; a fullscreen Preview is unlabeled). This case is clean.
- **WezTerm fullscreen on the external → `hyper+\`` (focus terminal). *Medium confidence.*** The follow hook keeps the `terminal` label on WezTerm and `focus terminal` does the same `space --focus`, so it should reach it. The murky bit is the **reorder on the external** with a *fullscreen* terminal: `yabai_reorder_spaces.sh` reserves the external's first space as scratch and would `--move` the fullscreen terminal into position — untested, most likely thing to misbehave.

**The single thing that can't be verified without the hardware:** whether `yabai -m space --focus <idx>` reliably lands focus on the *external* display in this setup. Rated high (standard yabai; `push` already depends on it), but if it ever fails, the one-line fix is a `yabai -m display --focus <display>` before the `space --focus` in `yabai_fullscreen_focus.sh`.

**30-second test on the next dock:** (1) connect external, fullscreen Preview, `hyper+\` to push it over; (2) return to the laptop and press `hyper+3` (or `+4` if reMarkable is also fullscreen) — should jump to Preview on the external; (3) bonus: fullscreen WezTerm, push it over, `hyper+\`` — should reach it (watch the reorder/scratch behavior).