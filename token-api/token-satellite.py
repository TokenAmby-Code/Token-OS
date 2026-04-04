"""
Token Satellite: Windows companion server for token-api.

Stateless FastAPI app running on WSL (port 7777). Executes Windows-side
commands on behalf of token-api — same pattern as the phone's MacroDroid
HTTP server.

Endpoints:
    GET  /health           — heartbeat
    POST /enforce          — close a Windows process (brave, minecraft)
    GET  /processes        — list distraction-relevant processes
    POST /tts/speak        — speak text via Windows SAPI direct (blocking, for voice-chat)
    POST /tts/skip         — skip current TTS speech
    POST /tts/synthesize   — synthesize text to WAV file (blocking)
    POST /tts/control      — transport controls: pause/resume/stop (non-blocking)
    POST /tts/synth-and-play — synthesize to WAV + speak via SAPI (blocking, for queued TTS)
    GET  /tts/status       — current TTS engine state
    POST /ahk/execute      — execute a one-shot AHK v2 script
    POST /restart          — git pull + restart
    GET  /kvm/status       — DeskFlow watchdog state
    POST /kvm/control      — manual DeskFlow start/stop/hold
    GET  /files/read       — read a file under ~/.claude/ (for cross-machine transcript fetch)
    POST /tmux/send-keys   — send a command to a tmux pane (cross-machine dispatch)
"""

import glob
import json
import os
import subprocess
import logging
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests as http_requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger("token_satellite")

app = FastAPI(title="Token Satellite", version="1.0.0")

# Full paths — bare exes aren't on PATH under systemd
CMD_EXE = "/mnt/c/Windows/System32/cmd.exe"
POWERSHELL_EXE = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
AHK_EXE = "/mnt/c/Program Files/AutoHotkey/v2/AutoHotkey.exe"
AHK_SCRIPTS_DIR = Path.home() / "Scripts" / "ahk"

# PowerShell script for persistent TTS engine.
# Uses SpeakAsync so the main loop stays responsive to skip/poll/pause/resume commands.
# Also supports file-based synthesis (SetOutputToWaveFile) for replay/persistence.
# Pause/Resume use SAPI native methods. Poll returns "Paused" state natively.
#
# Protocol: JSON commands on stdin, line responses on stdout.
#   {"action":"speak","voice":"...","rate":N,"message":"..."}                      → "OK" | "VOICE_ERR"
#   {"action":"poll"}                                                               → "Speaking" | "Ready" | "Paused"
#   {"action":"skip"}                                                               → "OK"
#   {"action":"pause"}                                                              → "OK"
#   {"action":"resume"}                                                             → "OK"
#   {"action":"synthesize","voice":"...","rate":N,"message":"...","file_id":"..."}  → "SYNTH_OK" | "SYNTH_ERR:..."
#   "quit"                                                                          → exits
TTS_ENGINE_PS = r"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer

# Ensure TTS output directory exists
$ttsDir = "C:\temp\tts"
if (-not (Test-Path $ttsDir)) { New-Item -ItemType Directory -Path $ttsDir | Out-Null }

[Console]::WriteLine("READY")
[Console]::Out.Flush()

