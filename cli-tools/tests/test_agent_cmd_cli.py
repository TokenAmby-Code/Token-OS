from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "agent-cmd"


def _copy_agent_cmd(tmp_path: Path):
    root = tmp_path / "cli-tools"
    b = root / "bin"
    lib_dir = root / "lib"
    b.mkdir(parents=True)
    lib_dir.mkdir()
    s = b / "agent-cmd"
    s.write_text(SCRIPT.read_text())
    s.chmod(0o755)
    (lib_dir / "nas-path.sh").write_text("")
    (lib_dir / "tmux-guard.sh").write_text("tmux_wait_for_clear() { return 0; }\n")
    return s, b


def test_agent_cmd_instance_resolves_with_tmuxctl(tmp_path: Path):
    s, b = _copy_agent_cmd(tmp_path)
    log = tmp_path / "tmuxctl.log"
    t = b / "tmuxctl"
    t.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> {log}\nif [[ "$1" == "resolve-instance" ]]; then printf "%%321\\n"; exit 0; fi\nexit 1\n'
    )
    t.chmod(0o755)
    p = subprocess.run(
        [str(s), "--instance", "inst-abc", "--resolve-only"],
        text=True,
        capture_output=True,
        env={**os.environ, "PATH": f"{b}:{os.environ.get('PATH', '')}"},
        check=False,
    )
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == "%321"
    assert log.read_text().strip() == "resolve-instance --format physical inst-abc"


def test_agent_cmd_skill_delegates_to_tmuxctl_invoke_skill_submit(tmp_path: Path):
    s, b = _copy_agent_cmd(tmp_path)
    log = tmp_path / "tmuxctl.log"
    t = b / "tmuxctl"
    t.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> {log}\nif [[ "$1" == "invoke-skill" ]]; then printf ok; exit 0; fi\nexit 1\n'
    )
    t.chmod(0o755)
    p = subprocess.run(
        [str(s), "--skill", "preplan", "--arguments", "args", "--pane", "%9"],
        text=True,
        capture_output=True,
        env={**os.environ, "PATH": f"{b}:{os.environ.get('PATH', '')}"},
        check=False,
    )
    assert p.returncode == 0, p.stderr
    assert log.read_text().strip() == "invoke-skill preplan --pane %9 --arguments args --submit"
