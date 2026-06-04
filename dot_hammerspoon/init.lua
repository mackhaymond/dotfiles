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

hs.alert.show("Hammerspoon: Arc pin loaded")
