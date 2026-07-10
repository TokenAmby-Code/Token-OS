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
    POST /tts/synth-and-play — synthesize to WAV + play WAV artifact (blocking, for queued TTS)
    GET  /tts/status       — current TTS engine state
    POST /ahk/execute      — execute a one-shot AHK v2 script
    POST /restart          — restart current service
    POST /runtime/refresh  — authenticated local runtime/AHK cache refresh
    GET  /kvm/status       — DeskFlow watchdog state
    POST /kvm/control      — manual DeskFlow start/stop/hold
    GET  /files/read       — read a file under ~/.claude/ (for cross-machine transcript fetch)
    POST /tmux/send-keys   — send a command to a tmux pane (cross-machine dispatch)
"""

import glob
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests as http_requests
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger("token_satellite")

app = FastAPI(title="Token Satellite", version="1.0.0")

# Full paths — bare exes aren't on PATH under systemd
REPO_ROOT = Path(__file__).resolve().parent.parent
CMD_EXE = "/mnt/c/Windows/System32/cmd.exe"
POWERSHELL_EXE = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
AHK_EXE = "/mnt/c/Program Files/AutoHotkey/v2/AutoHotkey.exe"
# AHK scripts default to the repo's ahk/ dir, but WSL production runs from a
# Windows-local cache (C:\TokenOS\ahk) so AHK execution does not hit the NAS.
AHK_SCRIPTS_DIR = Path(os.environ.get("TOKEN_OS_AHK_DIR") or (REPO_ROOT / "ahk"))
RUNTIME_REFRESH_SECRET_ENV = "TOKEN_SATELLITE_REFRESH_SECRET"
RUNTIME_REFRESH_HELPER = Path(
    os.environ.get("TOKEN_SATELLITE_REFRESH_HELPER")
    or (Path.home() / ".local" / "bin" / "token-satellite-refresh")
)


def _runtime_git_sha() -> str | None:
    """Return the checked-out satellite runtime SHA, if this file is in Git."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "-q", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


# PowerShell script for persistent TTS engine.
# Uses SpeakAsync so the main loop stays responsive to skip/poll/pause/resume commands.
# Also supports file-based synthesis (SetOutputToWaveFile) for replay/persistence.
# Pause/Resume use SAPI native methods. Poll returns "Paused" state natively.
#
# Protocol: JSON commands on stdin, line responses on stdout.
#   {"action":"speak","voice":"...","rate":N,"message_file":"C:\...txt"}           → "OK:<chars>:<sha256>" | "VOICE_ERR"
#   {"action":"poll"}                                                               → "Speaking" | "Ready" | "Paused"
#   {"action":"skip"}                                                               → "OK"
#   {"action":"pause"}                                                              → "OK"
#   {"action":"resume"}                                                             → "OK"
#   {"action":"synthesize","voice":"...","rate":N,"message_file":"C:\...txt","file_id":"..."} → "SYNTH_OK:<chars>:<sha256>" | "SYNTH_ERR:..."
#   "quit"                                                                          → exits
TTS_ENGINE_PS = r"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer

function Get-TokenTtsText($cmd) {
    if ($cmd.PSObject.Properties.Name -contains "message_file" -and $cmd.message_file) {
        return [System.IO.File]::ReadAllText([string]$cmd.message_file, [System.Text.Encoding]::UTF8)
    }
    return [string]$cmd.message
}

