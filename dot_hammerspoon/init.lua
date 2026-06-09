-- Minimal Hammerspoon config (replaces the old, unused Spacehammer setup).
--
-- Job: pin the two MAIN Arc browser windows to the `main` and `school` yabai
-- spaces (one each, title-independent), while leaving everything else about Arc --
-- including "Little Arc" popups -- as a normal MANAGED window (tiled, in the
-- stack, cyclable). Little Arc is NOT floated and NOT pinned; it just lives
-- wherever it opens.
--
-- Why Hammerspoon at all: a Little Arc popup is byte-identical to a main window in
-- every yabai field (subrole, floating, even title is the page title), so yabai
-- cannot tell them apart. The ONLY reliable discriminator is AXIdentifier
-- (bigBrowserWindow-* vs littleBrowserWindow-*), which yabai can't read but
-- Hammerspoon can. macOS AX only exposes CURRENT-Space windows, so we read the
-- identifier off whatever Arc windows are currently on-screen and remember which
-- window ids are "main"; the remembered set is then pinned via yabai (which can
-- report any window's space by id, cross-Space).
--
-- TRIGGER: yabai calls `hs -c "arcSync()"` from the SAME signals that re-apply the
-- other pinned apps' space= rules -- application_launched (any app launch) and Arc
-- window_created -- so Arc snaps to its spaces on the same cadence as every other
-- pinned app. No polling.

require("hs.ipc") -- enables the `hs` command-line tool

local YABAI = "/opt/homebrew/bin/yabai"
local MAIN_LABEL = "main"
local SCHOOL_LABEL = "school"

-- Remembered ids of Arc MAIN (bigBrowserWindow) windows. Accumulated as we see
-- them on the current Space; pruned when they close.
mainSet = mainSet or {}

local function sh(cmd) return hs.execute(cmd .. " 2>/dev/null") or "" end
local function yabai(args) return sh(YABAI .. " -m " .. args) end
local function decode(s)
  local ok, t = pcall(hs.json.decode, s)
  if ok then return t end
  return nil
end

-- Arc window kind from an AXIdentifier string: "main" | "little" | "other" |
-- "unknown" (non-string id). SINGLE source of truth for the prefix mapping -- both
-- arcKind() and classifyCurrent() route through it so the two can never drift in
-- prefix order or sentinel value.
local function arcKindFromId(id)
  if type(id) ~= "string" then return "unknown" end
  if id:sub(1, 16) == "bigBrowserWindow" then return "main" end
  if id:sub(1, 19) == "littleBrowserWindow" then return "little" end
  return "other"
end

-- Kind of an hs.window via its AX element (used by the focused-window guard helper).
local function arcKind(win)
  local ax = hs.axuielement.windowElement(win)
  return arcKindFromId(ax and ax:attributeValue("AXIdentifier"))
end

local function labelIndex(label)
  local t = decode(yabai("query --spaces --space " .. label))
  return t and t.index or nil
end

-- Arc's AX windows (current Space only) read straight off the application's AX
-- element. This is FAST (~ms) -- unlike hs.window.allWindows(), which enumerates
-- EVERY app's windows via AX and takes ~2s on this machine.
local function arcAxWindows()
  local arc = hs.application.find("Arc")
  if not arc then return {} end
  local appEl = hs.axuielement.applicationElement(arc)
  return (appEl and appEl:attributeValue("AXWindows")) or {}
end

-- Classify whatever Arc windows are currently AX-visible (current Space) and fold
-- them into mainSet. Cheap; only touches what is on-screen now. A non-string id
-- yields "unknown", which matches NEITHER branch below -- a deliberate no-op that
-- preserves a window's prior classification across a transient AX read miss (do not
-- "simplify" the elseif into a plain else that would nil a known main on a miss).
local function classifyCurrent()
  for _, axw in ipairs(arcAxWindows()) do
    local kind = arcKindFromId(axw:attributeValue("AXIdentifier"))
    local hw = axw.asHSWindow and axw:asHSWindow()
    if hw then
      if kind == "main" then mainSet[hw:id()] = true
      elseif kind == "little" or kind == "other" then mainSet[hw:id()] = nil end
    end
  end
