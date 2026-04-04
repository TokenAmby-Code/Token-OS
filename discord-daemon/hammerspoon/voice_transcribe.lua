-- voice_transcribe.lua — Wispr Flow transcription bridge
-- Loaded by Hammerspoon init.lua
-- Exposes HTTP server on :7780 for daemon to trigger transcription
--
-- Flow: play audio → BlackHole → Wispr Flow → paste into sink → read → return

local M = {}

-- Config
local BLACKHOLE_DEVICE = "BlackHole 2ch"
local DICTATION_SHORTCUT = {{"ctrl", "shift"}, "f6"} -- Wispr Flow toggle (matches Karabiner ring mapping)
local PASTE_LAST_SHORTCUT = {{"alt", "shift"}, "z"}   -- Wispr "paste last transcript"
local SINK_APP = "TextEdit"
local HTTP_PORT = 7780
local PLAYBACK_BUFFER_MS = 500  -- extra wait after playback ends

-- State
local server = nil
local sinkWindow = nil
local transcribing = false

-- Logging
local function log(msg)
    print("[VoiceTranscribe] " .. msg)
end

-- Get or create the cursor sink (TextEdit window)
local function ensureSink()
    -- Look for existing TextEdit window titled "VoiceSink"
    local te = hs.application.find(SINK_APP)
    if te then
        for _, w in ipairs(te:allWindows()) do
            if w:title() == "VoiceSink" or w:title() == "VoiceSink.txt" then
                sinkWindow = w
                return sinkWindow
            end
        end
    end

    -- Create new TextEdit document
    log("Creating cursor sink...")
    hs.execute("open -a TextEdit")
    hs.timer.usleep(500000) -- 500ms for app to open

    te = hs.application.find(SINK_APP)
    if not te then
        log("ERROR: Could not open TextEdit")
        return nil
    end

    -- Create new document and save as VoiceSink
    local sinkPath = os.getenv("HOME") .. "/.discord-cli/VoiceSink.txt"
    hs.execute("touch '" .. sinkPath .. "'")
    hs.execute("open -a TextEdit '" .. sinkPath .. "'")
    hs.timer.usleep(500000)

    te = hs.application.find(SINK_APP)
    if te then
        for _, w in ipairs(te:allWindows()) do
            if w:title():find("VoiceSink") then
                sinkWindow = w
                -- Minimize it so it's out of the way
                -- w:minimize()
                log("Cursor sink ready: " .. w:title())
                return sinkWindow
            end
        end
    end

    log("WARNING: Sink window not found, using frontmost")
    return nil
end

-- Clear the sink content
local function clearSink()
    if not sinkWindow then return end
    sinkWindow:focus()
    hs.timer.usleep(100000) -- 100ms
    hs.eventtap.keyStroke({"cmd"}, "a") -- Select all
    hs.timer.usleep(50000)
    hs.eventtap.keyStroke({}, "delete")  -- Delete
    hs.timer.usleep(50000)
end

-- Read sink content
local function readSink()
    if not sinkWindow then return nil end
    sinkWindow:focus()
    hs.timer.usleep(100000)
    hs.eventtap.keyStroke({"cmd"}, "a") -- Select all
    hs.timer.usleep(50000)
    hs.eventtap.keyStroke({"cmd"}, "c") -- Copy
    hs.timer.usleep(100000)
    local text = hs.pasteboard.getContents()
    return text
end

-- Get audio duration in seconds using ffprobe
local function getAudioDuration(filepath)
    local cmd = string.format(
        "/opt/homebrew/bin/ffprobe -v quiet -show_entries format=duration -of csv=p=0 '%s' 2>/dev/null",
        filepath
    )
    local output = hs.execute(cmd)
    if output then
        local duration = tonumber(output:match("([%d%.]+)"))
        return duration or 2.0
    end
    return 2.0 -- fallback
end

-- Play audio file through BlackHole using play-to-device Swift utility
local PLAY_TO_DEVICE = "/Volumes/Imperium/Token-OS/discord-daemon/play-to-device"

local function playToBlackHole(filepath, callback)
    -- Convert PCM to WAV first if needed (play-to-device needs WAV)
    local actualPath = filepath
    local ext = filepath:match("%.(%w+)$")
    if ext == "pcm" then
        actualPath = filepath:gsub("%.pcm$", ".wav")
        local convertCmd = string.format(
            "/opt/homebrew/bin/ffmpeg -y -f s16le -ar 48000 -ac 1 -i '%s' '%s' 2>/dev/null",
            filepath, actualPath
        )
        hs.execute(convertCmd)
    end

    log("Playing audio → BlackHole: " .. actualPath)
    hs.task.new(PLAY_TO_DEVICE, function(exitCode, stdOut, stdErr)
        log("Playback finished (exit " .. tostring(exitCode) .. ")")
        if callback then callback() end
    end, {BLACKHOLE_DEVICE, actualPath}):start()
end

