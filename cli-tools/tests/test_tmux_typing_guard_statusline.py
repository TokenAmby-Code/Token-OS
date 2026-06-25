"""Statusline diagnostic tests — the segment must reflect the CANONICAL gate.

`bin/tmux-typing-guard-status` is the per-pane diagnostic. Its whole job is to
answer, honestly, "would the universal send gate hold an automated write to this
pane right now?" — so it must consult ``send_gate.typing_guard_active(target=…)``
(the predicate that actually gates the Python clobber path: state-hooks,
enforcement, dispatch). That predicate is now the keystroke-anchored per-pane
lock: it reads ``@TYPING_LOCK_UNTIL`` (an absolute expiry epoch the tmux any-key
binding stamps on first keystroke). It must NOT answer from the legacy 300s shell
stamp files, which over-report. The divergence test below pins exactly that: a
live stamp present, but no keystroke lock ⇒ the segment is dark.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATUS = REPO / "cli-tools" / "bin" / "tmux-typing-guard-status"

STYLED = "#[fg=colour214,bold]⌨ GUARD#[default] "


def _fake_tmux(tmp_path: Path) -> Path:
    """A tmux stand-in driven by FAKE_* env, recording set-option writes.

    Emulates exactly the calls the canonical predicate makes: the per-pane
    keystroke-lock read (``show-options -pqv -t <pane> @TYPING_LOCK_UNTIL``),
    the active-pane / live-pane queries, and ``set-option`` (recorded as a clean
    ``<option>\\t<pane>\\t<value>`` line in FAKE_SETOPT, value empty when cleared).
    A pane's lock epoch is read from ``FAKE_LOCK_DIR/<panekey>`` (absent = unset).
    """
    fake = tmp_path / "tmux"
    fake.write_text(
        textwrap.dedent(
            r"""
            #!/usr/bin/env bash
            set -uo pipefail
            echo "$*" >> "${FAKE_TMUX_CALLS}"
            key() { printf '%s' "$1" | sed 's/[^A-Za-z0-9_.:%-]/_/g'; }
            verb="${1:-}"; shift || true
            target=""; opt=""; val=""; prev=""; seen_opt=0
            for a in "$@"; do
              if [[ "$prev" == "-t" ]]; then target="$a"; fi
              if [[ "$a" == @* ]]; then opt="$a"; seen_opt=1; prev="$a"; continue; fi
              if [[ "$seen_opt" == "1" ]]; then val="$a"; fi
              prev="$a"
            done
            case "$verb" in
              display-message)
                if [[ "$*" == *"#{pane_id}"* && "$*" != *"-t"* ]]; then
                  echo "${FAKE_ACTIVE_PANE:-%1}"
                else
                  echo ""
                fi
                ;;
              list-panes)
                for p in ${FAKE_PANES:-}; do echo "$p"; done
                ;;
              show-options|show)
                if [[ "$*" == *"@TYPING_LOCK_UNTIL"* ]]; then
                  f="${FAKE_LOCK_DIR}/$(key "$target")"
                  if [[ -f "$f" ]]; then cat "$f"; fi
                  exit 0
                fi
                if [[ "$*" == *"@TYPING_PENDING_UNTIL"* ]]; then
                  f="${FAKE_PENDING_DIR}/$(key "$target")"
                  if [[ -f "$f" ]]; then cat "$f"; fi
                  exit 0
                fi
                exit 1
                ;;
              set-option|set)
                printf '%s\t%s\t%s\n' "$opt" "$target" "$val" >> "${FAKE_SETOPT}"
                exit 0
                ;;
              *)
                ;;
            esac
            """
        ).strip()
        + "\n"
    )
    fake.chmod(0o755)
    return fake


def _key(pane: str) -> str:
    return "".join(c if (c.isalnum() or c in "_.:%-") else "_" for c in pane)


def _env(
    tmp_path: Path,
    *,
    locks: dict[str, int],
    pending: dict[str, int] | None = None,
    active: str = "%1",
) -> dict[str, str]:
    """Build the env, stamping each pane's keystroke-lock epoch into FAKE_LOCK_DIR."""
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    for pane, epoch in locks.items():
        (lock_dir / _key(pane)).write_text(f"{int(epoch)}\n")
    pending_dir = tmp_path / "pending"
    pending_dir.mkdir()
    for pane, epoch in (pending or {}).items():
        (pending_dir / _key(pane)).write_text(f"{int(epoch)}\n")
    calls = tmp_path / "calls.log"
    setopt = tmp_path / "setopt.log"
    calls.write_text("")
    setopt.write_text("")
    fake = _fake_tmux(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}:{env.get('PATH', '')}",
            "IMPERIUM_TMUX_BIN": str(fake),
            "FAKE_TMUX_CALLS": str(calls),
            "FAKE_SETOPT": str(setopt),
            "FAKE_LOCK_DIR": str(lock_dir),
            "FAKE_PENDING_DIR": str(pending_dir),
            "FAKE_ACTIVE_PANE": active,
            "FAKE_PANES": "%1",
            # Isolate the legacy stamp dir so a stray host stamp can't leak in.
            "TMUX_GUARD_STATE_DIR": str(tmp_path / "guard-state"),
        }
    )
    return env


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(STATUS), *args],
        text=True,
        capture_output=True,
        env=env,
        timeout=15,  # widened for CPU contention under parallel runs
        check=False,
    )


