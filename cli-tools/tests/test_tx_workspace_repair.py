"""`tx start` self-heal must inspect the CANONICAL 5-page council topology, not
the retired per-fleet `legion` page.

`workspace_needs_repair` (cli-tools/bin/tx) is the drift detector that runs on
every attach via `ensure_workspace_before_attach`. When the topology was merged
to (palace, somnium, council, mechanicus, reservists), the colon-label sweep
fixed `legion:` labels but left bare `legion` as a tmux *window-name target* in
this check (`tmux list-panes -t main:legion`). Against the live council layout
that window no longer exists, so the check returned "needs repair" on EVERY
healthy attach — a permanent false self-heal warning.

These tests drive the real `tx` script with a fake tmux that reports a HEALTHY
council topology and assert: (1) no malformed-layout warning is emitted, and
(2) the custodes check reads the `council` window, never `legion`. Never touches
a live tmux server — the fake is a PATH stub that logs every call.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
TX = ROOT / "bin" / "tx"

# A healthy canonical workspace: window list + per-window pane projections that
# satisfy every assertion in workspace_needs_repair (palace=4, somnium=5,
# council=5 with council:custodes at .1, mechanicus>=1 with the
# fabricator-general at .1, reservists present).
_FAKE_TMUX = r"""#!/usr/bin/env bash
trap '' PIPE
printf '%s\n' "$*" >> "$TX_FAKE_TMUX_LOG"
cmd="${1:-}"; shift || true
case "$cmd" in
  has-session) exit 0 ;;
  list-windows)
    printf 'palace\nsomnium\ncouncil\nmechanicus\nreservists\n'
    exit 0 ;;
  list-panes)
    target=""
    while [[ $# -gt 0 ]]; do
      if [[ "$1" == "-t" ]]; then target="$2"; shift 2; continue; fi
      shift
    done
    case "$target" in
      *:palace)     printf 'palace:W\npalace:N\npalace:S\npalace:E\n' ;;
      *:somnium)    printf 'somnium:W\nsomnium:N\nsomnium:NE\nsomnium:S\nsomnium:SE\n' ;;
      *:council)    printf 'council:custodes\ncouncil:pax\ncouncil:malcador\ncouncil:true-terminal\ncouncil:administratum\n' ;;
      *:mechanicus) printf 'mechanicus:fabricator-general\nmechanicus:orchestrator\n' ;;
      *:reservists) printf 'reservists:civic\nreservists:token-os\n' ;;
      *)            : ;;
    esac
    exit 0 ;;
  attach-session) exit 0 ;;
  display-message) printf 'main\n' ;;
  *) exit 0 ;;
esac
exit 0
"""


def _run_start(tmp_path: pathlib.Path) -> tuple[subprocess.CompletedProcess[str], pathlib.Path]:
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    fake_tmux = stub / "tmux"
    fake_tmux.write_text(_FAKE_TMUX)
    fake_tmux.chmod(0o755)

    fake_log = tmp_path / "tmux-calls.log"

    env = {k: v for k, v in os.environ.items()}
    # `tx start` short-circuits when already inside tmux ($TMUX set); the test
    # runner itself may run in a pane, so strip it to force the attach path.
    env.pop("TMUX", None)
    env["PATH"] = f"{stub}:{env['PATH']}"
    env["TX_FAKE_TMUX_LOG"] = str(fake_log)
    env["TX_INVOCATION_LOG"] = str(tmp_path / "tx-invocations.log")
    env["IMPERIUM_MACHINE"] = "test"

    proc = subprocess.run(
        [str(TX), "start"],
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
    )
    return proc, fake_log


def test_healthy_council_topology_emits_no_repair_warning(tmp_path: pathlib.Path) -> None:
    proc, _log = _run_start(tmp_path)
    out = proc.stdout + proc.stderr
    assert "looks malformed" not in out, out
    assert "Attaching AS-IS" not in out, out


def test_custodes_check_reads_council_window_not_legion(tmp_path: pathlib.Path) -> None:
    _proc, log = _run_start(tmp_path)
    calls = log.read_text()
    # The custodes-seat assertion must inspect the council page...
    assert "list-panes -t main:council" in calls, calls
    # ...and the retired legion window must never be queried.
    assert "main:legion" not in calls, calls


def test_start_refuses_to_create_workspace_when_vault_unmounted(tmp_path: pathlib.Path) -> None:
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    fake_tmux = stub / "tmux"
    fake_tmux.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\n\' "$*" >> "$TX_FAKE_TMUX_LOG"\n'
        'case "${1:-}" in\n'
        "  has-session) exit 1 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    fake_tmux.chmod(0o755)
    fake_log = tmp_path / "tmux-calls.log"
    missing_vault = tmp_path / "Imperium" / "Imperium-ENV"

    env = {k: v for k, v in os.environ.items()}
    env.pop("TMUX", None)
    env["PATH"] = f"{stub}:{env['PATH']}"
    env["TX_FAKE_TMUX_LOG"] = str(fake_log)
    env["TX_INVOCATION_LOG"] = str(tmp_path / "tx-invocations.log")
    env["TX_NAS_WAIT_PATH"] = str(missing_vault)
    env["TX_NAS_WAIT_TIMEOUT"] = "0"
    env["IMPERIUM_MACHINE"] = "test"

    proc = subprocess.run(
        [str(TX), "start"],
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )

    assert proc.returncode == 1
    assert "refusing to build a ghost-town tmux workspace" in proc.stderr
    assert "Creating workspace session" not in proc.stderr
    assert "has-session -t main" in fake_log.read_text()