-- Main transcription flow
local function transcribeAudio(filepath, responseCallback)
    if transcribing then
        log("Already transcribing, rejecting")
        responseCallback('{"error":"already_transcribing"}', 429)
        return
    end

    transcribing = true
    log("Starting transcription: " .. filepath)

    -- Step 1: Ensure sink exists and is clear
    ensureSink()
    clearSink()

    -- Step 2: Get audio duration for timing
    local duration = getAudioDuration(filepath)
    log(string.format("Audio duration: %.2fs", duration))

    -- Step 3: Start Wispr dictation
    log("Starting Wispr dictation...")
    hs.eventtap.keyStroke(DICTATION_SHORTCUT[1], DICTATION_SHORTCUT[2])
    hs.timer.usleep(300000) -- 300ms for Wispr to start listening

    -- Step 4: Play audio through BlackHole
    playToBlackHole(filepath, function()
        -- Step 5: Wait for playback buffer
        hs.timer.doAfter(PLAYBACK_BUFFER_MS / 1000, function()

            -- Step 6: Focus sink window
            if sinkWindow then
                sinkWindow:focus()
                hs.timer.usleep(200000) -- 200ms
            end

            -- Step 7: Stop Wispr dictation (triggers auto-paste at cursor)
            log("Stopping Wispr dictation...")
            hs.eventtap.keyStroke(DICTATION_SHORTCUT[1], DICTATION_SHORTCUT[2])

            -- Step 8: Wait for Wispr to process and paste
            hs.timer.doAfter(1.5, function()
                -- Step 9: Read sink content
                local text = readSink()

                if not text or text:match("^%s*$") then
                    -- Fallback: try "paste last transcript"
                    log("Sink empty, trying paste-last-transcript fallback...")
                    clearSink()
                    if sinkWindow then sinkWindow:focus() end
                    hs.timer.usleep(200000)
                    hs.eventtap.keyStroke(PASTE_LAST_SHORTCUT[1], PASTE_LAST_SHORTCUT[2])
                    hs.timer.usleep(500000)
                    text = readSink()
                end

                transcribing = false

                if text and not text:match("^%s*$") then
                    text = text:gsub("^%s+", ""):gsub("%s+$", "")
                    log("Transcription: \"" .. text .. "\"")
                    local json = string.format('{"text":"%s","duration":%.2f}',
                        text:gsub('"', '\\"'):gsub("\n", "\\n"), duration)
                    responseCallback(json, 200)
                else
                    log("No transcription captured")
                    responseCallback('{"text":"","error":"no_transcription"}', 200)
                end
            end)
        end)
    end)
end

-- HTTP Server
function M.start()
    server = hs.httpserver.new(false, false)
    server:setPort(HTTP_PORT)
    server:setCallback(function(method, path, headers, body)
        log(method .. " " .. path)

        if path == "/status" then
            local status = string.format(
                '{"ready":true,"transcribing":%s,"sink":%s}',
                transcribing and "true" or "false",
                sinkWindow and "true" or "false"
            )
            return status, 200, {["Content-Type"] = "application/json"}
        end

        if path == "/transcribe" and method == "POST" then
            local ok, data = pcall(hs.json.decode, body)
            if not ok or not data or not data.audio_path then
                return '{"error":"audio_path required"}', 400, {["Content-Type"] = "application/json"}
            end

            -- Check file exists
            local f = io.open(data.audio_path, "r")
            if not f then
                return '{"error":"file not found"}', 404, {["Content-Type"] = "application/json"}
            end
            f:close()

            -- Async transcription — we need to respond synchronously for hs.httpserver
            -- So we'll use a polling approach: start transcription, return job ID
            local jobId = tostring(hs.timer.secondsSinceEpoch())
            local result = nil
            local resultCode = nil

            transcribeAudio(data.audio_path, function(json, code)
                result = json
                resultCode = code
                -- Write result to temp file for polling
                local resultPath = os.getenv("HOME") .. "/.discord-cli/audio/result-" .. jobId .. ".json"
                local rf = io.open(resultPath, "w")
                if rf then
                    rf:write(json)
                    rf:close()
                end
            end)

            local resp = string.format('{"job_id":"%s","status":"processing"}', jobId)
            return resp, 202, {["Content-Type"] = "application/json"}
        end

        if path:match("^/result/") and method == "GET" then
            local jobId = path:match("^/result/(.+)$")
            local resultPath = os.getenv("HOME") .. "/.discord-cli/audio/result-" .. jobId .. ".json"
            local f = io.open(resultPath, "r")
            if f then
                local content = f:read("*a")
                f:close()
                os.remove(resultPath)
                return content, 200, {["Content-Type"] = "application/json"}
            else
                return '{"status":"processing"}', 202, {["Content-Type"] = "application/json"}
            end
        end

        return '{"error":"not found"}', 404, {["Content-Type"] = "application/json"}
    end)

    server:start()
    log("HTTP server started on port " .. HTTP_PORT)

    -- Pre-create sink
    ensureSink()
end

function M.stop()
    if server then
        server:stop()
        server = nil
        log("HTTP server stopped")
    end
end

return M
