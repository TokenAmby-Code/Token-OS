-- D06 Pro Ring Mouse - Hammerspoon State Machine
-- Karabiner remaps ring buttons (device-filtered, vendor 9354:1025):
--   Left click   → F18  → Hammerspoon state machine
--   Right click   → Ctrl+Shift+F6 → Wispr (passthrough, observed for dictation tracking)
--   Middle click  → F17  → Period + Space

-- Key codes
local F18_CODE = 79  -- left click
local F17_CODE = 64  -- middle click
local F6_CODE  = 97  -- right click (observed only)
local F13_CODE = 105 -- dial clockwise
local F14_CODE = 107 -- dial counter-clockwise

-- Config (seconds)
local TAP_THRESHOLD    = 0.200
local DOUBLE_TAP       = 0.500
local DICTATION_BUFFER = 1.000
local BYPASS_WINDOW    = 10.000

-- State
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

-- Helpers
local function now() return hs.timer.secondsSinceEpoch() end

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

local function handleLeftTap()
    local t = now()

    -- 1. Dictation active → queue Enter
    if dictationActive then
        enterQueued = true
        print("[Ring] Enter queued (dictation active)")
        return
    end

    -- 2. In buffer window (< 1s after dictation ended) → queue with remaining delay
    local sinceDictEnd = t - dictationEndTime
    if dictationEndTime > 0 and sinceDictEnd < DICTATION_BUFFER then
        enterQueued = true
        local remaining = DICTATION_BUFFER - sinceDictEnd
        cancelBufferTimer()
        bufferTimer = hs.timer.doAfter(remaining, function()
            if enterQueued then
                enterQueued = false
                sendEnter()
                print("[Ring] Queued Enter fired (buffer window)")
            end
        end)
        print(string.format("[Ring] Enter queued (buffer window, %.0fms remaining)", remaining * 1000))
        return
    end

    -- 3. Bypass active (< 10s after dictation ended) → immediate Enter
    if bypassActive and (t - bypassStartTime) < BYPASS_WINDOW then
        bypassActive = false
        sendEnter()
        print("[Ring] Enter sent (single-tap bypass)")
        return
    end

    -- 4. Double-tap detection
    if (t - lastTapTime) < DOUBLE_TAP then
        lastTapTime = 0
        sendEnter()
        print("[Ring] Enter sent (double-tap)")
        return
    end

    -- 5. First tap → record, wait for second
    lastTapTime = t
    print("[Ring] First tap recorded")
end

-- F18 listener: keyDown + keyUp for mod-tap
local leftTap = hs.eventtap.new({hs.eventtap.event.types.keyDown, hs.eventtap.event.types.keyUp}, function(e)
    if e:getKeyCode() ~= F18_CODE then return false end

    if e:getType() == hs.eventtap.event.types.keyDown then
        leftDownTime = now()
        holdFired = false
        cancelHoldTimer()
        holdTimer = hs.timer.doAfter(TAP_THRESHOLD, function()
            if leftDownTime > 0 and not holdFired then
                holdFired = true
                sendPasteTranscript()
                print("[Ring] Hold → Cmd+Ctrl+V (paste transcript)")
            end
        end)
        return true
    end

    if e:getType() == hs.eventtap.event.types.keyUp then
        cancelHoldTimer()
        local held = now() - leftDownTime
        leftDownTime = 0

        if not holdFired and held < TAP_THRESHOLD then
            handleLeftTap()
        end
        return true
    end

    return false
end)

-- Notify Discord daemon to deafen/undeafen bots during dictation
local function notifyDictationState(active)
    local payload = active and '{"deaf":true}' or '{"deaf":false}'
    -- Fire-and-forget to Discord daemon (port 7779)
    hs.http.asyncPost("http://127.0.0.1:7779/voice/deafen", payload,
        {["Content-Type"] = "application/json"},
        function(code, body)
            if code ~= 200 then
                print("[Ring] Daemon deafen call failed: " .. tostring(code))
            else
                print("[Ring] Daemon " .. (active and "deafened" or "undeafened"))
            end
        end)
    -- Also notify Token-API for state tracking
    local apiPayload = active and "true" or "false"
    hs.http.asyncPost("http://127.0.0.1:7777/api/dictation?active=" .. apiPayload, "",
        {["Content-Type"] = "application/json"},
        function(code, body)
            if code ~= 200 then
                print("[Ring] Token-API dictation call failed: " .. tostring(code))
            end
        end)
