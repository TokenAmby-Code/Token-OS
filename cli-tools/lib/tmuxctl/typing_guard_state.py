"""Canonical tmux typing-guard state transitions.

The guard has one state machine per pane:

    off -> on -> pending -> off          (human keystroke lifecycle)
    off -> agent -> off                  (daemon send holds the pane)

State is represented only by tmux pane options:

* @TYPING_LOCK_UNTIL=<epoch> for ON
* @TYPING_PENDING_UNTIL=<epoch> for PENDING
* @TYPING_AGENT_UNTIL=<epoch> for AGENT (a daemon send holding the pane)
* @GUARD as the visual projection (yellow keyboard for ON, red keyboard for
  PENDING, green keyboard for AGENT)

A live human on/pending hold always wins: an ``agent`` hold may only be acquired
when the pane is OFF, so a daemon send never silently stomps the Emperor's
in-progress keystrokes. No prompt scraping, focus/click heuristics, or stamp
files live here.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass

LOCK_OPTION = "@TYPING_LOCK_UNTIL"
PENDING_OPTION = "@TYPING_PENDING_UNTIL"
AGENT_OPTION = "@TYPING_AGENT_UNTIL"
GUARD_OPTION = "@GUARD"
ON_MARKER = "#[fg=colour214,bold]⌨#[default]"
PENDING_MARKER = "#[fg=red,bold]⌨#[default]"
AGENT_MARKER = "#[fg=green,bold]⌨#[default]"


@dataclass(frozen=True)
class Tmux:
    binary: str

    def run(self, *args: str, timeout: float = 0.5) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                [self.binary, *args],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=timeout,
            )
        except Exception:
            return None


def tmux_binary() -> str:
    try:
        from .tmux_adapter import tmux_binary as _tmux_binary

        return _tmux_binary()
    except Exception:
        return "tmux"


def now_epoch(value: str | None = None) -> int:
    if value not in (None, ""):
        try:
            return int(float(value))
        except ValueError:
            pass
    return int(time.time())


def option_epoch(tmux: Tmux, pane: str, option: str) -> int | None:
    if not pane:
        return None
    proc = tmux.run("show-options", "-pqv", "-t", pane, option, timeout=0.3)
    if proc is None or proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def live_state(tmux: Tmux, pane: str, *, now: int | None = None) -> str:
    current = now_epoch() if now is None else now
    lock_until = option_epoch(tmux, pane, LOCK_OPTION)
    if lock_until is not None and current < lock_until:
        return "on"
    pending_until = option_epoch(tmux, pane, PENDING_OPTION)
    if pending_until is not None and current < pending_until:
        return "pending"
    agent_until = option_epoch(tmux, pane, AGENT_OPTION)
    if agent_until is not None and current < agent_until:
        return "agent"
    return "off"


def marker_for(state: str) -> str:
    if state == "on":
        return ON_MARKER
    if state == "pending":
        return PENDING_MARKER
    if state == "agent":
        return AGENT_MARKER
    return ""


def set_option(tmux: Tmux, pane: str, option: str, value: str) -> None:
    tmux.run("set-option", "-p", "-t", pane, option, value)


def unset_option(tmux: Tmux, pane: str, option: str) -> None:
    tmux.run("set-option", "-pu", "-t", pane, option)


def publish(tmux: Tmux, pane: str, state: str) -> None:
    set_option(tmux, pane, GUARD_OPTION, marker_for(state))


def schedule_expiry(tmux: Tmux, pane: str, seconds: int) -> None:
    """No background sleeper: expiry is lazy/event-driven via live_state/expire_pane."""
    return None


def mark_client_activity(
    *, client: str | None, term: str | None, pid: str | None, session: str | None
) -> None:
    if not client:
        return
    args = [
        "tmux-client-lease",
        "activity",
        "--client",
        client,
        "--reason",
        "key",
    ]
    if term:
        args.extend(["--term", term])
    if pid:
        args.extend(["--pid", pid])
    if session:
        args.extend(["--session", session])
    try:
        subprocess.run(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=1.0
        )
    except Exception:
        pass


def arm(
    tmux: Tmux,
    pane: str,
    *,
    seconds: int,
    now: int,
    client: str | None = None,
    term: str | None = None,
    pid: str | None = None,
    session: str | None = None,
) -> None:
    """Move OFF -> ON. Existing ON/PENDING state is preserved, not refreshed."""
    state = live_state(tmux, pane, now=now)
    if state != "off":
        publish(tmux, pane, state)
        return
    mark_client_activity(client=client, term=term, pid=pid, session=session)
    unset_option(tmux, pane, PENDING_OPTION)
    set_option(tmux, pane, LOCK_OPTION, str(now + int(seconds)))
    publish(tmux, pane, "on")
    schedule_expiry(tmux, pane, int(seconds))


def pending(tmux: Tmux, pane: str, *, seconds: int, now: int) -> None:
    """Move ON -> PENDING (also safe for submit keys when already OFF)."""
    set_option(tmux, pane, PENDING_OPTION, str(now + int(seconds)))
    unset_option(tmux, pane, LOCK_OPTION)
    publish(tmux, pane, "pending")
    schedule_expiry(tmux, pane, int(seconds))


def hold(tmux: Tmux, pane: str, *, seconds: int, now: int) -> bool:
    """Move OFF -> AGENT. Returns True on acquire, False (no-op) if non-off.

    A daemon send acquires this hold to render the pane green and make the gate
    treat it as active (state-blind) so concurrent sends to the same pane delay.
    A live human ON/PENDING hold takes precedence and is NOT overwritten — the
    OFF-guard mirrors ``arm()``; a denied caller falls through to the existing
    send-gate delay path so it queues behind the human instead of stomping it.
    """
    state = live_state(tmux, pane, now=now)
    if state != "off":
        return False
    set_option(tmux, pane, AGENT_OPTION, str(now + int(seconds)))
    publish(tmux, pane, "agent")
    schedule_expiry(tmux, pane, int(seconds))
    return True


def release(tmux: Tmux, pane: str, *, now: int | None = None) -> None:
    """Clear the AGENT hold and re-publish via expire_pane semantics.

    Only ``@TYPING_AGENT_UNTIL`` is cleared; if a human lock arrived during the
    hold, ``expire_pane`` re-projects it rather than blanking the marker.
    """
    unset_option(tmux, pane, AGENT_OPTION)
    expire_pane(tmux, pane, now=now)


def expire_pane(tmux: Tmux, pane: str, *, now: int | None = None) -> None:
    current = now_epoch() if now is None else now
    lock_until = option_epoch(tmux, pane, LOCK_OPTION)
    pending_until = option_epoch(tmux, pane, PENDING_OPTION)
    agent_until = option_epoch(tmux, pane, AGENT_OPTION)

    if lock_until is not None and current >= lock_until:
        unset_option(tmux, pane, LOCK_OPTION)
        lock_until = None
    if pending_until is not None and current >= pending_until:
        unset_option(tmux, pane, PENDING_OPTION)
        pending_until = None
    if agent_until is not None and current >= agent_until:
        unset_option(tmux, pane, AGENT_OPTION)
        agent_until = None

    if lock_until is not None and current < lock_until:
        publish(tmux, pane, "on")
    elif pending_until is not None and current < pending_until:
        publish(tmux, pane, "pending")
    elif agent_until is not None and current < agent_until:
        publish(tmux, pane, "agent")
    else:
        publish(tmux, pane, "off")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Canonical tmux typing-guard state helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--pane", default="", help="target pane id (default: $TMUX_PANE)")
        p.add_argument("--now", default=None, help="epoch to use as now (default: wall clock)")

    p_arm = sub.add_parser("arm", help="move OFF -> ON")
    add_common(p_arm)
    p_arm.add_argument("--seconds", type=int, default=300)
    p_arm.add_argument("--client", default=None)
    p_arm.add_argument("--term", default=None)
    p_arm.add_argument("--pid", default=None)
    p_arm.add_argument("--session", default=None)

    p_pending = sub.add_parser("pending", help="move ON -> PENDING")
    add_common(p_pending)
    p_pending.add_argument("--seconds", type=int, required=True)

    p_hold = sub.add_parser("hold", help="move OFF -> AGENT (daemon send hold)")
    add_common(p_hold)
    p_hold.add_argument("--seconds", type=int, default=8)

    p_release = sub.add_parser("release", help="clear an AGENT hold")
    add_common(p_release)

    p_expire = sub.add_parser(
        "expire-pane", help="clear stale ON/PENDING/AGENT projection for one pane"
    )
    add_common(p_expire)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    pane = args.pane or __import__("os").environ.get("TMUX_PANE", "")
    if not pane:
        return 0
    tmux = Tmux(tmux_binary())
    current = now_epoch(args.now)
    try:
        if args.cmd == "arm":
            arm(
                tmux,
                pane,
                seconds=args.seconds,
                now=current,
                client=args.client,
                term=args.term,
                pid=args.pid,
                session=args.session,
            )
        elif args.cmd == "pending":
            pending(tmux, pane, seconds=args.seconds, now=current)
        elif args.cmd == "hold":
            hold(tmux, pane, seconds=args.seconds, now=current)
        elif args.cmd == "release":
            release(tmux, pane, now=current)
        elif args.cmd == "expire-pane":
            expire_pane(tmux, pane, now=current)
    except Exception:
        return 0  # fail-open: state projection must never break typing
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
