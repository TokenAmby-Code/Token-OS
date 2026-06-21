"""Statusline diagnostic tests — the segment must reflect the CANONICAL gate.

`bin/tmux-typing-guard-status` is the per-pane diagnostic. Its whole job is to
answer, honestly, "would the universal send gate hold an automated write to this
pane right now?" — so it must consult ``send_gate.typing_guard_active(target=…)``
(the predicate that actually gates the Python clobber path: state-hooks,
enforcement, dispatch). It must NOT answer from the legacy 300s shell stamp
files, which over-report (they light long after a keystroke and across agent
panes). The divergence test below pins exactly that: a live stamp present, but
the canonical predicate inactive ⇒ the segment is dark.
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

    Emulates exactly the calls the canonical predicate makes, and records each
    ``set-option`` as a clean ``<option>\\t<pane>\\t<value>`` line in FAKE_SETOPT
    (value empty when cleared), so tests never have to parse flattened ``$*``.
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
              if [[ "$seen_opt" == "1" && -z "$opt" ]]; then :; fi
              if [[ "$a" == @* ]]; then opt="$a"; seen_opt=1; prev="$a"; continue; fi
              if [[ "$seen_opt" == "1" ]]; then val="$a"; fi
              prev="$a"
            done
            case "$verb" in
              display-message)
                if [[ "$*" == *"#{pane_id}"* && "$*" != *"-t"* ]]; then
                  echo "${FAKE_ACTIVE_PANE:-%1}"
                elif [[ "$*" == *"#{client_activity}"* ]]; then
                  echo "${FAKE_CLIENT_ACTIVITY:-}"
                elif [[ "$*" == *"pane_active"* ]]; then
                  if [[ "$target" == "${FAKE_ACTIVE_PANE:-%1}" ]]; then echo "11"; else echo "00"; fi
                else
                  echo ""
                fi
                ;;
              list-clients)
                if [[ "$target" == "${FAKE_ACTIVE_PANE:-%1}" ]]; then echo "x"; fi
                ;;
              capture-pane)
                f="${FAKE_CAP_DIR}/$(key "$target")"
                if [[ -f "$f" ]]; then cat "$f"; fi
                ;;
              list-panes)
                for p in ${FAKE_PANES:-}; do echo "$p"; done
                ;;
              set-option|set)
                printf '%s\t%s\t%s\n' "$opt" "$target" "$val" >> "${FAKE_SETOPT}"
                exit 0
                ;;
              show-options|show)
                exit 1
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


def _env(tmp_path: Path, *, captures: dict[str, str], active: str = "%1") -> dict[str, str]:
    cap_dir = tmp_path / "caps"
    cap_dir.mkdir()
    for pane, text in captures.items():
        key = "".join(c if (c.isalnum() or c in "_.:%-") else "_" for c in pane)
        (cap_dir / key).write_text(text)
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
            "FAKE_CAP_DIR": str(cap_dir),
            "FAKE_ACTIVE_PANE": active,
            "FAKE_CLIENT_ACTIVITY": "",
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
        timeout=5,
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
    # Active pane has an unsent draft → canonical predicate active.
    env = _env(tmp_path, captures={"%1": "> draft\n"})
    assert _run(env).stdout == STYLED
    assert _run(env, "--plain").stdout == "TYPE"


def test_segment_dark_when_predicate_clear(tmp_path: Path) -> None:
    # Empty prompt, no recent keystroke → predicate inactive.
    env = _env(tmp_path, captures={"%1": "> \n"})
    assert _run(env).stdout == ""
    assert _run(env, "--plain").stdout == ""


def test_segment_lights_on_attendance_plus_keystroke_where_stamp_model_is_dark(
    tmp_path: Path,
) -> None:
    """Divergence pin: the segment must track the CANONICAL predicate.

    Case that distinguishes the two models: the active pane is attended and the
    human just hit a key (``client_activity`` fresh) but the prompt line shows
    no text yet (between keystrokes / a control key). The canonical predicate
    holds via its attendance+activity branch; the legacy stamp model — which
    only keys off captured prompt text — would clear and go dark. The honest
    diagnostic must light here. A stray legacy stamp is also present to prove it
    is irrelevant to the decision.
    """
    env = _env(tmp_path, captures={"%1": "> \n"})  # empty prompt line
    env["FAKE_CLIENT_ACTIVITY"] = str(int(time.time()))  # keystroke just now
    state_dir = Path(env["TMUX_GUARD_STATE_DIR"])
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "%1.stamp").write_text("started_at=1\nstate=expired\n")  # stale/irrelevant

    assert _run(env).stdout == STYLED, "must follow canonical attendance+activity, not the stamp"


def test_segment_publishes_pane_scoped_guard_option(tmp_path: Path) -> None:
    """The active pane's border var is pushed so it renders with zero fork."""
    env = _env(tmp_path, captures={"%1": "> draft\n"})
    _run(env)
    assert _guard_value_for(env, "%1") not in (None, ""), "active pane must get a non-empty @GUARD"


def test_segment_clears_pane_guard_option_when_dark(tmp_path: Path) -> None:
    env = _env(tmp_path, captures={"%1": "> \n"})
    _run(env)
    assert _guard_value_for(env, "%1") == "", "clean pane must have @GUARD cleared"


def test_scan_marks_guarded_panes_and_clears_clean_ones(tmp_path: Path) -> None:
    """--scan refreshes every pane's @GUARD in a single fork (not per render)."""
    env = _env(tmp_path, captures={"%1": "> draft\n", "%2": "> \n"})
    env["FAKE_PANES"] = "%1 %2"
    _run(env, "--scan")

    assert _guard_value_for(env, "%1") not in (None, ""), "%1 guarded"
    assert _guard_value_for(env, "%2") == "", "%2 clear"
