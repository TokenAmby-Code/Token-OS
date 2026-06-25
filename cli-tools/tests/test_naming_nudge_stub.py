from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "claude-config" / "hooks" / "naming-nudge.sh"


def test_naming_nudge_hook_is_noop_compatibility_stub(tmp_path: Path) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    marker = tmp_path / "curl-called"
    curl = fakebin / "curl"
    quoted_marker = shlex.quote(str(marker))
    curl.write_text(f"#!/usr/bin/env bash\ntouch {quoted_marker}\nexit 99\n", encoding="utf-8")
    curl.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}{os.pathsep}{env.get('PATH', '')}"

    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        input=b'{"session_id":"stale-stop"}',
        env=env,
        check=False,
        capture_output=True,
    )

    assert proc.returncode == 0
    assert not marker.exists(), "compatibility stub must not call Token-API/curl"