def _setopts(env: dict[str, str]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for line in Path(env["FAKE_SETOPT"]).read_text().splitlines():
        parts = line.split("\t")
        while len(parts) < 3:
            parts.append("")
        rows.append((parts[0], parts[1], parts[2]))
    return rows


def _guard_value_for(env: dict[str, str], pane: str) -> str | None:
    """Last @GUARD value written for ``pane`` (None if never written)."""
    value: str | None = None
    for opt, target, val in _setopts(env):
        if opt == "@GUARD" and target == pane:
            value = val
    return value


def test_segment_active_when_canonical_predicate_holds(tmp_path: Path) -> None:
    # Active pane carries a live keystroke lock → canonical predicate active.
    env = _env(tmp_path, locks={"%1": int(time.time()) + 200})
    assert _run(env).stdout == STYLED
    assert _run(env, "--plain").stdout == "TYPE"


def test_segment_dark_when_predicate_clear(tmp_path: Path) -> None:
    # No lock (never typed into) → predicate inactive.
    env = _env(tmp_path, locks={})
    assert _run(env).stdout == ""
    assert _run(env, "--plain").stdout == ""


def test_segment_dark_when_lock_expired(tmp_path: Path) -> None:
    # An expired lock (5-min window elapsed, or an Enter cleared it) reads clear.
    env = _env(tmp_path, locks={"%1": int(time.time()) - 5})
    assert _run(env).stdout == ""


def test_segment_follows_lock_not_legacy_stamp(tmp_path: Path) -> None:
    """Divergence pin: the segment tracks the CANONICAL keystroke lock, not the
    legacy shell stamp. A stale ``%1.stamp`` is present and the pane has NO live
    lock — the honest diagnostic must stay dark, proving it ignores the stamp."""
    env = _env(tmp_path, locks={})  # no keystroke lock
    state_dir = Path(env["TMUX_GUARD_STATE_DIR"])
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "%1.stamp").write_text("started_at=1\nstate=active\n")  # stale/irrelevant

    assert _run(env).stdout == "", "must follow the canonical lock, not the legacy stamp"


def test_segment_publishes_pane_scoped_guard_option(tmp_path: Path) -> None:
    """The active pane's border var is pushed so it renders with zero fork."""
    env = _env(tmp_path, locks={"%1": int(time.time()) + 200})
    _run(env)
    assert _guard_value_for(env, "%1") not in (None, ""), "locked pane must get a non-empty @GUARD"


def test_segment_clears_pane_guard_option_when_dark(tmp_path: Path) -> None:
    env = _env(tmp_path, locks={})
    _run(env)
    assert _guard_value_for(env, "%1") == "", "unlocked pane must have @GUARD cleared"


def test_scan_marks_guarded_panes_and_clears_clean_ones(tmp_path: Path) -> None:
    """--scan refreshes every pane's @GUARD in a single fork (not per render)."""
    now = int(time.time())
    env = _env(tmp_path, locks={"%1": now + 200, "%2": now - 5})
    env["FAKE_PANES"] = "%1 %2"
    _run(env, "--scan")

    assert _guard_value_for(env, "%1") not in (None, ""), "%1 locked"
    assert _guard_value_for(env, "%2") == "", "%2 clear (expired lock)"


def test_expire_pane_clears_stale_event_projection(tmp_path: Path) -> None:
    now = int(time.time())
    env = _env(tmp_path, locks={"%1": now - 10}, pending={"%1": now - 5})

    _run(env, "--expire-pane", "%1")

    assert _guard_value_for(env, "%1") == ""


def test_expire_pane_keeps_pending_projection(tmp_path: Path) -> None:
    now = int(time.time())
    env = _env(tmp_path, locks={}, pending={"%1": now + 5})

    _run(env, "--expire-pane", "%1")

    assert _guard_value_for(env, "%1") is None