end

-- F6 observer: track dictation toggle (Ctrl+Shift+F6 from Karabiner)
local dictationObserver = hs.eventtap.new({hs.eventtap.event.types.keyDown}, function(e)
    if e:getKeyCode() ~= F6_CODE then return false end

    local flags = e:getFlags()
    if not (flags.ctrl and flags.shift) then return false end

    dictationActive = not dictationActive

    if dictationActive then
        hs.alert.show("🎤 Dictation ON", 0.5)
        print("[Ring] Dictation ON")
        notifyDictationState(true)
    else
        dictationEndTime = now()
        bypassActive = true
        bypassStartTime = dictationEndTime
        hs.alert.show("🎤 Dictation OFF", 0.5)
        print("[Ring] Dictation OFF — bypass window started")
        notifyDictationState(false)

        -- Fire queued Enter after buffer delay
        if enterQueued then
            cancelBufferTimer()
            bufferTimer = hs.timer.doAfter(DICTATION_BUFFER, function()
                if enterQueued then
                    enterQueued = false
                    sendEnter()
                    print("[Ring] Queued Enter fired (dictation ended)")
                end
            end)
        end
    end

    return false  -- passthrough to Wispr
end)

-- F17 listener: middle click → period + space
local middleTap = hs.eventtap.new({hs.eventtap.event.types.keyDown}, function(e)
    if e:getKeyCode() ~= F17_CODE then return false end
    sendPeriodSpace()
    print("[Ring] Middle click → '. '")
    return true
end)

-- F13/F14 listener: dial → mac scroll
local dialScrollTap = hs.eventtap.new({hs.eventtap.event.types.keyDown}, function(e)
    local keyCode = e:getKeyCode()
    if keyCode == F13_CODE then
        postDialScroll(3)
        return true
    end

    if keyCode == F14_CODE then
        postDialScroll(-3)
        return true
    end

    return false
end)

-- Start all listeners
leftTap:start()
dictationObserver:start()
middleTap:start()
dialScrollTap:start()

-- Watchdog: macOS disables eventtaps after timeout / CPU starvation / sleep wake.
-- Without this, F13/F14 dial scroll silently dies and only `hs.reload()` revives it.
-- Poll every 15s and resurrect any tap that has been killed.
local watchedTaps = {
    {name = "leftTap",          tap = leftTap},
    {name = "dictationObserver", tap = dictationObserver},
    {name = "middleTap",         tap = middleTap},
    {name = "dialScrollTap",     tap = dialScrollTap},
}
local tapWatchdog = hs.timer.doEvery(15, function()
    for _, entry in ipairs(watchedTaps) do
        if not entry.tap:isEnabled() then
            entry.tap:start()
            print("[Ring] Watchdog: restarted dead eventtap (" .. entry.name .. ")")
        end
    end
end)

-- Reload when NAS comes back online (canonical config lives there; stale dofile
-- references survive in memory, but a clean reload keeps the surface honest).
hs.fs.volume.new(function(event, info)
    if event == hs.fs.volume.didMount and info and info.path == "/Volumes/Imperium" then
        print("[Ring] Imperium re-mounted — reloading")
        hs.timer.doAfter(2, hs.reload)
    end
end):start()

hs.notify.show("D06 Pro Ring", "State machine loaded",
    "L:tap=Enter(2x) hold=paste | M:'. ' | R:dictation | watchdog 15s")
print("D06 Pro Ring state machine loaded (watchdog active)")

-- ===== Voice Transcription Bridge =====
-- Exposes HTTP server for daemon → Wispr Flow transcription
require("hs.ipc")  -- Enable CLI: hs -c "command"
local voiceTranscribe = require("voice_transcribe")
voiceTranscribe.start()
print("Voice transcription bridge loaded (port 7780)")
