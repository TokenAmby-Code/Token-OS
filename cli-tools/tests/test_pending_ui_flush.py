"""Acceptance tests for `pending-ui-flush` — the guarded pane-branding queue.

These pin the P0 contract from the 2026-06-09 incident (auto-naming/dispatch
branding send-keys'd slash-commands into the Emperor's actively-typed pane). The
queue lives at ~/.claude/pending-ui-cmds/<N> (filename N = tmux pane number) and
each line is a slash-command to send-keys into pane %N. Four origin defects, four
guards, one test each:

  1. Typing-guard HOLD — never send-keys while a human is typing; hold + re-check,
     never drop, never race.
  2. Drain + expire — entries removed on flush; nothing older than a short TTL,
     targeting a dead pane, or from a legacy never-drained line ever replays.
  3. Pane epoch binding — entries are tagged with the session (generation) that
     enqueued them; a recycled pane id never inherits a prior occupant's branding.
  4. Never brand a human-attended pane — a pane a live client is viewing is held,
     not written.

The tool's tmux + agent-cmd dependencies are injected via env-pointed fakes
(PENDING_UI_TMUX / PENDING_UI_CLAUDE_CMD), so every case is deterministic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "bin" / "pending-ui-flush"

# A current, valid session epoch (a Claude session UUID shape).
SID = "11111111-2222-3333-4444-555555555555"
OTHER_SID = "99999999-8888-7777-6666-555555555555"


# --------------------------------------------------------------------------- #
# Fakes: a tmux that answers reads from env, and a agent-cmd that logs sends. #
# --------------------------------------------------------------------------- #

_FAKE_TMUX = r"""#!/usr/bin/env bash
# Minimal tmux double driven by FAKE_* env vars.
#   FAKE_ALIVE        newline/space list of alive pane ids (list-panes)
#   FAKE_ACTIVITY     integer epoch reported for #{client_activity}
#   FAKE_ATTEND       two-char flags returned for `display-message -t <pane>`
#   FAKE_CLIENTS      integer client count for `list-clients -t <pane>`
#   FAKE_TMUX_LOG     file to append send-keys invocations to
args="$*"
case "$1" in
  list-panes)
    for p in ${FAKE_ALIVE:-}; do printf '%s\n' "$p"; done
    ;;
  display-message)
    if printf '%s' "$args" | grep -q ' -t '; then
      printf '%s\n' "${FAKE_ATTEND:-00}"
    else
      printf '%s %s\n' "${FAKE_ACTIVITY:-0}" "${FAKE_LAST_KEY:-}"
    fi
    ;;
  list-clients)
    n="${FAKE_CLIENTS:-0}"
    i=0; while [ "$i" -lt "$n" ]; do printf 'x\n'; i=$((i+1)); done
    ;;
  send-keys)
    [ -n "${FAKE_TMUX_LOG:-}" ] && printf '%s\n' "$args" >> "$FAKE_TMUX_LOG"
    ;;
  *) : ;;
