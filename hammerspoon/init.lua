-- Imperium Hammerspoon config
--
-- Primary purpose:
--   D06 Pro / dial input on macOS.
--
-- Karabiner/device mappings currently expected:
--   Left click    -> F18                  -> tap/hold state machine
--   Right click   -> Ctrl+Shift+F6        -> Wispr toggle passthrough + state tracking
--   Middle click  -> F17                  -> ". "
--   Dial CW       -> F13                  -> scroll down/up equivalent
--   Dial CCW      -> F14                  -> opposite scroll

-- ===== Key codes =====
local F18_CODE = 79  -- left click
local F17_CODE = 64  -- middle click
local F6_CODE  = 97  -- right click, observed with ctrl+shift
local F13_CODE = 105 -- dial clockwise
local F14_CODE = 107 -- dial counter-clockwise

-- ===== Timing config (seconds) =====
local TAP_THRESHOLD    = 0.200
local DOUBLE_TAP       = 0.500
local DICTATION_BUFFER = 1.000
local BYPASS_WINDOW    = 10.000

-- ===== Endpoints =====
-- Hammerspoon launched as a GUI app often does not inherit shell env.
local function trim(s)
    return tostring(s or ""):gsub("^%s+", ""):gsub("%s+$", "")
end

local function imperiumConfigValue(name)
    local cmd = [[/usr/bin/python3 - <<'PY'
import sys
sys.path.insert(0, "/Volumes/Imperium/Token-OS/cli-tools/lib")
try:
    import imperium_config
    print(getattr(imperium_config, "]] .. name .. [[", ""))
except Exception:
    print("")
PY]]
    local output = hs.execute(cmd)
    output = trim(output)
    if output ~= "" then return output end
    return nil
end

local TOKEN_API_URL = os.getenv("TOKEN_API_URL") or imperiumConfigValue("TOKEN_API_URL") or "http://localhost:7777"
local DISCORD_DAEMON_URL = os.getenv("DISCORD_DAEMON_URL") or "http://127.0.0.1:7779"

-- ===== State =====
local leftDownTime     = 0
local holdFired        = false
local lastTapTime      = 0
local enterQueued      = false
local dictationActive  = false
local dictationEndTime = 0
local bypassActive     = false
local bypassStartTime  = 0
local holdTimer        = nil
local bufferTimer      = nil
local inputTap          = nil
local wakeWatcher      = nil
local volumeWatcher    = nil
local dialEvents       = 0
local tapRestarts      = 0

-- ===== Helpers =====
local function now()
    return hs.timer.secondsSinceEpoch()
end

local function log(msg)
    print("[ImperiumHS] " .. msg)
end

local function sendEnter()
    hs.eventtap.keyStroke({}, "return")
end

local function sendPasteTranscript()
    hs.eventtap.keyStroke({"alt", "shift"}, "z")
end

local function sendPeriodSpace()
    hs.eventtap.keyStrokes(". ")
end

local function postDialScroll(delta)
    dialEvents = dialEvents + 1
    hs.eventtap.event.newScrollEvent({0, delta}, {}, "line"):post()
end

local function cancelHoldTimer()
    if holdTimer then
        holdTimer:stop()
        holdTimer = nil
    end
end

local function cancelBufferTimer()
    if bufferTimer then
        bufferTimer:stop()
        bufferTimer = nil
    end
end

local function restartInputTap(reason)
    tapRestarts = tapRestarts + 1
    if inputTap then
        inputTap:stop()
        inputTap:start()
        log("restarted input eventtap (" .. tostring(reason) .. ")")
    end
end

local function handleLeftTap()
    local t = now()

    -- 1. Dictation active -> queue Enter.
    if dictationActive then
        enterQueued = true
        log("Enter queued (dictation active)")
        return
    end

    -- 2. In buffer window after dictation ended -> queue until buffer expires.
    local sinceDictEnd = t - dictationEndTime
    if dictationEndTime > 0 and sinceDictEnd < DICTATION_BUFFER then
        enterQueued = true
        local remaining = DICTATION_BUFFER - sinceDictEnd
        cancelBufferTimer()
        bufferTimer = hs.timer.doAfter(remaining, function()
            if enterQueued then
                enterQueued = false
                sendEnter()
                log("queued Enter fired (buffer window)")
            end
        end)
        log(string.format("Enter queued (buffer window, %.0fms remaining)", remaining * 1000))
        return
    end

    -- 3. Single-tap bypass immediately after dictation ended.
    if bypassActive and (t - bypassStartTime) < BYPASS_WINDOW then
        bypassActive = false
        sendEnter()
        log("Enter sent (single-tap bypass)")
        return
    end

    -- 4. Double-tap detection.
    if (t - lastTapTime) < DOUBLE_TAP then
        lastTapTime = 0
        sendEnter()
        log("Enter sent (double-tap)")
        return
    end

    -- 5. First tap -> record, wait for second.
    lastTapTime = t
    log("first tap recorded")
end

