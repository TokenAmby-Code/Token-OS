"""`tts` CLI delivery contract.

The command is an enforcement channel, so exit 0 must mean Token-API reported
audible delivery. Transport acceptance, queue acceptance, or a backgrounded curl
process is not enough.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TTS = ROOT / "cli-tools" / "bin" / "tts"


def _write_fake_curl(tmp_path: Path, response: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "curl-argv.txt"
    curl = bin_dir / "curl"
    curl.write_text(
        f"""#!/usr/bin/env bash
printf '%s\\n' "$*" > {str(log)!r}
printf '%s\\n' {response!r}
exit 0
"""
    )
    curl.chmod(0o755)
    return bin_dir


def _run_tts(tmp_path: Path, response: str, *args: str) -> subprocess.CompletedProcess[str]:
    fake_bin = _write_fake_curl(tmp_path, response)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TOKEN_API_URL": "http://token-api.test:7777",
    }
    return subprocess.run(
        [str(TTS), "--direct", *args],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_tts_cli_exits_nonzero_when_api_reports_no_audio_delivery(tmp_path: Path) -> None:
    proc = _run_tts(
        tmp_path,
        '{"delivered":false,"audio_delivered":false,"tts":{"reason":"phone_playback_unconfirmed"}}',
        "silent success regression",
    )

    assert proc.returncode != 0
    assert "phone_playback_unconfirmed" in proc.stderr
    assert "/api/notify" in (tmp_path / "curl-argv.txt").read_text()
    assert "/api/notify/queue" not in (tmp_path / "curl-argv.txt").read_text()


def test_tts_cli_exits_zero_only_on_reported_audio_delivery(tmp_path: Path) -> None:
    proc = _run_tts(
        tmp_path,
        '{"delivered":true,"audio_delivered":true,"route":"wsl_sapi_chunk"}',
        "audible line",
    )

    assert proc.returncode == 0
    assert proc.stderr == ""