while ($true) {
    $line = [Console]::ReadLine()
    if ($line -eq $null -or $line -eq "quit") { break }
    try { $cmd = $line | ConvertFrom-Json } catch { continue }

    switch ($cmd.action) {
        "speak" {
            try { $synth.SelectVoice($cmd.voice) } catch {
                [Console]::WriteLine("VOICE_ERR")
                [Console]::Out.Flush()
                continue
            }
            $synth.Rate = [int]$cmd.rate
            $synth.SpeakAsync($cmd.message) | Out-Null
            [Console]::WriteLine("OK")
            [Console]::Out.Flush()
        }
        "poll" {
            [Console]::WriteLine($synth.State.ToString())
            [Console]::Out.Flush()
        }
        "skip" {
            $synth.SpeakAsyncCancelAll()
            [Console]::WriteLine("OK")
            [Console]::Out.Flush()
        }
        "pause" {
            $synth.Pause()
            [Console]::WriteLine("OK")
            [Console]::Out.Flush()
        }
        "resume" {
            $synth.Resume()
            [Console]::WriteLine("OK")
            [Console]::Out.Flush()
        }
        "synthesize" {
            try {
                $synth.SelectVoice($cmd.voice)
            } catch {
                [Console]::WriteLine("SYNTH_ERR:Voice not found: $($cmd.voice)")
                [Console]::Out.Flush()
                continue
            }
            $synth.Rate = [int]$cmd.rate
            $wavPath = "$ttsDir\$($cmd.file_id).wav"
            try {
                $synth.SetOutputToWaveFile($wavPath)
                $synth.Speak($cmd.message)
                $synth.SetOutputToDefaultAudioDevice()
                [Console]::WriteLine("SYNTH_OK")
                [Console]::Out.Flush()
            } catch {
                # Restore default audio output even on failure
                try { $synth.SetOutputToDefaultAudioDevice() } catch {}
                [Console]::WriteLine("SYNTH_ERR:$($_.Exception.Message)")
                [Console]::Out.Flush()
            }
        }
    }
}
$synth.Dispose()
"""

# Write PS script to Windows-accessible path so PowerShell can run it
TTS_SCRIPT_WSL_PATH = "/mnt/c/temp/token_tts_engine.ps1"
TTS_SCRIPT_WIN_PATH = r"C:\temp\token_tts_engine.ps1"


class TTSEngine:
    """Persistent PowerShell process for Windows SAPI TTS.

    Eliminates ~3-4s cold-start per message by keeping the synthesizer loaded.
    Thread-safe: speak() runs in threadpool, skip() can interrupt from another thread.
    """

    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._io_lock = threading.Lock()
        self._speaking = False
        self._was_skipped = False
        # Managed playback state (synth-and-speak path)
        self._playing = False
        self._play_paused = False
        self._current_file: Optional[str] = None

    def _write_script(self):
        """Write the PS script to a Windows-accessible path."""
        os.makedirs("/mnt/c/temp", exist_ok=True)
        with open(TTS_SCRIPT_WSL_PATH, "w") as f:
            f.write(TTS_ENGINE_PS)

    def start(self):
        """Start the persistent PowerShell process."""
        self._write_script()
        self._process = subprocess.Popen(
            [POWERSHELL_EXE, "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", TTS_SCRIPT_WIN_PATH],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        # Wait for READY signal (timeout 15s for cold start)
        line = self._readline_raw(timeout=15)
        if line != "READY":
            logger.error(f"TTS engine: Expected READY, got: {line}")
            self._kill()
            raise RuntimeError(f"TTS engine failed to start: {line}")
        logger.info("TTS engine: Persistent PowerShell started")

    def _readline_raw(self, timeout=5):
        """Read one line from stdout with timeout. No lock needed (called during init)."""
        import select
        fileno = self._process.stdout.fileno()
        ready, _, _ = select.select([fileno], [], [], timeout)
        if ready:
            return self._process.stdout.readline().strip()
        return None

    def _send(self, cmd):
        """Send JSON command to PS stdin. Caller must hold _io_lock."""
        self._process.stdin.write(json.dumps(cmd) + "\n")
        self._process.stdin.flush()

    def _readline(self):
        """Read one line from PS stdout. Caller must hold _io_lock."""
        return self._process.stdout.readline().strip()

    def _kill(self):
        """Kill the PS process."""
        if self._process and self._process.poll() is None:
            try:
                self._process.kill()
                self._process.wait(timeout=3)
            except Exception:
                pass
        self._process = None

    def _ensure_running(self):
        """Start or restart the PS process if needed."""
        if self._process is None or self._process.poll() is not None:
            if self._process is not None:
                logger.warning("TTS engine: Process died, restarting")
            self.start()

    @property
    def is_speaking(self):
        return self._speaking

    def speak(self, message: str, voice: str, rate: int = 0) -> dict:
        """Speak text. Blocks until done or skipped. Returns result dict."""
        self._ensure_running()
        self._speaking = True
        self._was_skipped = False

        # Send speak command
        with self._io_lock:
            self._send({"action": "speak", "voice": voice, "rate": rate, "message": message})
            resp = self._readline()

        if resp == "VOICE_ERR":
            self._speaking = False
            return {"success": False, "error": f"Voice not found: {voice}"}
        if resp != "OK":
            self._speaking = False
            return {"success": False, "error": f"Unexpected response: {resp}"}

        # Poll for completion — release lock between polls so skip() can send
        while True:
            time.sleep(0.1)
            with self._io_lock:
                self._send({"action": "poll"})
                state = self._readline()
            if state == "Ready":
                break
            if state is None or state == "":
                # Process died
                self._speaking = False
                self._process = None
                return {"success": False, "error": "TTS engine process died"}

        self._speaking = False
        return {"success": True, "skipped": self._was_skipped}

    def skip(self) -> bool:
        """Cancel current speech (direct or managed playback). Returns True if was active."""
        if not (self._speaking or self._playing) or self._process is None:
            return False
        with self._io_lock:
            self._send({"action": "skip"})
            resp = self._readline()
        self._was_skipped = True
        return True

    def shutdown(self):
        """Gracefully stop the PS process."""
        if self._process and self._process.poll() is None:
            try:
                with self._io_lock:
                    self._process.stdin.write("quit\n")
                    self._process.stdin.flush()
                self._process.wait(timeout=5)
            except Exception:
                self._kill()
        logger.info("TTS engine: Shutdown")

    # ── File-based synthesis and playback ──

    TTS_DIR_WSL = "/mnt/c/temp/tts"
    TTS_DIR_WIN = r"C:\temp\tts"

    def cleanup_old_files(self, max_age_seconds: int = 3600):
        """Delete WAV files older than max_age_seconds from the TTS directory."""
        try:
            now = time.time()
            for f in glob.glob(os.path.join(self.TTS_DIR_WSL, "*.wav")):
                if now - os.path.getmtime(f) > max_age_seconds:
                    os.unlink(f)
                    logger.info(f"TTS cleanup: removed {os.path.basename(f)}")
        except Exception as e:
            logger.warning(f"TTS cleanup error: {e}")

    def synthesize(self, message: str, voice: str, rate: int = 0, file_id: str = None) -> dict:
        """Synthesize text to a WAV file using SAPI. Blocking — returns when file is written."""
        self._ensure_running()
        self.cleanup_old_files()

        if not file_id:
            file_id = str(uuid.uuid4())

        with self._io_lock:
            self._send({
                "action": "synthesize",
                "voice": voice,
                "rate": rate,
                "message": message,
                "file_id": file_id,
            })
            resp = self._readline()

        if resp == "SYNTH_OK":
            wav_path_win = f"{self.TTS_DIR_WIN}\\{file_id}.wav"
            wav_path_wsl = f"{self.TTS_DIR_WSL}/{file_id}.wav"
            logger.info(f"TTS synthesize: {len(message)} chars -> {file_id}.wav")
            return {"success": True, "file_id": file_id, "wav_path_win": wav_path_win, "wav_path_wsl": wav_path_wsl}
        elif resp and resp.startswith("SYNTH_ERR:"):
            error = resp[len("SYNTH_ERR:"):]
            logger.warning(f"TTS synthesize failed: {error}")
            return {"success": False, "error": error}
        else:
            logger.warning(f"TTS synthesize unexpected response: {resp}")
            return {"success": False, "error": f"Unexpected response: {resp}"}

    def synth_and_speak(self, message: str, voice: str, rate: int = 0) -> dict:
        """Synthesize text to WAV (for replay/persistence) then speak it via SAPI.

        Blocks until speech completes or is skipped. Supports pause/resume mid-speech.
        """
        synth_result = self.synthesize(message, voice, rate)
        if not synth_result.get("success"):
            return synth_result

        self._playing = True
        self._current_file = synth_result.get("wav_path_wsl")
        speak_result = self.speak(message, voice, rate)
        self._playing = False
        self._play_paused = False
        self._current_file = None

        return {
            **speak_result,
            "file_id": synth_result.get("file_id"),
            "wav_path_win": synth_result.get("wav_path_win"),
        }

    def play_control(self, command: str) -> dict:
        """Send transport control (pause/resume/stop) to SAPI. Non-blocking."""
        if not (self._speaking or self._playing) and command != "stop":
            return {"success": False, "error": "Not speaking or playing"}
        self._ensure_running()

        # Map commands to SAPI actions
        action_map = {"pause": "pause", "resume": "resume", "stop": "skip"}
        action = action_map.get(command)
        if not action:
            return {"success": False, "error": f"Unknown command: {command}"}

        with self._io_lock:
            self._send({"action": action})
            resp = self._readline()

        if command == "pause":
            self._play_paused = True
        elif command == "resume":
            self._play_paused = False
        elif command == "stop":
            self._was_skipped = True
            self._play_paused = False

        return {"success": True, "command": command}

    def get_status(self) -> dict:
        """Get current TTS engine status."""
        return {
            "speaking": self._speaking,
            "playing": self._playing,
            "paused": self._play_paused,
            "current_file": self._current_file,
            "engine_alive": self._process is not None and self._process.poll() is None,
        }


# Global TTS engine instance
tts_engine = TTSEngine()


# ─── DeskFlow KVM Watchdog ──────────────────────────────────────────────

MAC_API_BASE = "http://100.95.109.23:7777"
MAC_TAILSCALE_IP = "100.95.109.23"
DESKFLOW_EXE = r"C:\Tools\Deskflow\deskflow.exe"
DESKFLOW_POLL_INTERVAL = 30          # seconds between checks
DESKFLOW_CONFIRM_CHECKS = 2          # consecutive checks before state transition
DESKFLOW_STABLE_TIMEOUT = 900        # 15 min: stop polling after this long in RUNNING
DESKFLOW_PROCESS_CHECK_INTERVAL = 300 # verify DeskFlow process every 5 min


class DeskFlowWatchdog:
    """Background watchdog for DeskFlow KVM server lifecycle.

    The local DeskFlow server runs permanently. The watchdog only manages
    connection state and restarts the Mac client when needed.

    States:
        starting  — boot: waiting for Tailscale + DeskFlow startup
        running   — connected, periodic health checks
        waiting   — server up, Mac not connected, polling for connection
        idle      — stable for 15min, reduced polling (still checks server alive)
        held      — manual override, watchdog paused for N minutes
        stopped   — manual force_stop, fully paused
    """

    def __init__(self):
        self.state = "starting"
        self.consecutive_up = 0
        self.consecutive_down = 0
        self.last_process_check = 0.0
        self.hold_until: Optional[float] = None
        self.last_mac_status: Optional[bool] = None
        self.last_state_change = time.time()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self):
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="deskflow-watchdog"
        )
        self._thread.start()
        logger.info("KVM watchdog: Started")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    # ── Tailscale ──

    def _wait_for_tailscale(self):
        """Block until Tailscale is up and can reach the Mac's IP."""
        logger.info("KVM watchdog: Waiting for Tailscale...")
        for attempt in range(30):  # up to ~60s
            if self._stop_event.is_set():
                return False
            try:
                result = subprocess.run(
                    ["tailscale", "status", "--json"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    data = json.loads(result.stdout)
                    peers = data.get("Peer", {})
                    for peer in peers.values():
                        addrs = peer.get("TailscaleIPs", [])
                        if MAC_TAILSCALE_IP in addrs and peer.get("Online"):
                            logger.info("KVM watchdog: Tailscale ready, Mac peer online")
                            return True
            except Exception:
                pass
            self._stop_event.wait(2)
        logger.warning("KVM watchdog: Tailscale timeout, proceeding anyway")
        return False

    # ── Main loop ──

    def _run(self):
        self._wait_for_tailscale()
        if self._stop_event.is_set():
            return

        # Start DeskFlow server immediately — it stays up permanently
        self._start_deskflow_server()
        time.sleep(5)

        # Check if Mac already auto-connected
        if self._check_deskflow_connected():
            self.state = "running"
            self.last_state_change = time.time()
            self.last_process_check = time.time()
            logger.info("KVM watchdog: Mac already connected on boot → RUNNING")
        else:
            # Try to kick the Mac client
            self._ensure_mac_client_connected()
            if self._check_deskflow_connected():
                self.state = "running"
                self.last_state_change = time.time()
                self.last_process_check = time.time()
                logger.info("KVM watchdog: Mac connected after kick → RUNNING")
            else:
                self.state = "waiting"
                logger.info("KVM watchdog: Mac not connected on boot, server running, will poll")

        while not self._stop_event.is_set():
            self._stop_event.wait(DESKFLOW_POLL_INTERVAL)
            if self._stop_event.is_set():
                break
            try:
                self._tick()
            except Exception as e:
                logger.error(f"KVM watchdog: tick error: {e}")

    def _tick(self):
        # Stopped: manual force_stop, do nothing until force_start
        if self.state == "stopped":
            return

        # Held: wait for expiry
        if self.state == "held":
            if self.hold_until and time.time() > self.hold_until:
                logger.info("KVM watchdog: Hold expired, resuming")
                self.state = "waiting"
                self.consecutive_up = 0
                self.consecutive_down = 0
            # Even when held, keep the local server alive
            self._ensure_server_alive()
            return

        # Always ensure the local server is running
        self._ensure_server_alive()

        # Check actual deskflow connection (ESTABLISHED on port 24800)
        connected = self._check_deskflow_connected()
        self.last_mac_status = connected

        if connected:
            self.consecutive_up += 1
            self.consecutive_down = 0
        else:
            self.consecutive_down += 1
            self.consecutive_up = 0

        # State transitions based on actual connection
        if self.state in ("waiting", "starting") and connected:
            if self.consecutive_up >= DESKFLOW_CONFIRM_CHECKS:
                logger.info("KVM watchdog: Mac connected → RUNNING")
                self.state = "running"
                self.last_state_change = time.time()
                self.last_process_check = time.time()

        elif self.state == "running" and not connected:
            if self.consecutive_down >= DESKFLOW_CONFIRM_CHECKS:
                logger.info("KVM watchdog: Mac disconnected → WAITING (server stays up)")
                self.state = "waiting"
                self.last_state_change = time.time()

        elif self.state == "running" and connected:
            # Stable — check if we can reduce polling
            elapsed = time.time() - self.last_state_change
            if elapsed >= DESKFLOW_STABLE_TIMEOUT:
                logger.info(
                    f"KVM watchdog: Stable for {int(elapsed)}s → IDLE"
                )
                self.state = "idle"
                self.last_state_change = time.time()

        elif self.state == "idle":
            if not connected:
                logger.info("KVM watchdog: Connection lost while idle → WAITING")
                self.state = "waiting"
                self.last_state_change = time.time()
                self.consecutive_down = 1
                self.consecutive_up = 0

    def _ensure_server_alive(self):
        """Restart the local DeskFlow server if it died. Lightweight check."""
        now = time.time()
        if now - self.last_process_check < DESKFLOW_PROCESS_CHECK_INTERVAL:
            return
        self.last_process_check = now
        if not self._check_deskflow_running():
            logger.warning("KVM watchdog: DeskFlow server died, restarting")
            self._start_deskflow_server()

    # ── Reachability ──

    def _check_mac_reachable(self) -> bool:
        try:
            resp = http_requests.get(f"{MAC_API_BASE}/health", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    # ── Connection check ──

    def _check_deskflow_connected(self) -> bool:
        """Check if DeskFlow server has an ESTABLISHED client connection.

        This catches the case where the Mac process is running but its
        socket is CLOSED/dead — pgrep alone won't detect that.
        """
        try:
            # Check for ESTABLISHED connections on port 24800
            result = subprocess.run(
                [POWERSHELL_EXE, "-NoProfile", "-Command",
                 "Get-NetTCPConnection -LocalPort 24800 "
                 "-State Established -ErrorAction SilentlyContinue | "
                 "Select-Object -First 1 RemoteAddress"],
                capture_output=True, text=True, timeout=10,
            )
            return bool(result.stdout.strip())
        except Exception:
            return False

    # ── Windows DeskFlow management ──

    def _check_deskflow_running(self) -> bool:
        try:
            result = subprocess.run(
                [POWERSHELL_EXE, "-NoProfile", "-Command",
                 "Get-Process deskflow-core -ErrorAction SilentlyContinue"],
                capture_output=True, text=True, timeout=10,
            )
            return bool(result.stdout.strip())
        except Exception:
            return False

    def _start_deskflow_server(self):
        if self._check_deskflow_running():
            logger.info("KVM watchdog: DeskFlow already running, skipping start")
            return
        logger.info("KVM watchdog: Starting DeskFlow server")
        try:
            subprocess.run(
                [POWERSHELL_EXE, "-NoProfile", "-Command",
                 f"Start-Process '{DESKFLOW_EXE}' -WindowStyle Minimized"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception as e:
            logger.error(f"KVM watchdog: Failed to start DeskFlow: {e}")

    def _stop_deskflow_server(self):
        logger.info("KVM watchdog: Stopping DeskFlow server")
        try:
            subprocess.run(
                [POWERSHELL_EXE, "-NoProfile", "-Command",
                 "Stop-Process -Name deskflow, deskflow-core "
                 "-Force -ErrorAction SilentlyContinue"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception as e:
            logger.error(f"KVM watchdog: Failed to stop DeskFlow: {e}")

    # ── Mac client management ──

    def _wake_mac_display(self) -> bool:
        try:
            result = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                 "mini", "caffeinate -u -t 5"],
                capture_output=True, text=True, timeout=12,
            )
            return result.returncode == 0
        except Exception as e:
            logger.warning(f"KVM watchdog: Failed to wake Mac display: {e}")
            return False

    def _restart_mac_client(self):
        """Force-kill and restart the Mac DeskFlow client via SSH.

        The API-based stop (pkill) is too gentle — the GUI can respawn
        the core before the start call, leaving a stuck socket. SSH with
        kill -9 + fresh open is reliable.
        """
        logger.info("KVM watchdog: Restarting Mac client via SSH (kill → wake → open)")
        try:
            result = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                 "mini",
                 "killall -9 Deskflow deskflow-core 2>/dev/null; "
                 "sleep 2; caffeinate -u -t 5 & open -a Deskflow"],
                capture_output=True, text=True, timeout=20,
            )
            if result.returncode == 0:
                logger.info("KVM watchdog: Mac client restarted via SSH")
            else:
                logger.warning(
                    f"KVM watchdog: SSH restart returned {result.returncode}: "
                    f"{result.stderr.strip()}"
                )
        except Exception as e:
            logger.error(f"KVM watchdog: SSH restart failed: {e}")
            # Fallback to API
            try:
                http_requests.post(f"{MAC_API_BASE}/api/kvm/stop", timeout=10)
                time.sleep(2)
                http_requests.post(f"{MAC_API_BASE}/api/kvm/start", timeout=10)
            except Exception:
                pass

    def _ensure_mac_client_connected(self):
        """Start Mac client if needed, restart if connection is dead.

        Checks for an ESTABLISHED connection first — if the Mac auto-connected
        to our server, there's nothing to do.
        """
        if self._check_deskflow_connected():
            logger.info("KVM watchdog: Mac already connected, nothing to do")
            return

        # Not connected — try to wake and start the Mac client
        self._wake_mac_display()
        time.sleep(2)
        try:
            resp = http_requests.post(
                f"{MAC_API_BASE}/api/kvm/start", timeout=10
            )
            data = resp.json()
            logger.info(f"KVM watchdog: Mac client → {data.get('message', 'unknown')}")
        except Exception as e:
            logger.warning(f"KVM watchdog: Mac API not available (no token-api?): {e}")

        # Wait for connection to establish, then verify
        time.sleep(8)
        if not self._check_deskflow_connected():
            logger.warning(
                "KVM watchdog: No ESTABLISHED connection on port 24800, "
                "restarting Mac client via SSH"
            )
            self._restart_mac_client()

    # ── State transitions are handled inline in _tick() ──

    # ── API helpers ──

    def get_status(self) -> dict:
        running = self._check_deskflow_running()
        connected = self._check_deskflow_connected() if running else False
        return {
            "state": self.state,
            "mac_connected": connected,
            "deskflow_running": running,
            "deskflow_connected": connected,
            "consecutive_up": self.consecutive_up,
            "consecutive_down": self.consecutive_down,
            "last_state_change": datetime.fromtimestamp(
                self.last_state_change
            ).isoformat(),
            "hold_until": (
                datetime.fromtimestamp(self.hold_until).isoformat()
                if self.hold_until else None
            ),
        }

    def hold(self, minutes: int = 30):
        self.hold_until = time.time() + (minutes * 60)
        self.state = "held"
        logger.info(f"KVM watchdog: Held for {minutes} minutes")

    def force_start(self):
        self._start_deskflow_server()
        time.sleep(5)
        self._ensure_mac_client_connected()
        self.state = "running"
        self.last_state_change = time.time()
        self.last_process_check = time.time()

    def force_stop(self):
        self._stop_deskflow_server()
        self.state = "stopped"
        self.last_state_change = time.time()


# Global watchdog instance
deskflow_watchdog = DeskFlowWatchdog()


# Mapping of app aliases to Windows executables
APP_TARGETS = {
    "brave": "brave.exe",
    "minecraft": "javaw.exe",
    "spotify": "Spotify.exe",
}

# Processes that must NEVER be enforced
PROTECTED_PROCESSES = {"vivaldi.exe"}


class EnforceRequest(BaseModel):
    app: str
    action: str = "close"


class TTSSpeakRequest(BaseModel):
    message: str
    voice: str = "Microsoft David"
    rate: int = 0


class KvmControlRequest(BaseModel):
    action: str  # "start", "stop", "hold"
    hold_minutes: int = 30


class AhkRequest(BaseModel):
    script: str  # Script filename (e.g., "voice-select-other.ahk")
    args: list[str] = []  # Optional arguments

class TmuxSendKeysRequest(BaseModel):
    pane: str       # tmux pane ID (e.g., "%5")
    command: str    # slash command or text to send (e.g., "/color cyan")
    no_escape: bool = False  # Skip C-u clear before sending (prompt known-empty)


class GoldenThroneFollowupRequest(BaseModel):
    session_id: str
    tmux_pane: Optional[str] = None
    working_dir: str = "~"
    prompt: str


@app.on_event("startup")
async def startup_event():
    """Warm up TTS engine and start DeskFlow watchdog."""
    try:
        tts_engine.start()
    except Exception as e:
        logger.warning(f"TTS engine warm-up failed (will retry on first speak): {e}")
    deskflow_watchdog.start()


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "token-satellite",
        "timestamp": datetime.now().isoformat(),
        "tts_engine": "running" if tts_engine._process and tts_engine._process.poll() is None else "stopped",
        "kvm_watchdog": deskflow_watchdog.state,
    }


@app.post("/enforce")
async def enforce(request: EnforceRequest):
    """Close a Windows process by app alias."""
    app_name = request.app.lower()
    action = request.action.lower()

    if action != "close":
        raise HTTPException(status_code=400, detail=f"Unsupported action: {action}")

    if app_name not in APP_TARGETS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown app '{app_name}'. Valid: {list(APP_TARGETS.keys())}",
        )

    exe = APP_TARGETS[app_name]

    if exe.lower() in {p.lower() for p in PROTECTED_PROCESSES}:
        logger.warning(f"BLOCKED: Refusing to close protected process {exe}")
        raise HTTPException(status_code=403, detail=f"{exe} is protected")

    logger.info(f"ENFORCE: Closing {exe} (app={app_name})")

    try:
        result = subprocess.run(
            [CMD_EXE, "/c", "taskkill", "/IM", exe, "/F"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        success = result.returncode == 0
        logger.info(f"ENFORCE: taskkill {exe} -> rc={result.returncode} stdout={result.stdout.strip()}")
        return {
            "success": success,
            "app": app_name,
            "exe": exe,
            "returncode": result.returncode,
            "output": result.stdout.strip() or result.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        logger.error(f"ENFORCE: taskkill {exe} timed out")
        return {"success": False, "app": app_name, "exe": exe, "error": "timeout"}
    except Exception as e:
        logger.error(f"ENFORCE: taskkill {exe} failed: {e}")
        return {"success": False, "app": app_name, "exe": exe, "error": str(e)}


@app.get("/processes")
async def list_processes():
    """List running distraction-relevant processes (for debugging)."""
    all_targets = set(APP_TARGETS.values())
    running = []

    try:
        result = subprocess.run(
            [CMD_EXE, "/c", "tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.strip().split("\n"):
            line = line.strip().strip('"')
            if not line:
                continue
            # CSV format: "name.exe","PID","Session Name","Session#","Mem Usage"
            parts = line.split('","')
            if parts:
                proc_name = parts[0].strip('"')
                if proc_name.lower() in {t.lower() for t in all_targets}:
                    running.append(proc_name)
    except Exception as e:
        logger.error(f"Failed to list processes: {e}")
        return {"error": str(e), "running": []}

    return {
        "running": running,
        "monitored": list(APP_TARGETS.keys()),
        "protected": list(PROTECTED_PROCESSES),
    }


@app.post("/tts/speak")
def tts_speak(request: TTSSpeakRequest):
    """Speak text using Windows SAPI. Blocks until speech completes or is skipped."""
    if tts_engine.is_speaking:
        raise HTTPException(status_code=409, detail="Already speaking")

    logger.info(f"TTS: Speaking {len(request.message)} chars with {request.voice} (rate={request.rate})")
    result = tts_engine.speak(request.message, request.voice, request.rate)

    if result.get("skipped"):
        logger.info("TTS: Speech skipped")
    elif result.get("success"):
        logger.info("TTS: Speech completed")
    else:
        logger.warning(f"TTS: Failed: {result.get('error')}")

    return result


@app.post("/tts/skip")
async def tts_skip():
    """Skip current TTS playback (direct speak or file playback)."""
    was_active = tts_engine.skip()
    logger.info(f"TTS: Skip requested (was_active={was_active})")
    return {"success": True, "was_speaking": was_active}


# ── File-based TTS: synthesize to WAV + controlled playback ──

class TTSSynthesizeRequest(BaseModel):
    message: str
    voice: str = "Microsoft David"
    rate: int = 0

class TTSControlRequest(BaseModel):
    command: str   # pause, resume, stop

class TTSSynthAndPlayRequest(BaseModel):
    message: str
    voice: str = "Microsoft David"
    rate: int = 0


@app.post("/tts/synthesize")
def tts_synthesize(request: TTSSynthesizeRequest):
    """Synthesize text to a WAV file using SAPI. Blocks until file is written."""
    if tts_engine.is_speaking:
        raise HTTPException(status_code=409, detail="SAPI is busy speaking")

    logger.info(f"TTS synthesize: {len(request.message)} chars with {request.voice}")
    return tts_engine.synthesize(request.message, request.voice, request.rate)


@app.post("/tts/control")
async def tts_control(request: TTSControlRequest):
    """Transport control for SAPI speech (pause/resume/stop). Non-blocking."""
    logger.info(f"TTS control: {request.command}")
    return tts_engine.play_control(request.command)


@app.post("/tts/synth-and-play")
def tts_synth_and_play(request: TTSSynthAndPlayRequest):
    """Synthesize text to WAV (for replay) then speak it via SAPI. Blocks until done."""
    if tts_engine.is_speaking or tts_engine._playing:
        raise HTTPException(status_code=409, detail="TTS engine is busy")

    logger.info(f"TTS synth-and-play: {len(request.message)} chars with {request.voice}")
    result = tts_engine.synth_and_speak(request.message, request.voice, request.rate)

    method = "skipped" if result.get("skipped") else "wsl_sapi_file"
    return {
        "success": result.get("success", False),
        "skipped": result.get("skipped", False),
        "method": method,
        "voice": request.voice,
        "file_id": result.get("file_id"),
        "message": request.message[:50],
    }


@app.get("/tts/status")
async def tts_status():
    """Get current TTS engine status (speaking, playing, paused, etc.)."""
    return tts_engine.get_status()


@app.post("/ahk/execute")
async def execute_ahk(req: AhkRequest):
    """Execute a one-shot AHK v2 script. AHK is a dumb executor — token-api decides when to call."""
    script_path = AHK_SCRIPTS_DIR / req.script
    if not script_path.exists():
        raise HTTPException(status_code=404, detail=f"AHK script not found: {req.script}")
    # Security: only allow scripts in the ahk directory
    if not script_path.resolve().is_relative_to(AHK_SCRIPTS_DIR.resolve()):
        raise HTTPException(status_code=403, detail="Script path escapes ahk directory")
    try:
        cmd = [AHK_EXE, str(script_path)] + req.args
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        logger.info(f"AHK: Executed {req.script} (exit={result.returncode})")
        return {"ok": True, "exit_code": result.returncode, "stderr": result.stderr[:200] if result.stderr else None}
    except subprocess.TimeoutExpired:
        logger.warning(f"AHK: Timeout executing {req.script}")
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/kvm/status")
async def kvm_watchdog_status():
    """Get DeskFlow watchdog state."""
    return deskflow_watchdog.get_status()


@app.post("/kvm/control")
async def kvm_control(request: KvmControlRequest):
    """Manual control over DeskFlow watchdog."""
    action = request.action.lower()
    if action == "start":
        deskflow_watchdog.force_start()
        return {"success": True, "action": "start", "state": deskflow_watchdog.state}
    elif action == "stop":
        deskflow_watchdog.force_stop()
        return {"success": True, "action": "stop", "state": deskflow_watchdog.state}
    elif action == "hold":
        deskflow_watchdog.hold(request.hold_minutes)
        return {
            "success": True, "action": "hold",
            "minutes": request.hold_minutes, "state": deskflow_watchdog.state,
        }
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown action: {action}. Valid: start, stop, hold",
        )


@app.get("/files/read")
async def read_file(path: str):
    """Read a file from the local filesystem. Scoped to ~/.claude/ for security."""
    claude_dir = Path.home() / ".claude"
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(claude_dir.resolve()):
        raise HTTPException(status_code=403, detail="Path must be under ~/.claude/")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    try:
        content = resolved.read_text(encoding="utf-8")
        logger.info(f"FILES: Read {resolved} ({len(content)} bytes)")
        return {"path": str(resolved), "content": content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Read failed: {e}")


@app.post("/tmux/send-keys")
async def tmux_send_keys(req: TmuxSendKeysRequest):
    """Send a command to a Claude Code instance's tmux pane.

    Used by Mac Token-API / claude-cmd for cross-machine dispatch.
    Input locking is handled by the caller (Mac-side DB lock), not here.
    """
    pane = req.pane
    command = req.command

    # Validate pane format
    if not pane.startswith("%"):
        raise HTTPException(status_code=400, detail=f"Invalid pane format: {pane}")

    # Verify pane exists
    try:
        verify = subprocess.run(
            ["tmux", "display-message", "-t", pane, "-p", "#{pane_id}"],
            capture_output=True, text=True, timeout=5,
        )
        if verify.returncode != 0 or not verify.stdout.strip():
            raise HTTPException(status_code=404, detail=f"Pane {pane} not found")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="tmux command timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"tmux check failed: {e}")

    # Send command (mirrors claude-cmd pattern)
    try:
        if not req.no_escape:
            subprocess.run(["tmux", "send-keys", "-t", pane, "C-u"], timeout=5)
            time.sleep(0.3)
        subprocess.run(["tmux", "send-keys", "-t", pane, command, "Enter"], timeout=5)
        logger.info(f"TMUX: Sent to {pane}: {command[:80]}")
        return {"success": True, "pane": pane, "command": command}
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="tmux send-keys timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"tmux send-keys failed: {e}")


def _get_or_create_backrooms_pane() -> str:
    """Split a new pane in the backrooms window for an autonomous session.

    Each resume gets its own pane — backrooms is a dynamic process stack,
    not a shared resource. Creates the backrooms window if it doesn't exist.
    """
    # Check if backrooms window exists
    result = subprocess.run(
        ["tmux", "list-panes", "-t", "main:backrooms", "-F", "#{pane_id}"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode == 0 and result.stdout.strip():
        # Window exists — split a new pane into it
        split = subprocess.run(
            ["tmux", "split-window", "-t", "main:backrooms", "-d", "-P", "-F", "#{pane_id}"],
            capture_output=True, text=True, timeout=5,
        )
        if split.returncode == 0 and split.stdout.strip():
            pane_id = split.stdout.strip()
            subprocess.run(
                ["tmux", "set-option", "-p", "-t", pane_id, "@PANE_TYPE", "backrooms"],
                timeout=5,
            )
            logger.info(f"Golden Throne: split new backrooms pane {pane_id}")
            return pane_id
        # split failed (too many panes?) — fall through to use first existing pane
        logger.warning("Golden Throne: split-window failed, reusing existing backrooms pane")
        return result.stdout.strip().split("\n")[0]

    # Create backrooms window (detached so it doesn't steal focus)
    subprocess.run(
        ["tmux", "new-window", "-t", "main", "-n", "backrooms", "-d"],
        timeout=5,
    )

    # Get pane ID and tag it
    result = subprocess.run(
        ["tmux", "list-panes", "-t", "main:backrooms", "-F", "#{pane_id}"],
        capture_output=True, text=True, timeout=5,
    )
    pane_id = result.stdout.strip().split("\n")[0]
    subprocess.run(
        ["tmux", "set-option", "-p", "-t", pane_id, "@PANE_TYPE", "backrooms"],
        timeout=5,
    )
    logger.info(f"Golden Throne: created backrooms window (pane {pane_id})")
    return pane_id


@app.post("/golden-throne/followup")
async def golden_throne_followup(req: GoldenThroneFollowupRequest):
    """Resume an idle Claude instance via tmux send-keys or claude --resume.

    Called by Mac token-api when the Golden Throne timer fires for a WSL instance.
    Transport detection: if the tmux pane has claude running, send-keys the SOP prompt.
    Otherwise, spawn `claude --resume` in the backrooms window.
    """
    transport = "unknown"
    pane = req.tmux_pane

    if pane:
        # Check if pane exists and what's running in it
        try:
            verify = subprocess.run(
                ["tmux", "display-message", "-t", pane, "-p", "#{pane_current_command}"],
                capture_output=True, text=True, timeout=5,
            )
            current_cmd = verify.stdout.strip() if verify.returncode == 0 else ""
        except Exception:
            current_cmd = ""

        # On Mac, pane_current_command shows version string (e.g. "2.1.88") not "claude"
        cmd_is_claude = current_cmd and (
            "claude" in current_cmd.lower()
            or (current_cmd[0:1].isdigit() and "." in current_cmd)  # version string
        )
        if cmd_is_claude:
            # Claude is alive in the pane — send SOP prompt via send-keys
            try:
                subprocess.run(["tmux", "send-keys", "-t", pane, "C-u"], timeout=5)
                time.sleep(0.3)
                subprocess.run(["tmux", "send-keys", "-t", pane, req.prompt, "Enter"], timeout=5)
                transport = "send-keys"
                logger.info(f"Golden Throne: sent SOP to {pane} via send-keys (session {req.session_id[:12]})")
            except Exception as e:
                logger.error(f"Golden Throne: send-keys failed for {pane}: {e}")
                raise HTTPException(status_code=500, detail=f"send-keys failed: {e}")
        else:
            # Claude not running — resume in backrooms with SOP prompt
            try:
                backrooms_pane = _get_or_create_backrooms_pane()
                working_dir = os.path.expanduser(req.working_dir)
                # Write SOP to temp file (avoids shell escaping)
                sop_file = f"/tmp/golden-throne-sop-{req.session_id[:8]}.md"
                Path(sop_file).write_text(req.prompt)
                resume_cmd = (
                    f'cd {working_dir} && claude -p "$(cat {sop_file})" '
                    f'--resume {req.session_id} --dangerously-skip-permissions'
                )
                subprocess.run(
                    ["tmux", "send-keys", "-t", backrooms_pane, resume_cmd, "Enter"],
                    timeout=5,
                )
                transport = "resume"
                logger.info(
                    f"Golden Throne: resumed {req.session_id[:12]} in backrooms "
                    f"pane={backrooms_pane} via claude --resume"
                )
            except Exception as e:
                logger.error(f"Golden Throne: resume failed for {req.session_id[:12]}: {e}")
                raise HTTPException(status_code=500, detail=f"resume failed: {e}")
    else:
        raise HTTPException(status_code=400, detail="No tmux_pane provided")

    return {"success": True, "transport": transport, "session_id": req.session_id}


@app.post("/restart")
async def restart_satellite(pull: bool = True):
    """Git pull, write TUI signals, then exit for systemd restart."""
    result = {"pull": None, "tui_signals": False, "restarting": True}

    # 1. Git pull
    if pull:
        try:
            proc = subprocess.run(
                ["git", "-C", str(Path.home() / "Scripts"), "pull", "--ff-only"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            result["pull"] = {
                "success": proc.returncode == 0,
                "output": proc.stdout.strip() or proc.stderr.strip(),
            }
        except Exception as e:
            result["pull"] = {"success": False, "error": str(e)}

    # 2. Write TUI restart signals
    signal_dir = Path.home() / ".claude"
    signal_dir.mkdir(parents=True, exist_ok=True)
    signal_data = json.dumps({"reason": "token-restart", "timestamp": datetime.now().isoformat()})
    for suffix in ("desktop", "mobile"):
        (signal_dir / f"tui-restart-{suffix}.signal").write_text(signal_data)
    result["tui_signals"] = True

    # 3. Shutdown cleanly
    deskflow_watchdog.stop()
    tts_engine.shutdown()

    # 4. Schedule exit after response is sent (systemd Restart=always brings us back)
    def delayed_exit():
        time.sleep(0.5)
        logger.info("RESTART: Exiting for systemd restart")
        os._exit(0)

    threading.Thread(target=delayed_exit, daemon=True).start()

    logger.info(f"RESTART: pull={result['pull']}, signals written, exiting in 0.5s")
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7777)