function Get-TokenTtsSha256([string]$text) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
        $hash = $sha.ComputeHash($bytes)
        return -join ($hash | ForEach-Object { $_.ToString("x2") })
    } finally {
        $sha.Dispose()
    }
}

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
            try {
                $text = Get-TokenTtsText $cmd
                $textHash = Get-TokenTtsSha256 $text
            } catch {
                [Console]::WriteLine("TEXT_ERR:$($_.Exception.Message)")
                [Console]::Out.Flush()
                continue
            }
            $synth.SpeakAsync($text) | Out-Null
            [Console]::WriteLine("OK:$($text.Length):$textHash")
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
                $text = Get-TokenTtsText $cmd
                $textHash = Get-TokenTtsSha256 $text
                $synth.SetOutputToWaveFile($wavPath)
                $synth.Speak($text)
                $synth.SetOutputToDefaultAudioDevice()
                [Console]::WriteLine("SYNTH_OK:$($text.Length):$textHash")
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
        self._process: subprocess.Popen | None = None
        self._io_lock = threading.Lock()
        self._speaking = False
        self._was_skipped = False
        # Managed playback state (synth-and-speak path)
        self._playing = False
        self._play_paused = False
        self._current_file: str | None = None
        # Track current message for restart
        self._current_message: str | None = None
        self._current_voice: str | None = None
        self._current_rate: int = 0
        self._playback_process: subprocess.Popen | None = None
        self._playback_windows_pid: int | None = None
        self._playback_lock = threading.Lock()

    def _write_script(self):
        """Write the PS script to a Windows-accessible path."""
        os.makedirs("/mnt/c/temp", exist_ok=True)
        with open(TTS_SCRIPT_WSL_PATH, "w") as f:
            f.write(TTS_ENGINE_PS)

    def start(self):
        """Start the persistent PowerShell process."""
        self._write_script()
        self._process = subprocess.Popen(
            [
                POWERSHELL_EXE,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                TTS_SCRIPT_WIN_PATH,
            ],
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

    def skip(self) -> bool:
        """Cancel current speech (direct SAPI or WAV artifact playback)."""
        with self._playback_lock:
            playback_active = self._playing
            playback_proc = self._playback_process
        if playback_active and playback_proc is not None:
            self._was_skipped = True
            self._terminate_wav_playback()
            return True
        if playback_active:
            # Startup/shutdown edge: avoid silently dropping a stop while the
            # playback process handle is being published or cleared.
            for _ in range(10):
                time.sleep(0.01)
                with self._playback_lock:
                    playback_proc = self._playback_process
                    playback_active = self._playing
                if playback_proc is not None:
                    self._was_skipped = True
                    self._terminate_wav_playback()
                    return True
                if not playback_active:
                    break
        if not self._speaking or self._process is None:
            return False
        with self._io_lock:
            self._send({"action": "skip"})
            self._readline()
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
    PLAY_SCRIPT_DIR_WSL = "/mnt/c/temp"
    PLAY_SCRIPT_DIR_WIN = r"C:\temp"
    MAX_PLAYBACK_SECONDS = 3600

    @staticmethod
    def _text_hash(message: str) -> str:
        return hashlib.sha256(message.encode("utf-8")).hexdigest()

    def _write_message_file(self, message: str, file_id: str | None = None) -> dict:
        """Write TTS text to a Windows-readable UTF-8 file.

        The persistent PowerShell engine reads this file instead of receiving
        long text through a command payload. That removes transport truncation
        as a possible success-with-clipped-audio failure mode.
        """
        if not file_id:
            file_id = str(uuid.uuid4())
        os.makedirs(self.TTS_DIR_WSL, exist_ok=True)
        text_path_wsl = f"{self.TTS_DIR_WSL}/{file_id}.txt"
        text_path_win = f"{self.TTS_DIR_WIN}\\{file_id}.txt"
        with open(text_path_wsl, "w", encoding="utf-8", newline="") as f:
            f.write(message)
        return {
            "file_id": file_id,
            "text_path_wsl": text_path_wsl,
            "text_path_win": text_path_win,
            "message_hash": self._text_hash(message),
            "message_chars": len(message),
        }

    def _parse_text_ack(self, response: str | None, prefix: str, expected_hash: str) -> dict:
        """Parse OK/SYNTH_OK acknowledgements from PowerShell.

        New engines return PREFIX:<powershell-char-count>:<sha256-of-utf8-text>.
        The hash is the authoritative guard because Python len() and
        PowerShell .Length can differ for non-BMP Unicode.
        """
        if response is None:
            return {"success": False, "error": "No response from TTS engine"}
        if response.startswith("TEXT_ERR:"):
            return {"success": False, "error": response[len("TEXT_ERR:") :]}
        if response == prefix:
            return {
                "success": False,
                "error": f"TTS engine did not return text integrity ack for {prefix}",
            }
        marker = f"{prefix}:"
        if not response.startswith(marker):
            return {"success": False, "error": f"Unexpected response: {response}"}

        ack = response[len(marker) :]
        try:
            rendered_chars_raw, rendered_hash = ack.split(":", 1)
            rendered_chars = int(rendered_chars_raw)
        except ValueError:
            return {"success": False, "error": f"Malformed text ack: {response}"}

        if rendered_hash.lower() != expected_hash.lower():
            return {
                "success": False,
                "error": "TTS text integrity check failed",
                "expected_hash": expected_hash,
                "rendered_hash": rendered_hash,
                "rendered_chars": rendered_chars,
            }

        return {
            "success": True,
            "rendered_chars": rendered_chars,
            "rendered_hash": rendered_hash,
        }

    def cleanup_old_files(self, max_age_seconds: int = 3600):
        """Delete WAV files older than max_age_seconds from the TTS directory."""
        try:
            now = time.time()
            for pattern in ("*.wav", "*.txt"):
                for f in glob.glob(os.path.join(self.TTS_DIR_WSL, pattern)):
                    if now - os.path.getmtime(f) > max_age_seconds:
                        os.unlink(f)
                        logger.info(f"TTS cleanup: removed {os.path.basename(f)}")
        except Exception as e:
            logger.warning(f"TTS cleanup error: {e}")

    def speak(self, message: str, voice: str, rate: int = 0) -> dict:
        """Speak text. Blocks until done or skipped. Returns result dict."""
        self._ensure_running()
        self.cleanup_old_files()
        self._speaking = True
        self._was_skipped = False

        message_file = self._write_message_file(message)

        # Send speak command. Text goes through a temp file; command payload
        # carries only the file path plus voice/rate metadata.
        with self._io_lock:
            self._send(
                {
                    "action": "speak",
                    "voice": voice,
                    "rate": rate,
                    "message_file": message_file["text_path_win"],
                }
            )
            resp = self._readline()

        if resp == "VOICE_ERR":
            self._speaking = False
            return {"success": False, "error": f"Voice not found: {voice}"}

        ack = self._parse_text_ack(resp, "OK", message_file["message_hash"])
        if not ack.get("success"):
            self._speaking = False
            return ack

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
        return {
            "success": True,
            "skipped": self._was_skipped,
            "transport": "wsl_sapi_text_file",
            "message_chars": message_file["message_chars"],
            "rendered_chars": ack["rendered_chars"],
            "rendered_hash": ack["rendered_hash"],
        }

    def synthesize(self, message: str, voice: str, rate: int = 0, file_id: str = None) -> dict:
        """Synthesize text to a WAV file using SAPI. Blocking — returns when file is written."""
        self._ensure_running()
        self.cleanup_old_files()

        if not file_id:
            file_id = str(uuid.uuid4())

        message_file = self._write_message_file(message, file_id=file_id)

        with self._io_lock:
            self._send(
                {
                    "action": "synthesize",
                    "voice": voice,
                    "rate": rate,
                    "message_file": message_file["text_path_win"],
                    "file_id": file_id,
                }
            )
            resp = self._readline()

        if resp and resp.startswith("SYNTH_ERR:"):
            error = resp[len("SYNTH_ERR:") :]
            logger.warning(f"TTS synthesize failed: {error}")
            return {"success": False, "error": error}

        ack = self._parse_text_ack(resp, "SYNTH_OK", message_file["message_hash"])
        if not ack.get("success"):
            logger.warning(f"TTS synthesize failed integrity check: {ack.get('error')}")
            return ack

        wav_path_win = f"{self.TTS_DIR_WIN}\\{file_id}.wav"
        wav_path_wsl = f"{self.TTS_DIR_WSL}/{file_id}.wav"
        logger.info(f"TTS synthesize: {len(message)} chars -> {file_id}.wav")
        return {
            "success": True,
            "file_id": file_id,
            "wav_path_win": wav_path_win,
            "wav_path_wsl": wav_path_wsl,
            "transport": "wsl_sapi_text_file",
            "message_chars": message_file["message_chars"],
            "rendered_chars": ack["rendered_chars"],
            "rendered_hash": ack["rendered_hash"],
        }

    def _wav_player_script(self, wav_path_win: str) -> str:
        escaped = wav_path_win.replace("'", "''")
        return (
            "Add-Type -AssemblyName System.Windows.Extensions -ErrorAction SilentlyContinue\n"
            "Add-Type -AssemblyName System\n"
            f"$player = New-Object System.Media.SoundPlayer '{escaped}'\n"
            "$player.Load()\n"
            "$player.PlaySync()\n"
            "$player.Dispose()\n"
        )

    def _terminate_wav_playback(self) -> None:
        with self._playback_lock:
            proc = self._playback_process
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _play_wav_file(self, synth_result: dict) -> dict:
        """Play a synthesized WAV artifact with SoundPlayer.PlaySync()."""
        wav_path_win = synth_result["wav_path_win"]
        wav_path_wsl = Path(synth_result["wav_path_wsl"])
        if not wav_path_wsl.exists():
            return {"success": False, "error": f"WAV file missing: {wav_path_wsl}"}

        play_script_dir_wsl = Path(self.PLAY_SCRIPT_DIR_WSL)
        play_script_dir_wsl.mkdir(parents=True, exist_ok=True)
        fd, script_path_raw = tempfile.mkstemp(
            prefix="token_tts_play_", suffix=".ps1", dir=str(play_script_dir_wsl)
        )
        os.close(fd)
        script_path_wsl = Path(script_path_raw)
        script_path_win = self.PLAY_SCRIPT_DIR_WIN + "\\" + script_path_wsl.name
        script_path_wsl.write_text(self._wav_player_script(wav_path_win), encoding="utf-8")

        self._was_skipped = False
        self._current_file = str(wav_path_wsl)
        try:
            try:
                proc = subprocess.Popen(
                    [
                        POWERSHELL_EXE,
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        script_path_win,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except OSError as e:
                return {"success": False, "error": f"Failed to start WAV playback: {e}"}
            with self._playback_lock:
                self._playing = True
                self._playback_process = proc
                self._playback_windows_pid = proc.pid
            try:
                stdout, stderr = proc.communicate(timeout=self.MAX_PLAYBACK_SECONDS)
            except subprocess.TimeoutExpired:
                self._was_skipped = True
                proc.kill()
                try:
                    stdout, stderr = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", ""
                return {"success": False, "error": "WAV playback timed out"}
            skipped = self._was_skipped or proc.returncode < 0
            if proc.returncode not in (0, None) and not skipped:
                return {
                    "success": False,
                    "error": stderr.strip() or f"WAV playback exited {proc.returncode}",
                }
            return {
                "success": not skipped,
                "skipped": skipped,
                "transport": "wsl_sapi_wav_file",
                "file_id": synth_result.get("file_id"),
                "wav_path_win": wav_path_win,
                "wav_path_wsl": str(wav_path_wsl),
                "playback_pid": self._playback_windows_pid,
                "rendered_chars": synth_result.get("rendered_chars"),
                "rendered_hash": synth_result.get("rendered_hash"),
                "message_chars": synth_result.get("message_chars"),
            }
        finally:
            with self._playback_lock:
                self._playing = False
                self._playback_process = None
                self._playback_windows_pid = None
            self._play_paused = False
            self._current_file = None
            try:
                script_path_wsl.unlink()
            except OSError:
                pass

    def synth_and_speak(self, message: str, voice: str, rate: int = 0) -> dict:
        """Synthesize the full utterance to WAV, then play that WAV artifact."""
        synth_result = self.synthesize(message, voice, rate)
        if not synth_result.get("success"):
            return synth_result

        self._current_message = message
        self._current_voice = voice
        self._current_rate = rate
        try:
            return self._play_wav_file(synth_result)
        finally:
            self._current_message = None
            self._current_voice = None
            self._current_rate = 0

    def play_control(self, command: str) -> dict:
        """Transport controls for direct SAPI; WAV playback only supports stop."""
        if self._playing and not self._speaking:
            if command == "stop":
                stopped = self.skip()
                return {"success": stopped, "command": command, "skipped": stopped}
            if command in {"pause", "resume", "toggle", "restart"}:
                return {
                    "success": False,
                    "error": "unsupported_backend_control",
                    "reason": "wsl_wav_playback_pause_resume_unsupported",
                    "command": command,
                    "transport": "wsl_sapi_wav_file",
                }

        # Resolve toggle to pause or resume for direct SAPI speech
        if command == "toggle":
            if self._play_paused:
                command = "resume"
            elif self._speaking:
                command = "pause"
            else:
                return {"success": False, "error": "Not speaking or playing"}

        # Restart: stop current speech, re-speak same message from beginning
        if command == "restart":
            if not self._current_message:
                return {"success": False, "error": "No current message to restart"}
            # Stop current speech
            with self._io_lock:
                self._send({"action": "skip"})
                self._readline()
            self._play_paused = False
            # Re-speak the same message (non-blocking — speak polls in its own thread)
            with self._io_lock:
                self._send(
                    {
                        "action": "speak",
                        "voice": self._current_voice,
                        "rate": self._current_rate,
                        "message": self._current_message,
                    }
                )
                resp = self._readline()
            if resp != "OK":
                return {"success": False, "error": f"Restart speak failed: {resp}"}
            return {"success": True, "command": "restart"}

        if not self._speaking and command != "stop":
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
DESKFLOW_CORE_EXE = r"C:\Tools\Deskflow\deskflow-core.exe"
DESKFLOW_CORE_EXE_WSL = "/mnt/c/Tools/Deskflow/deskflow-core.exe"
DESKFLOW_SERVER_CONFIG_WIN = "C:/ProgramData/Deskflow/deskflow-server.conf"
DESKFLOW_SERVER_CONFIG_WSL = Path("/mnt/c/ProgramData/Deskflow/deskflow-server.conf")
DESKFLOW_GUI_CONFIG_WSL = Path("/mnt/c/Users/colby/AppData/Roaming/Deskflow/Deskflow.conf")
DESKFLOW_CORE_LOG = Path("/tmp/deskflow-core.log")
# Transient systemd user unit the server runs in — its own cgroup, so satellite
# restarts (every deploy) can't take the live KVM session down with them.
DESKFLOW_SERVER_UNIT = "deskflow-core-server"
DESKFLOW_SERVER_CONFIG_BACKUPS = [
    REPO_ROOT / "config" / "deskflow" / "wsl-server.conf",
]
DESKFLOW_RECONNECT_WAIT = 5  # seconds to allow opportunistic reconnect between recovery tiers
DESKFLOW_OPP_GRACE = 3  # seconds to watch the follower for self-heal before each escalation rung
DESKFLOW_BACKOFF_SECONDS = [30, 60, 120, 300, 900]
DESKFLOW_MAX_RECOVERY_ATTEMPTS = 6


class DeskflowLogFollower(threading.Thread):
    """Event-driven follower of deskflow-core's own log (replaces the connection poll).

    Pure-Python ``tail -F`` of ``DESKFLOW_CORE_LOG``: opens the log, seeks to the
    end, and classifies each appended line into a connection edge. Edges are pushed
    onto the watchdog's ``edge_queue`` — the follower NEVER runs recovery itself
    (recovery does blocking SSH/HTTP/PowerShell and would blind the reader). It only
    reads, classifies, and enqueues, so it is always available to observe the next
    edge. Handles inode-change / truncation (manual delete, logrotate) by reopening;
    the normal append path keeps a stable inode.

    Edges (substring-classified, robust to the ``[timestamp] LEVEL:`` prefix):
        DROP       — ``IPC: client "Tokens-Mac-Mini" has disconnected``
        UP         — ``IPC: client "Tokens-Mac-Mini" has connected`` /
                     ``NOTE: accepted client connection``
        SERVER_UP  — ``IPC: started server, waiting for clients``
    """

    DROP = "drop"
    UP = "up"
    SERVER_UP = "server_up"

    _DROP_MARKER = 'IPC: client "Tokens-Mac-Mini" has disconnected'
    _UP_MARKERS = (
        'IPC: client "Tokens-Mac-Mini" has connected',
        "NOTE: accepted client connection",
    )
    _SERVER_UP_MARKER = "IPC: started server, waiting for clients"

    READ_INTERVAL = 0.25  # seconds between tail reads when at EOF

    def __init__(self, edge_queue: "queue.Queue", stop_event: threading.Event) -> None:
        super().__init__(daemon=True, name="deskflow-log-follower")
        self._edge_queue = edge_queue
        self._stop_event = stop_event
        self.connected = False
        self._fh = None
        self._inode: int | None = None
        self._buf = b""
        self._first_open = True

    @classmethod
    def _classify(cls, line: str) -> str | None:
        """Map a raw log line to a connection edge, or None if uninteresting."""
        if cls._DROP_MARKER in line:
            return cls.DROP
        if any(marker in line for marker in cls._UP_MARKERS):
            return cls.UP
        if cls._SERVER_UP_MARKER in line:
            return cls.SERVER_UP
        return None

    def _emit(self, edge: str) -> None:
        """Update derived connected state from the edge and enqueue it."""
        if edge == self.UP:
            self.connected = True
        elif edge == self.DROP:
            self.connected = False
        # SERVER_UP carries no connection state — it only triggers the invite.
        self._edge_queue.put(edge)

    def _open(self, seek_end: bool) -> bool:
        """Open the log; ``seek_end`` skips existing content. False if absent yet.

        The very first open seeks to end (don't replay the day's history). A reopen
        after rotation/truncation reads from the START of the new file, so lines
        already written to it before we noticed the swap are not lost.
        """
        try:
            fh = open(DESKFLOW_CORE_LOG, "rb")
        except OSError:
            return False
        if seek_end:
            fh.seek(0, os.SEEK_END)
        self._fh = fh
        self._buf = b""
        try:
            self._inode = os.fstat(fh.fileno()).st_ino
        except OSError:
            self._inode = None
        return True

    def _close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
        self._fh = None
        self._buf = b""

    def _rotated(self) -> bool:
        """True if the log was replaced (inode change) or truncated below our cursor."""
        try:
            st = os.stat(DESKFLOW_CORE_LOG)
        except OSError:
            return False
        try:
            pos = self._fh.tell()
        except (OSError, ValueError):
            return True
        return (self._inode is not None and st.st_ino != self._inode) or st.st_size < pos

    def run(self) -> None:
        while not self._stop_event.is_set():
            if self._fh is None:
                if not self._open(seek_end=self._first_open):
                    self._stop_event.wait(1.0)
                    continue
                self._first_open = False
            try:
                chunk = self._fh.readline()
            except (OSError, ValueError):
                self._close()
                continue
            if chunk:
                self._buf += chunk
                if self._buf.endswith(b"\n"):
                    line = self._buf.decode("utf-8", "replace")
                    self._buf = b""
                    edge = self._classify(line)
                    if edge:
                        self._emit(edge)
                # Drain remaining buffered lines immediately before sleeping.
                continue
            # EOF: handle rotation/truncation, then wait for the next append.
            if self._rotated():
                self._close()
                continue
            self._stop_event.wait(self.READ_INTERVAL)
        self._close()


class DeskFlowWatchdog:
    """Background watchdog for DeskFlow KVM server lifecycle.

    The local DeskFlow server runs permanently. The watchdog only manages
    connection state and restarts the Mac client when needed. Detection is
    event-driven: a ``DeskflowLogFollower`` tails deskflow-core's own log and
    pushes connection edges onto ``self._edge_queue``; the watchdog consumes
    them on its own thread (so all recovery stays under ``_recovery_lock`` and
    never blinds the follower). There is NO periodic liveness poll: a server
    death that writes no log line is caught at the next satellite boot (every
    deploy restarts the satellite, whose boot path starts the server if absent)
    or by manual ``/kvm/control start`` — a KVM drop is immediately user-visible,
    so a background check adds latency-bounded noise, not safety. The only timed
    wake-ups are the bounded recovery retries while actively un-connected.

    States:
        starting  — boot: waiting for Tailscale + DeskFlow startup
        running   — connected (persists until a DROP edge)
        waiting   — server up, Mac not connected, recovery pending
        recovering — tiered reconnect/restart ladder is in progress
        backoff   — recovery failed, waiting before next attempt
        ceased    — repeated recovery failures; manual/signal intervention required
        held      — manual override, watchdog paused for N minutes
        stopped   — manual force_stop, fully paused
    """

    def __init__(self):
        self.state = "starting"
        self.hold_until: float | None = None
        self.last_state_change = time.time()
        self.recovery_attempts = 0
        self.next_recovery_at = 0.0
        self.last_recovery_action: str | None = None
        self.last_observation: dict | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._recovery_lock = threading.Lock()
        self._edge_queue: queue.Queue = queue.Queue()
        self._follower = DeskflowLogFollower(self._edge_queue, self._stop_event)
        # When boot launches the server, its paired SERVER_UP edge is redundant
        # with the eager boot invite — consume it once to avoid a double-invite.
        self._suppress_boot_server_up = False

    def start(self) -> None:
        self._follower.start()
        self._thread = threading.Thread(target=self._run, daemon=True, name="deskflow-watchdog")
        self._thread.start()
        logger.info("KVM watchdog: Started")

    def stop(self) -> None:
        self._stop_event.set()
        # Sentinel unblocks the edge loop, which may be parked on a timeout-less
        # queue.get (no periodic wake-up exists in connected/held/ceased states).
        self._edge_queue.put(None)
        if self._thread:
            self._thread.join(timeout=10)
        if self._follower:
            self._follower.join(timeout=5)

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
                    capture_output=True,
                    text=True,
                    timeout=5,
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

    def _run(self) -> None:
        self._wait_for_tailscale()
        if self._stop_event.is_set():
            return

        # Start DeskFlow server immediately — it stays up permanently.
        started = self._start_deskflow_server()

        # Boot invitation. The Mac never busy-waits; we invite eagerly so a missed
        # boot SERVER_UP line (the follower can open the freshly-created log just
        # after that line is written) can never leave the Mac un-invited. If we
        # launched the server, arm _suppress_boot_server_up so the paired SERVER_UP
        # edge is consumed once instead of issuing a duplicate (bouncing) invite.
        if self._follower_connected():
            self._mark_connected()
            logger.info("KVM watchdog: Mac connected on boot → RUNNING")
        else:
            if self.state == "starting":
                self.state = "waiting"
            if started:
                self._suppress_boot_server_up = True
            logger.info("KVM watchdog: Inviting Mac on boot (server started=%s)", started)
            self._invite_mac()
            # Arm a fallback retry: if the boot invite + its paired SERVER_UP never
            # connect the Mac, no further edge arrives — the idle tick re-drives the
            # ladder at next_recovery_at. _mark_connected() resets this to 0.0.
            self.next_recovery_at = time.time() + DESKFLOW_BACKOFF_SECONDS[0]

        # Edge loop: block on the follower's edge queue — indefinitely unless a
        # recovery retry or hold expiry is scheduled. On timeout, run the idle
        # tick. A ``None`` edge is the stop sentinel; a real edge queued ahead of
        # the sentinel must not dispatch once shutdown has started.
        while not self._stop_event.is_set():
            try:
                edge = self._edge_queue.get(timeout=self._edge_loop_timeout())
            except queue.Empty:
                if not self._stop_event.is_set():
                    self._on_idle_tick()
                continue
            if edge is None or self._stop_event.is_set():
                continue
            try:
                self._dispatch_edge(edge)
            except Exception as e:
                logger.error(f"KVM watchdog: edge dispatch error ({edge}): {e}")

    def _edge_loop_timeout(self) -> float | None:
        """Edge-queue blocking timeout.

        Two timed obligations exist: the next recovery retry while actively
        un-connected (``waiting``/``backoff``, at ``next_recovery_at``) and the
        hold expiry while ``held`` (at ``hold_until``). Wake for those, floored
        at 1.0s. In every other state (``running``/``ceased``/``stopped``) there
        is nothing timed to do: block until an edge or the stop sentinel arrives
        (no periodic liveness poll, by decree).
        """
        if self.state in ("waiting", "backoff"):
            return max(1.0, self.next_recovery_at - time.time())
        if self.state == "held" and self.hold_until:
            return max(1.0, self.hold_until - time.time())
        return None

    def _on_idle_tick(self) -> None:
        """Idle-tick body: hold expiry + a bounded recovery retry.

        Only reachable in ``waiting``/``backoff``/``held`` — every other state
        blocks on the edge queue without a timeout.

        ``held``: resume when the hold expires — connected holds settle straight
        to ``running``; un-connected holds drop to ``waiting`` with an immediate
        retry armed (a DROP swallowed during the hold produces no further edge,
        so the resume itself must re-drive recovery).

        ``waiting``/``backoff``: re-drive the recovery ladder at the scheduled
        retry time so a wedged state (boot invite or a prior rung that never
        connected the Mac, with no further edge arriving) heals without an
        external trigger. ``ceased`` is excluded by design (it waits for an UP
        edge to resurrect); ``running`` never retries. ``_recovery_lock`` guards
        re-entrancy; backoff escalation to ``ceased`` after
        DESKFLOW_MAX_RECOVERY_ATTEMPTS still bounds it.
        """
        if self.state == "held":
            if self.hold_until and time.time() > self.hold_until:
                logger.info("KVM watchdog: Hold expired, resuming")
                if self._follower_connected():
                    self._mark_connected()
                else:
                    self.state = "waiting"
                    self.last_state_change = time.time()
                    self.next_recovery_at = time.time()
            return
        if (
            self.state in ("waiting", "backoff")
            and not self._follower.connected
            and time.time() >= self.next_recovery_at
        ):
            self._recover_connection(reason=self.state)

    def _dispatch_edge(self, edge: str) -> None:
        # Stopped: manual force_stop, ignore edges until force_start.
        if self.state == "stopped":
            return

        # Held: ignore connection edges until the hold expires.
        if self.state == "held":
            if self.hold_until and time.time() > self.hold_until:
                logger.info("KVM watchdog: Hold expired, resuming")
                self.state = "waiting"
            else:
                return

        if edge == DeskflowLogFollower.SERVER_UP:
            self._on_server_up()
        elif edge == DeskflowLogFollower.UP:
            self._on_link_up()
        elif edge == DeskflowLogFollower.DROP:
            self._on_link_down()

    def _on_server_up(self) -> None:
        """Server (re)started — invite the Mac client.

        Skip in two cases, because the invite path stop+starts the client and a
        redundant invite would bounce a live/converging session:
          - the boot SERVER_UP that pairs with the eager boot invite (one-shot), or
          - the client is already connected (an established client needs no invite).
        """
        if self._suppress_boot_server_up:
            self._suppress_boot_server_up = False
            logger.info("KVM watchdog: boot SERVER_UP edge (already invited) → no invite")
            return
        if self._follower.connected:
            logger.info("KVM watchdog: SERVER_UP edge while connected → no invite")
            return
        logger.info("KVM watchdog: SERVER_UP edge → inviting Mac")
        self._invite_mac()

    def _on_link_up(self) -> None:
        """Client-connected edge — mark RUNNING (resurrects from waiting/backoff/ceased)."""
        if self.state != "running":
            logger.info(f"KVM watchdog: UP edge → RUNNING (was {self.state})")
            self._mark_connected()

    def _on_link_down(self) -> None:
        """Client-disconnected edge — drop to WAITING and run the heal ladder."""
        if self.state == "running":
            logger.info("KVM watchdog: DROP edge → WAITING, healing")
            self.state = "waiting"
            self.last_state_change = time.time()
            self.next_recovery_at = time.time()
            self._recover_connection(reason="drop_edge")

    def _invite_mac(self) -> None:
        """The invitation IS the Mac client start (POST MAC_API_BASE/api/kvm/start)."""
        self._start_mac_client()

    def _follower_connected(self) -> bool:
        """Authoritative connection read: the follower's derived state, with a
        one-shot (never periodic) Established probe as the boot/edge fallback.

        A positive fallback probe latches ``self._follower.connected`` so
        ``_observe()`` / ``/kvm/status`` stop reporting the link down until the
        next contradicting log edge arrives.
        """
        if self._follower.connected:
            return True
        if self._check_deskflow_connected():
            self._follower.connected = True
            return True
        return False

    def _opportunistic_defer(self, seconds: float = DESKFLOW_OPP_GRACE) -> bool:
        """Watch the follower for an UP within a short grace window before escalating.

        Reads deskflow's own connect events (the authoritative state machine — this
        is not an error-suppressing debounce): if the link self-heals, abort the
        ladder before touching the Mac.
        """
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._stop_event.is_set():
                return False
            if self._follower.connected:
                return True
            self._stop_event.wait(0.2)
        return self._follower_connected()

    # ── Reachability ──

    def _check_mac_reachable(self) -> bool:
        try:
            resp = http_requests.get(f"{MAC_API_BASE}/health", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def _get_mac_kvm_status(self) -> dict:
        try:
            resp = http_requests.get(f"{MAC_API_BASE}/api/kvm/status", timeout=5)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {"running": None, "reachable": False}

    def _observe(self) -> dict:
        running = self._check_deskflow_running()
        listening = self._check_deskflow_listening() if running else False
        # Connection truth comes from the event follower, not a periodic probe.
        connected = self._follower.connected if running else False
        mac_reachable = self._check_mac_reachable()
        mac_status = self._get_mac_kvm_status() if mac_reachable else {"running": None}
        return {
            "deskflow_running": running,
            "deskflow_listening": listening,
            "deskflow_connected": connected,
            "mac_reachable": mac_reachable,
            "mac_client_running": mac_status.get("running"),
        }

    # ── Connection check ──

    def _check_deskflow_connected(self) -> bool:
        """Check if DeskFlow server has an ESTABLISHED client connection.

        This catches the case where the Mac process is running but its
        socket is CLOSED/dead — pgrep alone won't detect that.
        """
        try:
            # Check for ESTABLISHED connections on port 24800
            result = subprocess.run(
                [
                    POWERSHELL_EXE,
                    "-NoProfile",
                    "-Command",
                    "Get-NetTCPConnection -LocalPort 24800 "
                    "-State Established -ErrorAction SilentlyContinue | "
                    "Select-Object -First 1 RemoteAddress",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return bool(result.stdout.strip())
        except Exception:
            return False

    # ── Windows DeskFlow management ──

    def _deskflow_server_config_valid(self) -> bool:
        try:
            text = DESKFLOW_SERVER_CONFIG_WSL.read_text()
        except OSError:
            return False
        required = (
            "section: screens",
            "TokenPC:",
            "Tokens-Mac-Mini:",
            "section: links",
            "right = Tokens-Mac-Mini",
            "left = TokenPC",
        )
        return all(item in text for item in required)

    def _deskflow_backup_config(self) -> Path | None:
        for path in DESKFLOW_SERVER_CONFIG_BACKUPS:
            if path.exists():
                return path
        return None

    def _ensure_deskflow_config(self):
        """Restore the server topology if DeskFlow settings cleanup erased it."""
        if not self._deskflow_server_config_valid():
            backup = self._deskflow_backup_config()
            if not backup:
                logger.error("KVM watchdog: No DeskFlow server config backup found")
            else:
                DESKFLOW_SERVER_CONFIG_WSL.parent.mkdir(parents=True, exist_ok=True)
                DESKFLOW_SERVER_CONFIG_WSL.write_text(backup.read_text())
                logger.warning(f"KVM watchdog: Restored DeskFlow server config from {backup}")

        DESKFLOW_GUI_CONFIG_WSL.parent.mkdir(parents=True, exist_ok=True)
        try:
            gui_text = DESKFLOW_GUI_CONFIG_WSL.read_text()
        except OSError:
            gui_text = (
                "[core]\ncomputerName=TokenPC\nlastVersion=1.26.0.0\ncoreMode=2\n\n"
                "[gui]\n\n"
                "[server]\n"
            )

        if "[gui]" not in gui_text:
            gui_text += "\n[gui]\n"
        if "startCoreWithGui=" not in gui_text:
            gui_text = gui_text.replace("[gui]\n", "[gui]\nstartCoreWithGui=true\n", 1)

        if "[server]" not in gui_text:
            gui_text += "\n[server]\n"
        if "externalConfig=" in gui_text:
            gui_text = gui_text.replace("externalConfig=false", "externalConfig=true")
        else:
            gui_text = gui_text.replace("[server]\n", "[server]\nexternalConfig=true\n", 1)
        if "externalConfigFile=" not in gui_text:
            gui_text = gui_text.replace(
                "[server]\n",
                f"[server]\nexternalConfigFile={DESKFLOW_SERVER_CONFIG_WIN}\n",
                1,
            )
        if "[security]" not in gui_text:
            gui_text += "\n[security]\n"
        if "checkPeerFingerprints=" in gui_text:
            gui_text = gui_text.replace("checkPeerFingerprints=true", "checkPeerFingerprints=false")
        else:
            gui_text = gui_text.replace(
                "[security]\n", "[security]\ncheckPeerFingerprints=false\n", 1
            )
        DESKFLOW_GUI_CONFIG_WSL.write_text(gui_text)

    def _check_deskflow_listening(self) -> bool:
        try:
            result = subprocess.run(
                [
                    POWERSHELL_EXE,
                    "-NoProfile",
                    "-Command",
                    "Get-NetTCPConnection -LocalPort 24800 "
                    "-State Listen -ErrorAction SilentlyContinue | "
                    "Select-Object -First 1 LocalPort",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return bool(result.stdout.strip())
        except Exception:
            return False

    def _check_deskflow_running(self) -> bool:
        try:
            result = subprocess.run(
                [
                    POWERSHELL_EXE,
                    "-NoProfile",
                    "-Command",
                    "Get-Process deskflow-core -ErrorAction SilentlyContinue",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return bool(result.stdout.strip())
        except Exception:
            return False

    def _start_deskflow_server(self) -> bool:
        """Start deskflow-core if not running. Returns True iff a fresh process was
        launched (→ a SERVER_UP log edge will follow); False if already running.

        The server is spawned into its own transient systemd unit
        (``DESKFLOW_SERVER_UNIT``), NOT as a child of this process. The satellite
        restarts on every deploy, and a direct child dies with the satellite's
        cgroup (KillMode=control-group) — which dropped the live KVM session on
        every merge-to-main. The server stays up permanently by design; its
        lifetime must not be coupled to the satellite's.
        """
        if self._check_deskflow_running():
            logger.info("KVM watchdog: DeskFlow already running, skipping start")
            return False
        logger.info("KVM watchdog: Starting DeskFlow server")
        try:
            self._ensure_deskflow_config()
            # Clear any stale failed instance of the transient unit (e.g. after a
            # taskkill of the Windows process) so systemd-run can reuse the name.
            subprocess.run(
                ["systemctl", "--user", "reset-failed", DESKFLOW_SERVER_UNIT],
                capture_output=True,
                timeout=10,
            )
            result = subprocess.run(
                [
                    "systemd-run",
                    "--user",
                    "--collect",
                    f"--unit={DESKFLOW_SERVER_UNIT}",
                    f"--property=StandardOutput=append:{DESKFLOW_CORE_LOG}",
                    f"--property=StandardError=append:{DESKFLOW_CORE_LOG}",
                    DESKFLOW_CORE_EXE_WSL,
                    "server",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                # Fall back to a direct child so the KVM still heals — but that
                # re-couples the server to the satellite's lifetime, so shout.
                logger.error(
                    "KVM watchdog: systemd-run launch failed (rc=%s, stderr=%r) — "
                    "falling back to direct child; server will die with the "
                    "satellite cgroup on the next restart",
                    result.returncode,
                    result.stderr.strip(),
                )
                log_file = DESKFLOW_CORE_LOG.open("a")
                subprocess.Popen(
                    [DESKFLOW_CORE_EXE_WSL, "server"],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                )
            return True
        except Exception as e:
            logger.error(f"KVM watchdog: Failed to start DeskFlow: {e}")
            return False

    def _reload_deskflow_server(self):
        """Light local reload: restart only the core process and relaunch the GUI wrapper."""
        logger.info("KVM watchdog: Reloading local DeskFlow server")
        try:
            self._ensure_deskflow_config()
            subprocess.run(
                [
                    POWERSHELL_EXE,
                    "-NoProfile",
                    "-Command",
                    "Stop-Process -Name deskflow-core -Force -ErrorAction SilentlyContinue",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self._start_deskflow_server()
        except Exception as e:
            logger.error(f"KVM watchdog: Failed to reload DeskFlow: {e}")

    def _stop_deskflow_server(self):
        logger.info("KVM watchdog: Stopping DeskFlow server")
        try:
            subprocess.run(
                [
                    POWERSHELL_EXE,
                    "-NoProfile",
                    "-Command",
                    "Stop-Process -Name deskflow, deskflow-core "
                    "-Force -ErrorAction SilentlyContinue",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as e:
            logger.error(f"KVM watchdog: Failed to stop DeskFlow: {e}")

    # ── Mac client management ──

    def _wake_mac_display(self) -> bool:
        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "ConnectTimeout=5",
                    "-o",
                    "BatchMode=yes",
                    "mini",
                    "caffeinate -u -t 5",
                ],
                capture_output=True,
                text=True,
                timeout=12,
            )
            return result.returncode == 0
        except Exception as e:
            logger.warning(f"KVM watchdog: Failed to wake Mac display: {e}")
            return False

    def _start_mac_client(self):
        self._wake_mac_display()
        try:
            resp = http_requests.post(f"{MAC_API_BASE}/api/kvm/start", timeout=10)
            data = resp.json()
            logger.info(f"KVM watchdog: Mac client start → {data.get('message', 'unknown')}")
        except Exception as e:
            logger.warning(f"KVM watchdog: Mac API start unavailable: {e}")

    def _reload_mac_client(self):
        try:
            resp = http_requests.post(f"{MAC_API_BASE}/api/kvm/reload", timeout=10)
            data = resp.json()
            logger.info(f"KVM watchdog: Mac client reload → {data.get('message', 'unknown')}")
        except Exception as e:
            logger.warning(f"KVM watchdog: Mac API reload unavailable: {e}")

    def _restart_mac_client(self):
        """Force-kill and restart the Mac DeskFlow client via SSH.

        Bypasses a wedged Mac token-api entirely. The client is headless now
        (deskflow-core under the com.imperium.deskflow-client launchd job), so
        the hard path is kill -9 + `launchctl kickstart -k` of that job — never
        `open -a Deskflow`, which would resurrect the GUI and its own core
        supervisor.
        """
        logger.info("KVM watchdog: Restarting Mac client via SSH (kill → kickstart → wake)")
        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "ConnectTimeout=5",
                    "-o",
                    "BatchMode=yes",
                    "mini",
                    "killall -9 Deskflow deskflow-core 2>/dev/null; "
                    "launchctl kickstart -k gui/501/com.imperium.deskflow-client; "
                    "caffeinate -u -t 5 &",
                ],
                capture_output=True,
                text=True,
                timeout=20,
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

    def _wait_for_connection(self, seconds: int = DESKFLOW_RECONNECT_WAIT) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._stop_event.is_set():
                return False
            if self._follower.connected:
                self._mark_connected()
                return True
            self._stop_event.wait(0.2)
        # One-shot Established confirmation at the deadline (never a periodic probe).
        if self._follower_connected():
            self._mark_connected()
            return True
        return False

    def _mark_connected(self):
        self.state = "running"
        self.recovery_attempts = 0
        self.next_recovery_at = 0.0
        self.last_recovery_action = None
        self.last_state_change = time.time()

    def _schedule_backoff(self):
        self.recovery_attempts += 1
        if self.recovery_attempts >= DESKFLOW_MAX_RECOVERY_ATTEMPTS:
            self.state = "ceased"
            self.last_state_change = time.time()
            self.last_recovery_action = "ceased"
            logger.warning(
                "KVM watchdog: Recovery attempts exhausted → CEASED "
                "(manual /kvm/control start or satellite restart required)"
            )
            return
        delay = DESKFLOW_BACKOFF_SECONDS[
            min(self.recovery_attempts - 1, len(DESKFLOW_BACKOFF_SECONDS) - 1)
        ]
        self.next_recovery_at = time.time() + delay
        self.state = "backoff"
        self.last_state_change = time.time()
        logger.warning(f"KVM watchdog: Recovery failed → BACKOFF {delay}s")

    def _recover_connection(self, reason: str):
        if not self._recovery_lock.acquire(blocking=False):
            logger.info(f"KVM watchdog: Recovery already running, skipping ({reason})")
            return

        try:
            # Rung 0 — opportunistic grace: watch the follower for a self-heal
            # before any escalation. Aborts the ladder without touching the Mac.
            if self._opportunistic_defer():
                self._mark_connected()
                logger.info(f"KVM watchdog: Self-healed during opportunistic grace ({reason})")
                return

            self.state = "recovering"
            self.last_state_change = time.time()
            observation = self._observe()
            self.last_observation = observation
            logger.info(f"KVM watchdog: Recovery start ({reason}) observation={observation}")

            # Tier 0: process exists but the server port is absent. Fix local server first.
            if observation["deskflow_running"] and not observation["deskflow_listening"]:
                self.last_recovery_action = "local_reload_not_listening"
                self._reload_deskflow_server()
                if self._wait_for_connection(DESKFLOW_RECONNECT_WAIT):
                    logger.info(
                        "KVM watchdog: Recovered after local reload (server was not listening)"
                    )
                    return
                observation = self._observe()
                self.last_observation = observation

            # Tier 1: server is healthy but Mac is absent. Wake/start client and allow reconnect.
            if (
                observation["deskflow_running"]
                and observation["deskflow_listening"]
                and observation["mac_reachable"]
            ):
                if self._follower.connected:
                    self._mark_connected()
                    return
                self.last_recovery_action = "mac_quick_reconnect"
                self._start_mac_client()
                if self._wait_for_connection(DESKFLOW_RECONNECT_WAIT):
                    logger.info("KVM watchdog: Recovered after Mac quick reconnect")
                    return

            # Tier 2: local soft reload. This is the CLI equivalent of nudging server state.
            if self._follower.connected:
                self._mark_connected()
                return
            self.last_recovery_action = "local_reload"
            self._reload_deskflow_server()
            if self._wait_for_connection(DESKFLOW_RECONNECT_WAIT):
                logger.info("KVM watchdog: Recovered after local reload")
                return

            # Tier 3: full local kill/start, then give the Mac an opportunistic reconnect window.
            if self._follower.connected:
                self._mark_connected()
                return
            self.last_recovery_action = "local_full_restart"
            self._stop_deskflow_server()
            self._stop_event.wait(1)
            self._start_deskflow_server()
            if self._wait_for_connection(DESKFLOW_RECONNECT_WAIT):
                logger.info("KVM watchdog: Recovered after full local restart")
                return

            # Re-observe before touching the Mac. This prevents a stale escalation
            # from kicking the user out after the Mac reconnects late.
            if self._follower_connected():
                self._mark_connected()
                return

            observation = self._observe()
            self.last_observation = observation
            if observation["mac_reachable"]:
                # Tier 4: light Mac reload if available, then full Mac client restart.
                self.last_recovery_action = "mac_reload"
                self._reload_mac_client()
                if self._wait_for_connection(DESKFLOW_RECONNECT_WAIT):
                    logger.info("KVM watchdog: Recovered after Mac reload")
                    return

                if self._follower_connected():
                    self._mark_connected()
                    return

                self.last_recovery_action = "mac_full_restart"
                self._restart_mac_client()
                if self._wait_for_connection(DESKFLOW_RECONNECT_WAIT):
                    logger.info("KVM watchdog: Recovered after Mac full restart")
                    return

            self._schedule_backoff()
        finally:
            self._recovery_lock.release()

    def _ensure_mac_client_connected(self):
        """Start Mac client if needed, restart if connection is dead.

        Checks for an ESTABLISHED connection first — if the Mac auto-connected
        to our server, there's nothing to do.
        """
        if self._check_deskflow_connected():
            logger.info("KVM watchdog: Mac already connected, nothing to do")
            return

        self._recover_connection(reason="manual_ensure")

    # ── State transitions are handled inline by the edge handlers ──

    # ── API helpers ──

    def get_status(self) -> dict[str, object]:
        observation = self._observe()
        self.last_observation = observation
        running = observation["deskflow_running"]
        listening = observation["deskflow_listening"]
        connected = observation["deskflow_connected"]
        return {
            "state": self.state,
            "mac_connected": connected,
            "deskflow_running": running,
            "deskflow_listening": listening,
            "deskflow_connected": connected,
            "follower_connected": self._follower.connected,
            "mac_reachable": observation.get("mac_reachable"),
            "mac_client_running": observation.get("mac_client_running"),
            "recovery_attempts": self.recovery_attempts,
            "next_recovery_at": (
                datetime.fromtimestamp(self.next_recovery_at).isoformat()
                if self.next_recovery_at
                else None
            ),
            "last_recovery_action": self.last_recovery_action,
            "last_state_change": datetime.fromtimestamp(self.last_state_change).isoformat(),
            "hold_until": (
                datetime.fromtimestamp(self.hold_until).isoformat() if self.hold_until else None
            ),
        }

    def hold(self, minutes: int = 30):
        self.hold_until = time.time() + (minutes * 60)
        self.state = "held"
        logger.info(f"KVM watchdog: Held for {minutes} minutes")

    def force_start(self):
        self.recovery_attempts = 0
        self.next_recovery_at = 0.0
        self.last_recovery_action = "manual_start"
        self._start_deskflow_server()
        if self._wait_for_connection(DESKFLOW_RECONNECT_WAIT):
            return
        self._recover_connection(reason="manual_start")
        if not self._check_deskflow_connected() and self.state == "recovering":
            self.state = "waiting"
            self.last_state_change = time.time()

    def force_stop(self):
        self._stop_deskflow_server()
        self.state = "stopped"
        self.recovery_attempts = 0
        self.next_recovery_at = 0.0
        self.last_recovery_action = "manual_stop"
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
    action: str  # "start", "stop", "reload", "hold"
    hold_minutes: int = 30


class AhkRequest(BaseModel):
    script: str  # Script filename (e.g., "voice-select-other.ahk")
    args: list[str] = []  # Optional arguments


class TmuxSendKeysRequest(BaseModel):
    pane: str  # tmux pane ID (e.g., "%5")
    command: str  # slash command or text to send
    no_escape: bool = False  # Skip C-u clear before sending (prompt known-empty)


class GoldenThroneFollowupRequest(BaseModel):
    session_id: str
    tmux_pane: str | None = None
    working_dir: str = "~"
    prompt: str
    prompt_summary: str | None = None
    engine: str = "claude"


class RuntimeRefreshRequest(BaseModel):
    sha: str
    changed_paths: list[str] = Field(default_factory=list)


def _pane_input_line_has_text(line: str) -> bool:
    stripped = line.rstrip()
    if not stripped:
        return False
    if re.search(r"^[\s│░▒▓]*>\s*$", stripped):
        return False
    if re.search(r"[$%#>❯]\s*$", stripped):
        return False
    if not re.search(r"[$%#>❯]", stripped):
        return False
    return True


def _tmux_pane_has_pending_input(pane: str) -> bool:
    capture = subprocess.run(
        ["tmux", "capture-pane", "-t", pane, "-p"],
        capture_output=True,
        text=True,
        timeout=5,
        env={**os.environ, "IMPERIUM_TMUX_RAW": "1"},
    )
    if capture.returncode != 0:
        return False
    lines = [line for line in capture.stdout.splitlines() if line.strip()]
    return bool(lines and _pane_input_line_has_text(lines[-1]))


def _tmux_pane_pid(pane: str) -> int | None:
    proc = subprocess.run(
        ["tmux", "display-message", "-t", pane, "-p", "#{pane_pid}"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def _tmux_pane_has_agent_process(pane: str, engine: str) -> bool:
    pane_pid = _tmux_pane_pid(pane)
    if not pane_pid:
        return False
    proc = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,command="],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        return False
    children: dict[int, list[int]] = {}
    commands: dict[int, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        commands[pid] = parts[2].lower()
        children.setdefault(ppid, []).append(pid)

    needles = ("codex",) if engine == "codex" else ("claude",)
    stack = list(children.get(pane_pid, []))
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if any(needle in commands.get(pid, "") for needle in needles):
            return True
        stack.extend(children.get(pid, []))
    return False


def _dispatch_resume_into_pane(
    session_id: str,
    pane: str,
    sop_file: str,
    working_dir: str,
    engine: str,
) -> subprocess.CompletedProcess:
    """Resume a dead-agent instance into an allocated kreig pane via `dispatch`.

    Mirrors the Mac path: dispatch resumes the agent INTERACTIVELY (no `claude -p`),
    so the whole resume stays in ONE pane with tmux automation intact. `claude -p`
    is a banned anti-pattern — the wrapper re-redirects print-mode into a second
    pane, leaving an empty host pane behind.
    """
    dispatch_bin = Path(__file__).resolve().parents[1] / "cli-tools" / "bin" / "dispatch"
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env["TOKEN_API_DISPATCH_ORIGIN"] = "golden_throne"
    return subprocess.run(
        [
            str(dispatch_bin),
            "--direct",
            "--id",
            session_id,
            "--pane",
            pane,
            "--engine",
            engine,
            "--dir",
            working_dir,
            "--prompt-file",
            sop_file,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def _dispatch_deferred(pane: str, reason: str = "dispatch_deferred") -> dict:
    return {
        "success": False,
        "status": "deferred",
        "reason": reason,
        "pane": pane,
    }


def _looks_like_codex_skill_invocation(text: str) -> bool:
    # Keep this local: token-satellite is deployed as a standalone WSL service
    # and should not import tmuxctl internals from the Mac-side CLI package.
    stripped = (text or "").lstrip()
    if not stripped.startswith("$"):
        return False
    parts = stripped[1:].split(None, 1)
    if not parts:
        return False
    head = parts[0]
    return bool(head) and all(ch.isalnum() or ch in {"-", "_"} for ch in head)


def _tmux_send_payload_then_submit(
    pane: str,
    payload: str,
    *,
    clear_prompt: bool = False,
    enable_skill_sink: bool = False,
) -> None:
    """Send text and submit as separate tmux operations."""
    if clear_prompt:
        subprocess.run(["tmux", "send-keys", "-t", pane, "C-u"], check=True, timeout=5)
        time.sleep(0.3)
    subprocess.run(["tmux", "send-keys", "-t", pane, "-l", payload], check=True, timeout=5)
    if enable_skill_sink and _looks_like_codex_skill_invocation(payload):
        subprocess.run(["tmux", "send-keys", "-t", pane, "Tab"], check=True, timeout=5)
        time.sleep(0.3)
        subprocess.run(["tmux", "send-keys", "-t", pane, "C-m"], check=True, timeout=5)
        time.sleep(0.3)
    subprocess.run(["tmux", "send-keys", "-t", pane, "C-m"], check=True, timeout=5)


def _announce_to_mac():
    """Background thread: announce satellite startup to Mac Token-API."""
    import socket

    time.sleep(3)  # Let the server finish binding
    hostname = socket.gethostname()
    _send_lifecycle_event("startup", {"hostname": hostname, "port": 7777})
    logger.info("Startup lifecycle event sent to Mac")


def _send_lifecycle_event(event: str, details: dict | None = None):
    """Best-effort lifecycle signal to Mac Token-API."""
    payload = {
        "event_type": "satellite_lifecycle",
        "details": {
            "satellite": "wsl",
            "event": event,
            "timestamp": datetime.now().isoformat(),
            **(details or {}),
        },
    }
    try:
        http_requests.post(f"{MAC_API_BASE}/api/events/log", json=payload, timeout=2)
    except Exception as e:
        logger.warning(f"Lifecycle event '{event}' not delivered to Mac: {e}")


@app.on_event("startup")
async def startup_event():
    """Warm up TTS engine and start DeskFlow watchdog."""
    try:
        tts_engine.start()
    except Exception as e:
        logger.warning(f"TTS engine warm-up failed (will retry on first speak): {e}")
    deskflow_watchdog.start()
    # Announce to Mac in background thread (non-blocking, non-fatal)
    threading.Thread(target=_announce_to_mac, daemon=True).start()


@app.on_event("shutdown")
async def shutdown_event():
    """Best-effort shutdown signal before WSL/systemd takes the satellite down."""
    _send_lifecycle_event("shutdown", {"kvm_state": deskflow_watchdog.state})


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "token-satellite",
        "timestamp": datetime.now().isoformat(),
        "git_sha": _runtime_git_sha(),
        "runtime_path": str(REPO_ROOT),
        "tts_engine": "running"
        if tts_engine._process and tts_engine._process.poll() is None
        else "stopped",
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
        logger.info(
            f"ENFORCE: taskkill {exe} -> rc={result.returncode} stdout={result.stdout.strip()}"
        )
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

    logger.info(
        f"TTS: Speaking {len(request.message)} chars with {request.voice} (rate={request.rate})"
    )
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
    command: str  # pause, resume, stop, toggle, restart
    speed: float | None = None


class TTSSynthAndPlayRequest(BaseModel):
    message: str
    voice: str = "Microsoft David"
    rate: int = 0


class AudioPlayRequest(BaseModel):
    artifact_url: str
    artifact_id: str | None = None
    sha256: str | None = None
    format: str = "wav"


class TTSChunkRequest(BaseModel):
    session_id: str | None = None
    playback_id: str
    utterance_id: str | None = None
    chunk_id: str
    current_index: int | None = None
    current_chunk: str | None = None
    current_chunk_text: str | None = None
    next_index: int | None = None
    next_chunk: str | None = None
    next_chunk_text: str | None = None
    speed: float | None = None
    message: str | None = None
    voice: str = "Microsoft David"
    rate: int = 0


@app.post("/audio/play")
def audio_play(request: AudioPlayRequest):
    """Download and play a server-rendered WAV artifact. No local synthesis."""
    if request.format != "wav":
        raise HTTPException(status_code=400, detail="only wav artifacts are supported")
    if tts_engine.is_speaking or tts_engine._playing:
        raise HTTPException(status_code=409, detail="TTS engine is busy")

    cache_dir = Path(
        os.environ.get(
            "TOKEN_SATELLITE_AUDIO_CACHE", str(Path.home() / ".cache" / "token-satellite" / "audio")
        )
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    file_id = (
        request.artifact_id or hashlib.sha256(request.artifact_url.encode("utf-8")).hexdigest()[:32]
    )
    wav_path = cache_dir / f"{file_id}.wav"
    try:
        if not wav_path.exists():
            resp = http_requests.get(request.artifact_url, timeout=(5, 180))
            if resp.status_code != 200:
                return {"success": False, "error": f"artifact_download_http_{resp.status_code}"}
            wav_path.write_bytes(resp.content)
        data = wav_path.read_bytes()
        actual_sha = hashlib.sha256(data).hexdigest()
        if request.sha256 and actual_sha != request.sha256:
            try:
                wav_path.unlink()
            except OSError:
                pass
            return {"success": False, "error": "artifact_hash_mismatch", "sha256": actual_sha}
        win_path = subprocess.check_output(
            ["wslpath", "-w", str(wav_path)], text=True, timeout=5
        ).strip()
        result = tts_engine._play_wav_file(
            {
                "file_id": file_id,
                "wav_path_wsl": str(wav_path),
                "wav_path_win": win_path,
                "rendered_hash": request.sha256,
                "rendered_chars": None,
                "message_chars": None,
            }
        )
        result["transport"] = "openai_tts_wav_artifact"
        result["artifact_id"] = request.artifact_id
        result["sha256"] = actual_sha
        return result
    except Exception as exc:
        logger.warning(f"audio/play failed: {exc}")
        return {"success": False, "error": str(exc)}


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


@app.post("/tts/chunk")
def tts_chunk(request: TTSChunkRequest):
    """Uniform Token-OS chunk-dispatch surface for the WSL backend."""
    message = request.current_chunk or request.current_chunk_text or request.message or ""
    if not message:
        return {"success": False, "error": "empty_chunk", "chunk_id": request.chunk_id}
    if tts_engine.is_speaking or tts_engine._playing:
        raise HTTPException(status_code=409, detail="TTS engine is busy")
    logger.info(
        f"TTS chunk: {request.chunk_id} index={request.current_index} chars={len(message)} voice={request.voice}"
    )
    result = tts_engine.synth_and_speak(message, request.voice, request.rate)
    method = "skipped" if result.get("skipped") else "wsl_sapi_chunk"
    return {
        "success": result.get("success", False),
        "skipped": result.get("skipped", False),
        "method": method,
        "voice": request.voice,
        "session_id": request.session_id,
        "playback_id": request.playback_id,
        "chunk_id": request.chunk_id,
        "current_index": request.current_index,
        "next_index": request.next_index,
        "file_id": result.get("file_id"),
        "wav_path_win": result.get("wav_path_win"),
        "playback_pid": result.get("playback_pid"),
        "transport": result.get("transport"),
        "message_chars": len(message),
        "rendered_chars": result.get("rendered_chars"),
        "rendered_hash": result.get("rendered_hash"),
        "message_preview": message[:50],
    }


@app.post("/tts/synth-and-play")
def tts_synth_and_play(request: TTSSynthAndPlayRequest):
    """Synthesize text to WAV, then play that WAV artifact. Blocks until done."""
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
        "wav_path_win": result.get("wav_path_win"),
        "playback_pid": result.get("playback_pid"),
        "transport": result.get("transport"),
        "message_chars": len(request.message),
        "rendered_chars": result.get("rendered_chars"),
        "rendered_hash": result.get("rendered_hash"),
        "message_preview": request.message[:50],
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
        return {
            "ok": True,
            "exit_code": result.returncode,
            "stderr": result.stderr[:200] if result.stderr else None,
        }
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
    elif action == "reload":
        deskflow_watchdog.recovery_attempts = 0
        deskflow_watchdog.next_recovery_at = 0.0
        deskflow_watchdog._recover_connection(reason="manual_reload")
        return {"success": True, "action": "reload", "state": deskflow_watchdog.state}
    elif action == "stop":
        deskflow_watchdog.force_stop()
        return {"success": True, "action": "stop", "state": deskflow_watchdog.state}
    elif action == "hold":
        deskflow_watchdog.hold(request.hold_minutes)
        return {
            "success": True,
            "action": "hold",
            "minutes": request.hold_minutes,
            "state": deskflow_watchdog.state,
        }
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown action: {action}. Valid: start, stop, reload, hold",
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
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "IMPERIUM_TMUX_RAW": "1"},
        )
        if verify.returncode != 0 or not verify.stdout.strip():
            raise HTTPException(status_code=404, detail=f"Pane {pane} not found")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="tmux command timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"tmux check failed: {e}")

    if _tmux_pane_has_pending_input(pane):
        logger.info(f"TMUX: deferred send to {pane}; pane has pending user input")
        return _dispatch_deferred(pane)

    # Send text and submit separately so Codex/Claude receives a real Enter key.
    try:
        _tmux_send_payload_then_submit(pane, command, clear_prompt=not req.no_escape)
        logger.info(f"TMUX: Sent to {pane}: {command[:80]}")
        return {"success": True, "pane": pane, "command": command}
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="tmux send-keys timed out")
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"tmux send-keys failed: rc={e.returncode}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"tmux send-keys failed: {e}")


def _get_or_create_kreig_pane() -> str:
    """Split a new pane in the kreig window for an autonomous session.

    Each resume gets its own pane — kreig is a dynamic process stack,
    not a shared resource. Creates the kreig window if it doesn't exist.
    """
    # Check if kreig window exists
    result = subprocess.run(
        ["tmux", "list-panes", "-t", "main:kreig", "-F", "#{pane_id}"],
        capture_output=True,
        text=True,
        timeout=5,
        env={**os.environ, "IMPERIUM_TMUX_RAW": "1"},
    )
    if result.returncode == 0 and result.stdout.strip():
        # Window exists — split a new pane into it
        split = subprocess.run(
            ["tmux", "split-window", "-t", "main:kreig", "-d", "-P", "-F", "#{pane_id}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if split.returncode == 0 and split.stdout.strip():
            pane_id = split.stdout.strip()
            subprocess.run(
                ["tmux", "set-option", "-p", "-t", pane_id, "@PANE_TYPE", "kreig"],
                timeout=5,
            )
            logger.info(f"Golden Throne: split new kreig pane {pane_id}")
            return pane_id
        # split failed (too many panes?) — fall through to use first existing pane
        logger.warning("Golden Throne: split-window failed, reusing existing kreig pane")
        return result.stdout.strip().split("\n")[0]

    # Create kreig window (detached so it doesn't steal focus)
    subprocess.run(
        ["tmux", "new-window", "-t", "main", "-n", "kreig", "-d"],
        timeout=5,
    )

    # Get pane ID and tag it
    result = subprocess.run(
        ["tmux", "list-panes", "-t", "main:kreig", "-F", "#{pane_id}"],
        capture_output=True,
        text=True,
        timeout=5,
        env={**os.environ, "IMPERIUM_TMUX_RAW": "1"},
    )
    pane_id = result.stdout.strip().split("\n")[0]
    subprocess.run(
        ["tmux", "set-option", "-p", "-t", pane_id, "@PANE_TYPE", "kreig"],
        timeout=5,
    )
    logger.info(f"Golden Throne: created kreig window (pane {pane_id})")
    return pane_id


def _resolve_instance_pane(session_id: str) -> tuple[str | None, str | None]:
    """Resolve an instance UUID to its live ``(pane_id, role)`` on THIS host.

    The satellite is the sole owner of ``instance_id -> pane`` resolution for the
    panes on its own tmux server (panes are host-local). It shells the same
    ``tmuxctl resolve-instance`` primitive token-api uses locally, reading the
    pane's ``@INSTANCE_ID`` stamp. Fails closed: any miss, error, or unstamped/
    dead pane returns ``(None, None)`` so callers treat it as "instance gone"
    rather than sending to — or speaking the position of — a vanished pane.
    """
    if not session_id:
        return (None, None)
    tmuxctld_lib = Path(__file__).resolve().parents[1] / "tmuxctld" / "lib"
    cli_lib = Path(__file__).resolve().parents[1] / "cli-tools" / "lib"
    try:
        # sys.executable, not bare "python3" (the uv shim re-syncs the venv on every
        # spawn and fails closed on a corrupt venv). tmuxctl's CLI is stdlib-only.
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "tmuxctl.cli",
                "resolve-instance",
                session_id,
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            env={
                **os.environ,
                "PYTHONPATH": f"{tmuxctld_lib}{os.pathsep}{cli_lib}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
            },
        )
    except Exception:
        return (None, None)
    # Exit 1 is the not-found sentinel; --format json prints the payload on both
    # exit codes, so parse stdout regardless and trust the `found` flag.
    try:
        payload = json.loads((proc.stdout or "").strip() or "{}")
    except (ValueError, json.JSONDecodeError):
        return (None, None)
    if not payload.get("found"):
        return (None, None)
    pane_id = (payload.get("pane_id") or "").strip() or None
    role = (payload.get("pane_role") or "").strip() or None
    return (pane_id, role)


def _clip_one_line(text: str, max_len: int) -> str:
    compact = " ".join(str(text).split())
    if max_len <= 0:
        return ""
    if len(compact) <= max_len:
        return compact
    if max_len == 1:
        return "…"
    return compact[: max_len - 1].rstrip() + "…"


def _golden_throne_file_bridge_prompt(
    sop_file: str,
    *,
    prompt_summary: str | None = None,
    max_len: int = 200,
) -> str:
    """Short live-agent prompt for long/multi-line Golden Throne payloads.

    Mac token-api already generated the full prompt. For rubric-driven fires it
    also sends a one-line summary naming the missing criterion so the remote
    pane does not receive a generic "execute this SOP" injection.
    """
    if prompt_summary:
        tail = f". Run: cat {sop_file}, then address those criteria."
        return f"{_clip_one_line(prompt_summary, max_len - len(tail))}{tail}"
    text = f"Golden Throne follow-up. Run: cat {sop_file}, then resume this thread."
    return _clip_one_line(text, max_len)


@app.get("/tmux/resolve-instance")
async def tmux_resolve_instance(session_id: str):
    """Resolve a Claude instance UUID to its live pane on this satellite's host.

    Called by the Mac token-api when it needs the pane/role of a remote-hosted
    instance — panes are host-local, so the Mac never resolves them against its
    own tmux server. Returns ``found: false`` (not an error) when the pane is
    gone, so the caller can fail closed and mark the instance stopped.
    """
    pane_id, role = _resolve_instance_pane(session_id)
    return {
        "session_id": session_id,
        "found": pane_id is not None,
        "pane_id": pane_id,
        "pane_role": role,
    }


@app.post("/golden-throne/followup")
async def golden_throne_followup(req: GoldenThroneFollowupRequest):
    """Resume an idle Claude instance via tmux send-keys or claude --resume.

    Called by Mac token-api when the Golden Throne timer fires for a WSL instance.
    Transport detection: if the tmux pane has claude running, send-keys the SOP prompt.
    Otherwise, spawn `claude --resume` in the remote managed worker stack.
    """
    transport = "unknown"
    pane = req.tmux_pane

    if pane:
        # Check if pane exists and what's running in it
        try:
            verify = subprocess.run(
                ["tmux", "display-message", "-t", pane, "-p", "#{pane_current_command}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            current_cmd = verify.stdout.strip() if verify.returncode == 0 else ""
        except Exception:
            current_cmd = ""

        engine = req.engine.strip().lower()
        if engine not in {"codex", "claude"}:
            engine = "claude"
        # On Mac, pane_current_command shows version string (e.g. "2.1.88") not "claude"
        cmd_is_claude = current_cmd and (
            engine in current_cmd.lower()
            or (current_cmd[0:1].isdigit() and "." in current_cmd)  # version string
        )
        if cmd_is_claude or _tmux_pane_has_agent_process(pane, engine):
            # Claude is alive in the pane — send SOP prompt via send-keys
            try:
                if _tmux_pane_has_pending_input(pane):
                    logger.info(
                        f"Golden Throne: deferred SOP to {pane}; pane has pending user input"
                    )
                    return {
                        **_dispatch_deferred(pane),
                        "transport": "send-keys",
                        "session_id": req.session_id,
                    }
                # Keep live-agent injection to one prompt line plus a real submit.
                # Multi-line payloads paste as prompt newlines in Codex/Claude,
                # which can leave the follow-up unsent. Mirror Mac GT behavior:
                # write long/multi-line SOPs to a file and inject a short command.
                if len(req.prompt) <= 200 and "\n" not in req.prompt:
                    inject_prompt = req.prompt
                else:
                    sop_file = f"/tmp/golden-throne-sop-{req.session_id[:8]}.md"
                    Path(sop_file).write_text(req.prompt)
                    inject_prompt = _golden_throne_file_bridge_prompt(
                        sop_file,
                        prompt_summary=req.prompt_summary,
                    )
                _tmux_send_payload_then_submit(
                    pane,
                    inject_prompt,
                    clear_prompt=True,
                    enable_skill_sink=(engine == "codex"),
                )
                transport = "send-keys"
                logger.info(
                    f"Golden Throne: sent SOP to {pane} via send-keys (session {req.session_id[:12]})"
                )
            except Exception as e:
                logger.error(f"Golden Throne: send-keys failed for {pane}: {e}")
                raise HTTPException(status_code=500, detail=f"send-keys failed: {e}")
        else:
            # Claude not running — resume in kreig with SOP prompt
            try:
                kreig_pane = _get_or_create_kreig_pane()
                if _tmux_pane_has_pending_input(kreig_pane):
                    logger.info(
                        f"Golden Throne: deferred resume to {kreig_pane}; pane has pending user input"
                    )
                    return {
                        **_dispatch_deferred(kreig_pane),
                        "transport": "resume",
                        "session_id": req.session_id,
                    }
                working_dir = os.path.expanduser(req.working_dir)
                # Write SOP to a unique, short-lived temp file (avoids shell escaping
                # and predictable-path collisions between parallel retries); dispatch
                # reads it via --prompt-file and forwards it as the resume prompt, then
                # we remove it so the SOP does not persist on disk.
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    suffix=".md",
                    prefix=f"golden-throne-sop-{req.session_id[:8]}-",
                    dir="/tmp",
                    delete=False,
                ) as tmp:
                    tmp.write(req.prompt)
                    sop_file = tmp.name
                try:
                    resume_proc = _dispatch_resume_into_pane(
                        req.session_id, kreig_pane, sop_file, working_dir, engine
                    )
                finally:
                    Path(sop_file).unlink(missing_ok=True)
                if resume_proc.returncode != 0:
                    raise RuntimeError(
                        f"dispatch resume rc={resume_proc.returncode}: "
                        f"{(resume_proc.stderr or '').strip()[:200]}"
                    )
                transport = "dispatch-resume"
                logger.info(
                    f"Golden Throne: resumed {req.session_id[:12]} via dispatch in kreig "
                    f"pane={kreig_pane} ({engine}, interactive)"
                )
            except Exception as e:
                logger.error(f"Golden Throne: resume failed for {req.session_id[:12]}: {e}")
                raise HTTPException(status_code=500, detail=f"resume failed: {e}")
    else:
        # No live pane resolved for this instance on this host — fail closed.
        # The Mac resolves via /tmux/resolve-instance before dispatching, so an
        # empty pane here means the instance is gone (or the pane vanished in the
        # resolve->dispatch race). Never fabricate a target or resume blindly.
        logger.info(
            f"Golden Throne: no live pane for {req.session_id[:12]} on this host; "
            f"reporting instance_gone"
        )
        return {
            "success": False,
            "status": "instance_gone",
            "session_id": req.session_id,
        }

    return {"success": True, "transport": transport, "session_id": req.session_id}


@app.post("/runtime/refresh")
async def runtime_refresh(req: RuntimeRefreshRequest, request: Request):
    """Refresh WSL-local Token-OS runtime and AHK cache to an authenticated SHA."""
    configured = os.environ.get(RUNTIME_REFRESH_SECRET_ENV, "").strip()
    if not configured:
        logger.error("runtime refresh requested but %s is unset", RUNTIME_REFRESH_SECRET_ENV)
        raise HTTPException(status_code=503, detail="runtime refresh not configured")

    auth = request.headers.get("Authorization", "")
    presented = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if not presented or not hmac.compare_digest(presented, configured):
        logger.warning("runtime refresh rejected: bad/missing bearer token")
        raise HTTPException(status_code=401, detail="invalid credentials")

    sha = req.sha.strip()
    if not re.fullmatch(r"[0-9A-Fa-f]{7,64}", sha):
        raise HTTPException(status_code=400, detail="sha must be a git commit hash")
    if not RUNTIME_REFRESH_HELPER.exists():
        raise HTTPException(
            status_code=503, detail=f"refresh helper not found: {RUNTIME_REFRESH_HELPER}"
        )

    manifest = {
        "sha": sha,
        "changed_paths": [p for p in req.changed_paths if isinstance(p, str) and p],
        "requested_at": datetime.now().isoformat(),
    }
    handle = tempfile.NamedTemporaryFile(
        "w", prefix="token-runtime-refresh-", suffix=".json", delete=False, encoding="utf-8"
    )
    try:
        json.dump(manifest, handle)
        handle.write("\n")
        manifest_path = handle.name
    finally:
        handle.close()

    log_path = Path("/tmp/token-satellite-refresh.log")
    log_handle = log_path.open("a", encoding="utf-8")
    try:
        subprocess.Popen(
            [str(RUNTIME_REFRESH_HELPER), sha, manifest_path],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_handle.close()

    logger.info(
        "runtime refresh scheduled: sha=%s paths=%d", sha[:9], len(manifest["changed_paths"])
    )
    return {"ok": True, "refresh": "scheduled", "sha": sha, "manifest": manifest_path}


@app.post("/restart")
async def restart_satellite(pull: bool = False) -> dict[str, object]:
    """Exit for systemd restart, reloading code from the WSL-local runtime.

    The satellite runs from the local runtime checkout (``WorkingDirectory`` ==
    ``Path(__file__).resolve().parents[1]``). CI/CD updates that checkout through
    the authenticated ``/runtime/refresh`` path; a plain restart reloads the
    already-synced local code with no pull, so ``pull`` defaults to **False**.

    Historically this pulled ``~/Scripts`` — the dead pre-rename Token-OS
    namespace, a path that no longer exists on this host, so the pull silently
    failed on every self-restart. When a pull IS requested we now ff the repo the
    satellite actually runs from, never that dusty namespace.

    Legacy TUI restart signal files are intentionally gone. The live cockpit is
    `/ui/ops`; browser refresh is handled by token-restart on Mac and by the
    frontend build-id auto-reload loop for remote browsers.
    """
    result = {"pull": None, "browser_gui": "frontend_auto_reload", "restarting": True}
    _send_lifecycle_event("restart_requested", {"pull": pull})

    # 1. Git pull (opt-in). Target the repo this process runs from — the local
    # runtime checkout — not the retired ~/Scripts namespace.
    if pull:
        try:
            proc = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "pull", "--ff-only"],
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

    # 2. Shutdown cleanly
    deskflow_watchdog.stop()
    tts_engine.shutdown()

    # 3. Schedule exit after response is sent (systemd Restart=always brings us back)
    def delayed_exit():
        time.sleep(0.5)
        logger.info("RESTART: Exiting for systemd restart")
        os._exit(0)

    threading.Thread(target=delayed_exit, daemon=True).start()

    logger.info(f"RESTART: pull={result['pull']}, exiting in 0.5s")
    return result


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=7777)
