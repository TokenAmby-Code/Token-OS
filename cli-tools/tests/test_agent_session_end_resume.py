from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_TOOLS = REPO_ROOT / "cli-tools"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.skipif(shutil.which("jq") is None, reason="agent resume hook uses jq")
@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_agent_session_end_resume_stages_generic_dispatch_command(
    tmp_path: Path, engine: str
) -> None:
    # Unique per-test pane token so the hardcoded /tmp sentinels the script writes
    # (/tmp/agent-resume-<pane>, /tmp/claude-resume-<pane>) never collide across
    # xdist workers or parametrizations. The script does not honor $TMPDIR.
    pane = f"%resumehook-{engine}"

    db = tmp_path / "agents.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE instances (
                   id TEXT PRIMARY KEY,
                   tmux_pane TEXT,
                   pane_label TEXT,
                   status TEXT,
                   last_activity TEXT
               )"""
        )
        conn.execute(
            """INSERT INTO instances (id, tmux_pane, pane_label, status, last_activity)
               VALUES ('iid-generic', ?, 'legion:worker', 'idle', '2026-06-22T12:00:00')""",
            (pane,),
        )

    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _write_executable(
        fakebin / "tmux",
        """#!/usr/bin/env bash
if [[ "$*" == *"@INSTANCE_ID"* ]]; then
  printf 'iid-generic\n'
elif [[ "$*" == *"@PANE_ID"* ]]; then
  printf 'legion:worker\n'
fi
""",
    )

    sentinel = Path(f"/tmp/agent-resume-{pane}")
    legacy = Path(f"/tmp/claude-resume-{pane}")
    sentinel.unlink(missing_ok=True)
    legacy.unlink(missing_ok=True)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fakebin}:{env.get('PATH', '')}",
            "TMUX_PANE": pane,
            "TOKEN_API_DB": str(db),
            "TOKEN_API_SESSION_ID": "iid-generic",
        }
    )
    payload = {"session_id": "claude-or-codex-native-session"}
    subprocess.run(
        ["bash", str(CLI_TOOLS / "scripts" / "agent-session-end-resume.sh"), engine],
        input=json.dumps(payload),
        text=True,
        env=env,
        check=True,
    )

    try:
        assert (
            sentinel.read_text(encoding="utf-8")
            == f"{engine}\n\ndispatch --id iid-generic --pane self\n"
        )
        assert legacy.read_text(encoding="utf-8") == "dispatch --id iid-generic --pane self"
    finally:
        sentinel.unlink(missing_ok=True)
        legacy.unlink(missing_ok=True)
