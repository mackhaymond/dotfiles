-- Minimal Hammerspoon config (replaces the old, unused Spacehammer setup).
--
-- Job: manage Arc browser windows. yabai handles everything else, but it cannot
-- read AXIdentifier, which is the ONLY reliable way to tell a "Little Arc" popup
-- (littleBrowserWindow-*) apart from a main browser window (bigBrowserWindow-*) --
-- they are byte-identical in every yabai field, including title. So Hammerspoon
-- reads AXIdentifier and FLOATS Little Arc / PiP popups in yabai (stopping them
-- from tiling and from being mistaken for a main window). Once the popups float,
-- yabai_arc_pin.sh pins the remaining (non-floating) main windows to main/school.
--
-- macOS AX only exposes windows on the CURRENT Space, but that is fine: a Little
-- Arc popup is always created on (and a window becomes visible on) the current
-- Space, so we catch each one the moment it is reachable.

require("hs.ipc") -- enables the `hs` command-line tool

local YABAI = "/opt/homebrew/bin/yabai"
local ARC_PIN = os.getenv("HOME") .. "/code/various_scripts/yabai_arc_pin.sh"

local function sh(cmd) return hs.execute(cmd .. " 2>/dev/null") or "" end
local function yabai(args) return sh(YABAI .. " -m " .. args) end

-- Arc window kind from AXIdentifier: "main" | "little" | "other" | "unknown".
local function arcKind(win)
  local ax = hs.axuielement.windowElement(win)
  local id = ax and ax:attributeValue("AXIdentifier")
  if type(id) ~= "string" then return "unknown" end
  if id:sub(1, 19) == "littleBrowserWindow" then return "little" end
  if id:sub(1, 16) == "bigBrowserWindow" then return "main" end
  return "other"
end

local function isFloating(id)
  local ok, t = pcall(hs.json.decode, yabai("query --windows --window " .. id))
  if ok and t then return t["is-floating"] == true end
  return nil
end

-- Float a Little Arc / popup in yabai and shrink it to a centered popup (yabai may
-- already have tiled it to full size). Only acts on the transition to floating, so
-- it never re-grabs a popup the user has since moved/resized.
local function floatPopup(win)
  local id = win:id()
  if isFloating(id) == false then
    yabai("window " .. id .. " --toggle float")
    local scr = win:screen()
    if scr then
      local f = scr:frame()
      local w, h = math.floor(f.w * 0.5), math.floor(f.h * 0.7)
      win:setFrame({ x = f.x + (f.w - w) / 2, y = f.y + (f.h - h) / 2, w = w, h = h })
    end
  end
end

local function repin() sh(ARC_PIN) end

local function handle(win)
  if not win then return end
  local app = win:application()
  if not app or app:name() ~= "Arc" then return end
  local kind = arcKind(win)
  if kind == "little" then
    floatPopup(win)
    repin() -- the popup is now floating -> safe to (re)pin the real windows
  elseif kind == "main" then
    repin()
  end
end

local arcWF = hs.window.filter.new({ "Arc" })
arcWF:subscribe(hs.window.filter.windowCreated, function(win)
  hs.timer.doAfter(0.2, function() handle(win) end) -- let yabai register it first
end)
arcWF:subscribe(hs.window.filter.windowVisible, function(win)
  hs.timer.doAfter(0.2, function() handle(win) end) -- catch windows as Spaces are visited
end)

-- Initial pass shortly after load.
hs.timer.doAfter(1.0, function()
  for _, w in ipairs(hs.window.allWindows()) do
    local app = w:application()
    if app and app:name() == "Arc" then handle(w) end
  end
end)

-- Debug helper: `hs -c "print(arcDiag())"`.
function arcDiag()
  local out = {}
  for _, w in ipairs(hs.window.allWindows()) do
    local app = w:application()
    if app and app:name() == "Arc" then
      out[#out + 1] = string.format("hsid=%s kind=%s floating=%s title=[%s]",
        tostring(w:id()), arcKind(w), tostring(isFloating(w:id())), tostring(w:title()))
    end
  end
  return table.concat(out, "\n")
end

hs.alert.show("Hammerspoon: Arc manager loaded")