end

-- Pin the remembered main windows to main/school, one each, stably. Never moves a
-- window already on a target; only fills an empty target from off-target/surplus
-- windows. Fullscreen main windows are left alone (reachable via hyper+3-9, they
-- return to their space on exit). Touches ONLY ids in mainSet, so Little Arc and
-- every other Arc window are never moved.
local function pinMains()
  local mIdx, sIdx = labelIndex(MAIN_LABEL), labelIndex(SCHOOL_LABEL)
  if not mIdx or not sIdx then return end
  local all = decode(yabai("query --windows"))
  if not all then return end
  local byId = {}
  for _, w in ipairs(all) do byId[w.id] = w end

  local onMain, onSchool, pool = {}, {}, {}
  for id in pairs(mainSet) do
    local w = byId[id]
    if not w then
      mainSet[id] = nil -- window is gone
    elseif not w["is-native-fullscreen"] then
      if w.space == mIdx then onMain[#onMain + 1] = id
      elseif w.space == sIdx then onSchool[#onSchool + 1] = id
      else pool[#pool + 1] = id end
    end
  end
  for i = 2, #onMain do pool[#pool + 1] = onMain[i] end
  for i = 2, #onSchool do pool[#pool + 1] = onSchool[i] end

  if #onMain == 0 and #pool > 0 then
    yabai("window " .. table.remove(pool, 1) .. " --space " .. MAIN_LABEL)
  end
  if #onSchool == 0 and #pool > 0 then
    yabai("window " .. table.remove(pool, 1) .. " --space " .. SCHOOL_LABEL)
  end
end

-- arcSync (and the debug/guard helpers arcFocusedKind/pinNow/arcDiag below) are
-- GLOBAL functions so they are reachable via `hs -c "..."` IPC, which evaluates in
-- the global environment -- a `local function` would be invisible to it. mainSet is
-- likewise global so it persists across the separate `hs -c` invocations that share
-- this Lua state; making it local would reset cross-Space main tracking each call.
function arcSync()
  classifyCurrent()
  pinMains()
end

-- One-shot initial pass after load: classify whatever Arc windows are on-screen
-- now and pin. All ongoing re-pinning is driven by yabai signals calling
-- `hs -c "arcSync()"` (see yabairc: application_launched + Arc window_created).
hs.timer.doAfter(1.0, arcSync)

-- Kind of the currently FOCUSED window ("main" | "little" | "other" | "none" |
-- "notarc"). Kept for an OPTIONAL main-only force-move guard in yabai_send_window.sh;
-- the shipped guard instead uses a pure-yabai main/school space check (the AX
-- focused-window races with yabai's focus), so this is currently unreferenced. Fast:
-- only inspects the one focused window.
function arcFocusedKind()
  local w = hs.window.focusedWindow()
  if not w then return "none" end
  local app = w:application()
  if not app or app:name() ~= "Arc" then return "notarc" end
  return arcKind(w)
end

-- Debug helpers.
function pinNow() pinMains() end
function arcDiag()
  local set = {}
  for id in pairs(mainSet) do set[#set + 1] = tostring(id) end
  local out = { "mainSet: " .. table.concat(set, ",") }
  for _, w in ipairs(hs.window.allWindows()) do
    local app = w:application()
    if app and app:name() == "Arc" then
      out[#out + 1] = string.format("hsid=%s kind=%s title=[%s]", tostring(w:id()), arcKind(w), tostring(w:title()))
    end
  end
  return table.concat(out, "\n")
end

-- ============================================================================
-- System-wide keybind help overlay (skhd binds hyper+? -> `hs -c "yabaiHelpToggle()"`).
--
-- A non-interactive hs.canvas HUD drawn on ALL spaces (canJoinAllSpaces), above
-- normal windows, that NEVER takes keyboard focus -- so the same hyper+? that shows
-- it also reaches skhd to toggle it back off. Toggling deletes/rebuilds the canvas,
-- so it always re-centers on the screen under the mouse (laptop or external).
--
-- yabaiHelpToggle + yabaiHelpCanvas are GLOBAL (reachable via `hs -c`, and persistent
-- across the separate IPC invocations -- same rationale as arcSync/mainSet above).
--
-- These HELP_COL tables are a HAND-MAINTAINED MIRROR of the keybinds (no auto-gen).
-- The SAME bind list lives in THREE files that MUST stay in sync:
--   1. ~/.config/skhd/skhdrc  (chezmoi: dot_config/skhd/skhdrc.tmpl)  <- SOURCE OF TRUTH
--   2. THIS overlay           (HELP_COL1 / HELP_COL2 / HELP_COL3 below)
--   3. dot_config/yabai/README.md  -- section 3.2 tables AND section 6 cheat sheet
-- Change a bind in only one place and the on-screen help LIES. When skhdrc changes,
-- re-mirror the affected HELP_COL row(s) here and update both README sections.
-- ============================================================================

local HELP_HEADER = { red = 1.00, green = 0.62, blue = 0.22, alpha = 1.0 }
local HELP_KEY    = { white = 0.97 }
local HELP_DESC   = { white = 0.72 }
local HELP_BODY   = 14   -- single source for header/key/desc point size (Menlo, monospace)

local function helpSeg(text, color, size, bold)
  return hs.styledtext.new(text, {
    font  = { name = bold and "Menlo-Bold" or "Menlo", size = size },
    color = color,
  })
end

-- Build one styledtext column from a list of rows:
--   { h = "HEADER" }          section header (orange, bold)
--   { gap = true }            vertical spacer
--   { k = "key", d = "desc" } a bind row (monospace-aligned key + description)
local function helpColumn(rows)
  local s = hs.styledtext.new("")
  for _, r in ipairs(rows) do
    if r.h then
      s = s .. helpSeg(r.h .. "\n", HELP_HEADER, HELP_BODY, true)
    elseif r.gap then
      s = s .. helpSeg("\n", HELP_DESC, 9)
    else
      s = s .. helpSeg(string.format("%-12s", r.k), HELP_KEY, HELP_BODY)
           .. helpSeg("  " .. r.d .. "\n", HELP_DESC, HELP_BODY)
    end
  end
  return s
end

-- MIRROR of ~/.config/skhd/skhdrc -- also update README §3.2 + §6 when editing rows.
local HELP_COL1 = {
  { h = "FOCUS SPACE  ·  hyper +" },
  { k = "`",        d = "terminal" },
  { k = "1   2",    d = "main · school" },
  { k = "tab  q",   d = "todo · schedule" },
  { k = "w   e",    d = "mail · calendar" },
  { k = "d   f",    d = "messages · ai" },
  { k = "esc",      d = "codex" },
  { gap = true },
  { h = "SEND WINDOW  ·  hyper+fn +" },
  { k = "same keys", d = "move window here + follow" },
  { k = "esc",       d = "send to codex" },
  { gap = true },
  { h = "FULLSCREEN APPS  ·  hyper +" },
  { k = "3 … 9",     d = "focus Nth fullscreen app" },
}

local HELP_COL2 = {
  { h = "BSP — MOVE / FOCUS  ·  hyper +" },
  { k = "h j k l",    d = "focus  ← ↓ ↑ →" },
  { k = "fn+h/j/k/l", d = "swap   ← ↓ ↑ →" },
  { gap = true },
  { h = "BSP — SIZE / SHAPE  ·  hyper +" },
  { k = "[   ]",      d = "narrower / wider" },
  { k = ";   '",      d = "shorter / taller" },
  { k = "b",          d = "balance splits" },
  { k = "v",          d = "split orientation  H ↔ V" },
  { k = "n",          d = "rotate tree 90°" },
  { k = "m",          d = "maximize (zoom)" },
  { k = "t",          d = "float / unfloat window" },
  { k = "fn+m",       d = "native fullscreen" },
  { k = "fn+b",       d = "toggle  bsp ↔ stack" },
}

local HELP_COL3 = {
  { h = "STACK / MIRROR  ·  hyper +" },
  { k = "z   x",      d = "stack: focus next / prev" },
  { k = "",           d = "bsp:   mirror H / V" },
  { k = "",           d = "(one key, layout-aware)" },
  { gap = true },
  { h = "DISPLAY & SPACES  ·  hyper +" },
  { k = "\\",         d = "push space to other display" },
  { k = "0",          d = "pull all spaces home" },
  { k = "g",          d = "focus ext" },
  { k = "fn+g",       d = "fling window to ext" },
  { k = "F1  F2",     d = "focus laptop / external" },
  { gap = true },
  { h = "SYSTEM" },
  { k = "hyper",       d = "= caps lock = ⌘⌃⌥⇧" },
  { k = "hyper+fn+?",  d = "toggle this help" },
  { k = "esc",         d = "close this help" },
}

local function buildHelpCanvas()
  local screen = hs.mouse.getCurrentScreen() or hs.screen.mainScreen()
  if not screen then return nil end   -- no active display (hot-plug/sleep-wake race); skip this toggle
  local sf = screen:frame()
  local W, H = 1120, 410
  local x = sf.x + (sf.w - W) / 2
  local y = sf.y + (sf.h - H) / 2
  local c = hs.canvas.new({ x = x, y = y, w = W, h = H })
  c:level(hs.canvas.windowLevels.overlay)
  c:behavior({ "canJoinAllSpaces", "stationary" })
  c:clickActivating(false)
  c:appendElements({
    { type = "rectangle", action = "fill",
      roundedRectRadii = { xRadius = 18, yRadius = 18 },
      fillColor = { red = 0.04, green = 0.05, blue = 0.07, alpha = 0.95 } },
    { type = "rectangle", action = "stroke", strokeWidth = 2,
      roundedRectRadii = { xRadius = 18, yRadius = 18 },
      strokeColor = { red = 1.0, green = 0.62, blue = 0.22, alpha = 0.9 } },
    { type = "text",
      text = helpSeg("yabai · skhd  —  keybindings", { white = 1.0 }, 20, true),
      frame = { x = 40, y = 20, w = W - 80, h = 30 } },
    -- thin orange divider under the title to anchor the header band
    { type = "rectangle", action = "fill",
      fillColor = { red = 1.0, green = 0.62, blue = 0.22, alpha = 0.32 },
      frame = { x = 40, y = 55, w = W - 80, h = 1.5 } },
    { type = "text", text = helpColumn(HELP_COL1), frame = { x = 40,  y = 68, w = 330, h = H - 84 } },
    { type = "text", text = helpColumn(HELP_COL2), frame = { x = 400, y = 68, w = 330, h = H - 84 } },
    { type = "text", text = helpColumn(HELP_COL3), frame = { x = 760, y = 68, w = 330, h = H - 84 } },
  })
  return c
end

-- Esc-to-close: a plain-Esc hotkey enabled ONLY while the overlay is showing, so
-- Esc dismisses the HUD but behaves normally everywhere else (disabled when hidden).
local helpEscHotkey = nil

local function yabaiHelpHide()
  if yabaiHelpCanvas then
    yabaiHelpCanvas:hide()   -- hides instantly; dropping the ref lets GC reclaim it
    yabaiHelpCanvas = nil     -- (explicit :delete() is deprecated for hs.canvas)
  end
  if helpEscHotkey then helpEscHotkey:disable() end
end

-- Toggle: hide if showing, otherwise (re)build + show and arm the Esc-to-close key.
function yabaiHelpToggle()
  if yabaiHelpCanvas then
    yabaiHelpHide()
    return
  end
  local c = buildHelpCanvas()
  if not c then return end   -- screen detection failed mid display-reconfig; no-op (next press works)
  yabaiHelpCanvas = c
  yabaiHelpCanvas:show()
  helpEscHotkey = helpEscHotkey or hs.hotkey.new({}, "escape", yabaiHelpHide)
  helpEscHotkey:enable()
end

hs.alert.show("Hammerspoon: Arc pin + keybind help loaded")
