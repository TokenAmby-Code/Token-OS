from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "agent-cmd"


def _copy_agent_cmd(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "cli-tools"
    bin_dir = root / "bin"
    lib_dir = root / "lib"
    bin_dir.mkdir(parents=True)
    lib_dir.mkdir()
    script = bin_dir / "agent-cmd"
    script.write_text(SCRIPT.read_text())
    script.chmod(0o755)
    (lib_dir / "nas-path.sh").write_text("")
    (lib_dir / "tmux-guard.sh").write_text("tmux_wait_for_clear() { return 0; }\n")
    return script, bin_dir


def test_agent_cmd_instance_resolves_with_tmuxctl(tmp_path: Path):
    script, bin_dir = _copy_agent_cmd(tmp_path)
    log = tmp_path / "tmuxctl.log"
    tmuxctl = bin_dir / "tmuxctl"
    tmuxctl.write_text(
        f"""#!/usr/bin/env bash
printf '%s\n' "$*" >> {log}
if [[ "$1" == "resolve-instance" ]]; then printf '%%321\n'; exit 0; fi
exit 1
"""
    )
    tmuxctl.chmod(0o755)

    proc = subprocess.run(
        [str(script), "--instance", "inst-abc", "--resolve-only"],
        text=True,
        capture_output=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"},
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "%321"
    assert log.read_text().strip() == "resolve-instance --format physical inst-abc"


def test_agent_cmd_skill_delegates_to_tmuxctl_invoke_skill_submit(tmp_path: Path):
    script, bin_dir = _copy_agent_cmd(tmp_path)
    log = tmp_path / "tmuxctl.log"
    tmuxctl = bin_dir / "tmuxctl"
    tmuxctl.write_text(
        f"""#!/usr/bin/env bash
printf '%s\n' "$*" >> {log}
if [[ "$1" == "invoke-skill" ]]; then printf 'ok'; exit 0; fi
exit 1
"""
    )
    tmuxctl.chmod(0o755)

    proc = subprocess.run(
        [str(script), "--skill", "preplan", "--arguments", "args", "--pane", "%9"],
        text=True,
        capture_output=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"},
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert log.read_text().strip() == "invoke-skill preplan --pane %9 --arguments args --submit"
