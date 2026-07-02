"""push-mobile must deploy mobile files from the checkout it is run from.

nas-path.sh intentionally exports TOKEN_OS to the live runtime checkout. That is
correct for services, but branch worktrees must still be able to push their own
mobile config during validation.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PUSH_MOBILE = ROOT / "cli-tools" / "bin" / "push-mobile"


def test_push_mobile_bashrc_uses_script_checkout_not_token_os(tmp_path: Path) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    calls = tmp_path / "calls.log"
    (fakebin / "ssh").write_text(f'#!/usr/bin/env bash\necho ssh "$@" >> {calls!s}\nexit 0\n')
    (fakebin / "scp").write_text(f'#!/usr/bin/env bash\necho scp "$@" >> {calls!s}\nexit 0\n')
    (fakebin / "ssh").chmod(0o755)
    (fakebin / "scp").chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["TOKEN_OS"] = str(tmp_path / "wrong-live-runtime")

    subprocess.run([str(PUSH_MOBILE), "-b"], env=env, check=True, capture_output=True, text=True)

    log = calls.read_text()
    assert f"scp -q {ROOT / 'mobile' / 'termux-bashrc-template'} phone:~/.bashrc" in log
    assert "wrong-live-runtime" not in log