esac
exit 0
"""

_FAKE_CLAUDE_CMD = r"""#!/usr/bin/env bash
# Logs each send and succeeds (or fails if FAKE_SEND_FAIL=1).
[ -n "${FAKE_SEND_LOG:-}" ] && printf '%s\n' "$*" >> "$FAKE_SEND_LOG"
[ "${FAKE_SEND_FAIL:-0}" = "1" ] && exit 1
exit 0
"""


def _mkfakes(tmp_path: Path) -> dict[str, Path]:
    binp = tmp_path / "bin"
    binp.mkdir(exist_ok=True)
    tmux = binp / "tmux"
    tmux.write_text(_FAKE_TMUX, encoding="utf-8")
    tmux.chmod(0o755)
    cc = binp / "agent-cmd"
    cc.write_text(_FAKE_CLAUDE_CMD, encoding="utf-8")
    cc.chmod(0o755)
    return {
        "tmux": tmux,
        "claude_cmd": cc,
        "tmux_log": tmp_path / "tmux.log",
        "send_log": tmp_path / "send.log",
    }


def _run(
    args: list[str], *, qdir: Path, fakes: dict, extra_env: dict | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PENDING_UI_DIR": str(qdir),
        "PENDING_UI_TMUX": str(fakes["tmux"]),
        "PENDING_UI_CLAUDE_CMD": str(fakes["claude_cmd"]),
        "FAKE_TMUX_LOG": str(fakes["tmux_log"]),
        "FAKE_SEND_LOG": str(fakes["send_log"]),
        # Keep holds short so the typing/attended cases finish quickly.
        "PENDING_UI_HOLD_SECONDS": "1",
        "PENDING_UI_POLL_SECONDS": "0.2",
        "PENDING_UI_TTL_SECONDS": "300",
        "PENDING_UI_TYPING_WINDOW": "10",
        **(extra_env or {}),
    }
    proc = subprocess.run(
        [sys.executable, str(TOOL), *args], text=True, capture_output=True, check=False, env=env
    )
    return proc


def _summary(proc: subprocess.CompletedProcess[str]) -> dict:
    # Tool prints a one-line JSON summary on stdout.
    line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "{}"
    return json.loads(line)


def _sends(fakes: dict) -> str:
    return fakes["send_log"].read_text() if fakes["send_log"].exists() else ""


def _qfile(qdir: Path, pane: str) -> Path:
    return qdir / pane.lstrip("%")


# --------------------------------------------------------------------------- #
# enqueue                                                                      #
# --------------------------------------------------------------------------- #


def test_enqueue_writes_epoch_and_timestamp(tmp_path: Path) -> None:
    qdir = tmp_path / "q"
    qdir.mkdir()
    fakes = _mkfakes(tmp_path)
    proc = _run(
        ["enqueue", "--pane", "%41", "--session", SID, "--rename", "foo"],
        qdir=qdir,
        fakes=fakes,
    )
    assert proc.returncode == 0, proc.stderr
    lines = _qfile(qdir, "%41").read_text().splitlines()
    assert len(lines) == 1
    ts, sid, pane, cmd = lines[0].split(None, 3)
    assert int(ts) > 0 and abs(int(ts) - int(time.time())) < 30
    assert sid == SID
    assert pane == "%41"
    assert cmd == "/rename foo"


# --------------------------------------------------------------------------- #
# Defect 1 / acceptance 1+3: typing-guard HOLD — never inject while typing.    #
# --------------------------------------------------------------------------- #


def test_flush_holds_and_keeps_entry_while_human_typing(tmp_path: Path) -> None:
    qdir = tmp_path / "q"
    qdir.mkdir()
    fakes = _mkfakes(tmp_path)
    _qfile(qdir, "%41").write_text(f"{int(time.time())} {SID} %41 /rename held\n")
    proc = _run(
        ["flush", "--pane", "%41", "--session", SID],
        qdir=qdir,
        fakes=fakes,
        # Human typed "just now" -> typing guard active. Pane alive.
        extra_env={
            "FAKE_ALIVE": "%41",
            "FAKE_ACTIVITY": str(int(time.time()) - 2),
            "FAKE_ATTEND": "11",
            "FAKE_CLIENTS": "1",
        },
    )
    assert proc.returncode == 0, proc.stderr
    # Zero keystrokes injected, and the command is NOT dropped — it stays queued.
    assert _sends(fakes) == ""
    assert _qfile(qdir, "%41").exists()
    assert "/rename held" in _qfile(qdir, "%41").read_text()
    assert _summary(proc).get("sent", 0) == 0
    assert _summary(proc).get("held", 0) >= 1


def test_flush_does_not_hold_unattended_pane_for_global_typing(tmp_path: Path) -> None:
    qdir = tmp_path / "q"
    qdir.mkdir()
    fakes = _mkfakes(tmp_path)
    _qfile(qdir, "%41").write_text(f"{int(time.time())} {SID} %41 /rename clear\n")
    proc = _run(
        ["flush", "--pane", "%41", "--session", SID],
        qdir=qdir,
        fakes=fakes,
        extra_env={
            "FAKE_ALIVE": "%41",
            "FAKE_ACTIVITY": str(int(time.time()) - 2),
            "FAKE_ATTEND": "00",
            "FAKE_CLIENTS": "0",
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert "/rename clear" in _sends(fakes)
    assert _summary(proc).get("sent", 0) == 1


def test_flush_sends_and_drains_when_idle(tmp_path: Path) -> None:
    qdir = tmp_path / "q"
    qdir.mkdir()
    fakes = _mkfakes(tmp_path)
    _qfile(qdir, "%41").write_text(
        f"{int(time.time())} {SID} %41 /rename foo\n{int(time.time())} {SID} %41 /rename held\n"
    )
    proc = _run(
        ["flush", "--pane", "%41", "--session", SID],
        qdir=qdir,
        fakes=fakes,
        # No recent typing, not attended, pane alive.
        extra_env={
            "FAKE_ALIVE": "%41",
            "FAKE_ACTIVITY": "1",
            "FAKE_ATTEND": "00",
            "FAKE_CLIENTS": "0",
        },
    )
    assert proc.returncode == 0, proc.stderr
    sends = _sends(fakes)
    assert "/rename foo" in sends and "/rename held" in sends
    assert "%41" in sends
    # Drained on success — file gone (or empty).
    qf = _qfile(qdir, "%41")
    assert (not qf.exists()) or qf.read_text().strip() == ""
    assert _summary(proc).get("sent", 0) == 2


# --------------------------------------------------------------------------- #
# Defect 2 / acceptance 2: drain + expire (TTL, legacy, dead pane).           #
# --------------------------------------------------------------------------- #


def test_flush_purges_entry_older_than_ttl(tmp_path: Path) -> None:
    qdir = tmp_path / "q"
    qdir.mkdir()
    fakes = _mkfakes(tmp_path)
    old = int(time.time()) - 4000  # well past the 300s TTL
    _qfile(qdir, "%41").write_text(f"{old} {SID} %41 /rename stale\n")
    proc = _run(
        ["flush", "--pane", "%41", "--session", SID],
        qdir=qdir,
        fakes=fakes,
        extra_env={"FAKE_ALIVE": "%41", "FAKE_ACTIVITY": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    assert _sends(fakes) == ""  # expired -> never replayed
    qf = _qfile(qdir, "%41")
    assert (not qf.exists()) or qf.read_text().strip() == ""
    assert _summary(proc).get("purged", 0) >= 1


def test_flush_purges_legacy_untagged_line(tmp_path: Path) -> None:
    qdir = tmp_path / "q"
    qdir.mkdir()
    fakes = _mkfakes(tmp_path)
    # Pre-fix format: "%<pane> <cmd>" with no timestamp/epoch — the April backlog.
    _qfile(qdir, "%41").write_text("%41 /rename legacy\n")
    proc = _run(
        ["flush", "--pane", "%41", "--session", SID],
        qdir=qdir,
        fakes=fakes,
        extra_env={"FAKE_ALIVE": "%41", "FAKE_ACTIVITY": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    assert _sends(fakes) == ""  # legacy rename never fires
    assert _summary(proc).get("purged", 0) >= 1


def test_flush_purges_entry_for_dead_pane(tmp_path: Path) -> None:
    qdir = tmp_path / "q"
    qdir.mkdir()
    fakes = _mkfakes(tmp_path)
    _qfile(qdir, "%41").write_text(f"{int(time.time())} {SID} %41 /rename held\n")
    proc = _run(
        ["flush", "--pane", "%41", "--session", SID],
        qdir=qdir,
        fakes=fakes,
        # %41 is NOT in the alive set -> pane is dead/recycled-away.
        extra_env={"FAKE_ALIVE": "%7 %9", "FAKE_ACTIVITY": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    assert _sends(fakes) == ""
    assert _summary(proc).get("purged", 0) >= 1


# --------------------------------------------------------------------------- #
# Defect 3 / acceptance 2: pane epoch binding — recycled pane never inherits.  #
# --------------------------------------------------------------------------- #


def test_flush_purges_entry_from_foreign_session_epoch(tmp_path: Path) -> None:
    qdir = tmp_path / "q"
    qdir.mkdir()
    fakes = _mkfakes(tmp_path)
    # Entry enqueued by a PRIOR occupant of %41 (different session epoch).
    _qfile(qdir, "%41").write_text(f"{int(time.time())} {OTHER_SID} %41 /rename stale\n")
    proc = _run(
        ["flush", "--pane", "%41", "--session", SID],  # current occupant is SID
        qdir=qdir,
        fakes=fakes,
        extra_env={"FAKE_ALIVE": "%41", "FAKE_ACTIVITY": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    assert _sends(fakes) == ""  # prior occupant's branding does not replay
    assert _summary(proc).get("purged", 0) >= 1


# --------------------------------------------------------------------------- #
# Defect 4 / acceptance 4: never write to a human-attended pane.               #
# --------------------------------------------------------------------------- #


def test_flush_holds_when_pane_has_live_client_attached(tmp_path: Path) -> None:
    qdir = tmp_path / "q"
    qdir.mkdir()
    fakes = _mkfakes(tmp_path)
    _qfile(qdir, "%41").write_text(f"{int(time.time())} {SID} %41 /rename held\n")
    proc = _run(
        ["flush", "--pane", "%41", "--session", SID],
        qdir=qdir,
        fakes=fakes,
        # Not typing, but a live client is viewing %41 (active pane + attached).
        extra_env={
            "FAKE_ALIVE": "%41",
            "FAKE_ACTIVITY": "1",
            "FAKE_ATTEND": "11",
            "FAKE_CLIENTS": "2",
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert _sends(fakes) == ""  # branding never writes to an attended pane
    assert _qfile(qdir, "%41").exists()  # kept, not dropped
    assert _summary(proc).get("sent", 0) == 0


# --------------------------------------------------------------------------- #
# sweep: bound queue depth at origin (the "58 files, oldest from April" bug).  #
# --------------------------------------------------------------------------- #


def test_sweep_removes_stale_keeps_fresh_alive(tmp_path: Path) -> None:
    qdir = tmp_path / "q"
    qdir.mkdir()
    fakes = _mkfakes(tmp_path)
    now = int(time.time())
    _qfile(qdir, "%38").write_text("%38 /rename stale\n")  # legacy
    _qfile(qdir, "%99").write_text(f"{now} {SID} %99 /rename keep\n")  # fresh+alive
    _qfile(qdir, "%50").write_text(f"{now - 9999} {SID} %50 /rename expired\n")  # expired
    _qfile(qdir, "%77").write_text(f"{now} {SID} %77 /rename dead\n")  # dead pane
    proc = _run(
        ["sweep"],
        qdir=qdir,
        fakes=fakes,
        extra_env={"FAKE_ALIVE": "%99 %50 %38"},  # %77 is dead
    )
    assert proc.returncode == 0, proc.stderr
    remaining = sorted(p.name for p in qdir.iterdir())
    assert remaining == ["99"], f"sweep left {remaining}"