-- Notify Discord daemon to deafen/undeafen bots during local dictation.
local function notifyDictationState(active)
    local payload = active and '{"deaf":true}' or '{"deaf":false}'

    hs.http.asyncPost(DISCORD_DAEMON_URL .. "/voice/deafen", payload,
        {["Content-Type"] = "application/json"},
        function(code, _body)
            if code ~= 200 then
                log("Discord daemon deafen call failed: " .. tostring(code))
            end
        end)

    hs.http.asyncPost(TOKEN_API_URL .. "/api/dictation?active=" .. (active and "true" or "false"), "",
        {["Content-Type"] = "application/json"},
        function(code, _body)
            if code ~= 200 then
                log("Token-API dictation call failed: " .. tostring(code))
            end
        end)
end

local function handleDictationToggle()
    dictationActive = not dictationActive

    if dictationActive then
        hs.alert.show("🎤 Dictation ON", 0.5)
        log("Dictation ON")
        notifyDictationState(true)
        return
    end

    dictationEndTime = now()
    bypassActive = true
    bypassStartTime = dictationEndTime
    hs.alert.show("🎤 Dictation OFF", 0.5)
    log("Dictation OFF; bypass window started")
    notifyDictationState(false)

    -- Fire queued Enter after buffer delay.
    if enterQueued then
        cancelBufferTimer()
        bufferTimer = hs.timer.doAfter(DICTATION_BUFFER, function()
            if enterQueued then
                enterQueued = false
                sendEnter()
                log("queued Enter fired (dictation ended)")
            end
        end)
    end
end

-- One eventtap handles both devices' F-key outputs. This is not treating the
-- ring and dial as the same hardware; it only keeps their shared macOS event
-- interception path cohesive. It also listens for macOS tap-disabled events so
-- recovery is event-driven, not polling.
local eventTypes = {
    hs.eventtap.event.types.keyDown,
    hs.eventtap.event.types.keyUp,
}
if hs.eventtap.event.types.tapDisabledByTimeout then
    table.insert(eventTypes, hs.eventtap.event.types.tapDisabledByTimeout)
end
if hs.eventtap.event.types.tapDisabledByUserInput then
    table.insert(eventTypes, hs.eventtap.event.types.tapDisabledByUserInput)
end

inputTap = hs.eventtap.new(eventTypes, function(e)
    local typ = e:getType()
    local types = hs.eventtap.event.types

    if typ == types.tapDisabledByTimeout or typ == types.tapDisabledByUserInput then
        log("eventtap disabled by macOS; scheduling restart")
        hs.timer.doAfter(0.1, function()
            restartInputTap("tap-disabled")
        end)
        return false
    end

    local keyCode = e:getKeyCode()

    -- F13/F14 dial -> macOS line scroll. Keep this path tiny; slow callbacks
    -- are a common reason macOS disables eventtaps.
    if typ == types.keyDown then
        if keyCode == F13_CODE then
            postDialScroll(3)
            return true
        elseif keyCode == F14_CODE then
            postDialScroll(-3)
            return true
        elseif keyCode == F17_CODE then
            sendPeriodSpace()
            log("middle click -> '. '")
            return true
        elseif keyCode == F6_CODE then
            local flags = e:getFlags()
            if flags.ctrl and flags.shift then
                handleDictationToggle()
                return false -- passthrough to Wispr
            end
        elseif keyCode == F18_CODE then
            leftDownTime = now()
            holdFired = false
            cancelHoldTimer()
            holdTimer = hs.timer.doAfter(TAP_THRESHOLD, function()
                if leftDownTime > 0 and not holdFired then
                    holdFired = true
                    sendPasteTranscript()
                    log("left hold -> paste transcript")
                end
            end)
            return true
        end
    elseif typ == types.keyUp then
        if keyCode == F18_CODE then
            cancelHoldTimer()
            local held = now() - leftDownTime
            leftDownTime = 0

            if not holdFired and held < TAP_THRESHOLD then
                handleLeftTap()
            end
            return true
        end
    end

    return false
end)

inputTap:start()

wakeWatcher = hs.caffeinate.watcher.new(function(event)
    local w = hs.caffeinate.watcher
    if event == w.systemDidWake or event == w.screensDidWake or event == w.sessionDidBecomeActive then
        hs.timer.doAfter(1, function()
            restartInputTap("wake/session")
        end)
    end
end)
wakeWatcher:start()

-- Reload when NAS comes back online. Canonical config lives there.
volumeWatcher = hs.fs.volume.new(function(event, info)
    if event == hs.fs.volume.didMount and info and info.path == "/Volumes/Imperium" then
        log("Imperium re-mounted; reloading")
        hs.timer.doAfter(2, hs.reload)
    end
end)
volumeWatcher:start()

-- Enable CLI: hs -c "ImperiumHammerspoonStatus()"
require("hs.ipc")

function _G.ImperiumHammerspoonStatus()
    return {
        inputTapEnabled = inputTap and inputTap:isEnabled() or false,
        dictationActive = dictationActive,
        enterQueued = enterQueued,
        dialEvents = dialEvents,
        tapRestarts = tapRestarts,
        tokenApiUrl = TOKEN_API_URL,
        discordDaemonUrl = DISCORD_DAEMON_URL,
    }
end

function _G.ImperiumHammerspoonRestartTaps()
    restartInputTap("manual")
    return _G.ImperiumHammerspoonStatus()
end

hs.notify.show("Imperium Input", "Hammerspoon loaded",
    "F13/F14 dial + ring state machine; event-driven recovery")
log("loaded; event-driven recovery only")
