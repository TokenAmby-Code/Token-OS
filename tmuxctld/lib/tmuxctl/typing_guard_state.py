"""Daemon-owned tmux typing-guard state transitions.

Canonical state is one pane option, ``@TYPING_GUARD_JSON``.  tmux-facing
projection options (``@TYPING_GUARD_UNTIL``, ``@TYPING_GUARD_KIND``, and
``@TYPING_GUARD_MARKER``) are derived from that JSON record for zero-fork
border/key-binding fast paths.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Any

GUARD_JSON_OPTION = "@TYPING_GUARD_JSON"
GUARD_UNTIL_OPTION = "@TYPING_GUARD_UNTIL"
GUARD_KIND_OPTION = "@TYPING_GUARD_KIND"
GUARD_MARKER_OPTION = "@TYPING_GUARD_MARKER"


HUMAN = "human"
PENDING = "pending"
AGENT = "agent"
OFF = "off"
SOURCE = "tmuxctld"

ON_MARKER = "#[fg=colour214,bold]⌨#[default]"
PENDING_MARKER = "#[fg=red,bold]⌨#[default]"
AGENT_MARKER = "#[fg=green,bold]⌨#[default]"

ANY_BINDING = """\
bind -n Any {
  if -F '#{==:#{mouse_x},}' {
    run-shell -b "tmuxctld-ping POST /typing-guard-state cmd=arm pane=#{q:pane_id} seconds=300 now=#{client_activity} client=#{q:client_tty} term=#{q:client_termname} pid=#{q:client_pid} session=#{q:session_name} >/dev/null || env IMPERIUM_TMUX_RAW=1 tmux display-message tmuxctld-ping-/typing-guard-state-failed"
    send-keys
  } {
  }
}
"""


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


def marker_for(kind: str) -> str:
    if kind in {HUMAN, "on"}:
        return ON_MARKER
    if kind == PENDING:
        return PENDING_MARKER
    if kind == AGENT:
        return AGENT_MARKER
    return ""


def _read_option(tmux: Tmux, pane: str, option: str) -> str:
    if not pane:
        return ""
    proc = tmux.run("show-options", "-pqv", "-t", pane, option, timeout=0.3)
    if proc is None or proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _run_ok(tmux: Tmux, *args: str, timeout: float = 0.5) -> bool:
    proc = tmux.run(*args, timeout=timeout)
    return proc is not None and proc.returncode == 0


def enable_any_binding(tmux: Tmux) -> dict[str, Any]:
    """Enable the root-table ordinary-key hook.

    tmux key bindings are global, not per-pane, so this deliberately changes only
    hook topology.  It does not inspect, write, extend, or clear guard state.
    """

    path = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write(ANY_BINDING)
            path = handle.name
        ok = _run_ok(tmux, "source-file", path, timeout=1.0)
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
    return {"topology": "enabled", "any_bound": bool(ok)}


def disable_any_binding(tmux: Tmux) -> dict[str, Any]:
    """Disable the root-table ordinary-key hook without touching guard state."""

    ok = _run_ok(tmux, "unbind-key", "-q", "-n", "Any")
    return {"topology": "disabled", "any_bound": False, "ok": bool(ok)}


def focused_pane(tmux: Tmux) -> str:
    proc = tmux.run("display-message", "-p", "#{pane_id}", timeout=0.3)
    if proc is None or proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def projection_status(tmux: Tmux, pane: str, *, now: int | None = None) -> dict[str, Any]:
    """Read only tmux-facing projection options for focus/topology decisions."""

    current = now_epoch() if now is None else now
    kind = (_read_option(tmux, pane, GUARD_KIND_OPTION) or OFF).strip().lower()
    if kind == "on":
        kind = HUMAN
    if kind not in {HUMAN, PENDING, AGENT, OFF}:
        kind = OFF
    until_raw = _read_option(tmux, pane, GUARD_UNTIL_OPTION)
    try:
        until = int(float(until_raw)) if until_raw not in (None, "") else None
    except (TypeError, ValueError):
        until = None
    active = kind in {HUMAN, PENDING, AGENT} and until is not None and current < int(until)
    if not active:
        kind = OFF
        until = None
    return {"kind": kind, "until": until, "active": active, "pane": pane}


def rehydrate_any_binding(
    tmux: Tmux, pane: str = "", *, now: int | None = None
) -> dict[str, Any]:
    """Rebuild ordinary-key hook topology from existing focused-pane projections.

    Focus changes must not mutate typing-guard state.  This function only reads
    ``@TYPING_GUARD_KIND``/``@TYPING_GUARD_UNTIL`` and toggles the global root
    ``Any`` binding: active HUMAN guard => disabled; anything else => enabled.
    """

    target = pane or focused_pane(tmux)
    projected = projection_status(tmux, target, now=now) if target else {
        "kind": OFF,
        "until": None,
        "active": False,
        "pane": "",
    }
    topology = (
        disable_any_binding(tmux)
        if projected["kind"] == HUMAN and projected["active"]
        else enable_any_binding(tmux)
    )
    return {"pane": target, "projection": projected, **topology}



def set_option(tmux: Tmux, pane: str, option: str, value: str) -> None:
    tmux.run("set-option", "-p", "-t", pane, option, value)


def unset_option(tmux: Tmux, pane: str, option: str) -> None:
    tmux.run("set-option", "-pu", "-t", pane, option)


def _normalize_record(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"kind": OFF, "until": None, "owner": None, "source": SOURCE}
    kind = str(raw.get("kind") or OFF).strip().lower()
    if kind == "on":
        kind = HUMAN
    if kind not in {HUMAN, PENDING, AGENT, OFF}:
        kind = OFF
    until_raw = raw.get("until")
    try:
        until = int(float(until_raw)) if until_raw not in (None, "") else None
    except (TypeError, ValueError):
        until = None
    owner = raw.get("owner")
    if owner is not None:
        owner = str(owner).strip() or None
    source = str(raw.get("source") or SOURCE)
    return {"kind": kind, "until": until, "owner": owner, "source": source}


def read_record(tmux: Tmux, pane: str) -> dict[str, Any]:
    raw = _read_option(tmux, pane, GUARD_JSON_OPTION)
    if not raw:
        return {"kind": OFF, "until": None, "owner": None, "source": SOURCE}
    try:
        return _normalize_record(json.loads(raw))
    except json.JSONDecodeError:
        return {"kind": OFF, "until": None, "owner": None, "source": SOURCE}


def _active_record(tmux: Tmux, pane: str, *, now: int | None = None) -> dict[str, Any]:
    current = now_epoch() if now is None else now
    record = read_record(tmux, pane)
    until = record.get("until")
    if record.get("kind") in {HUMAN, PENDING, AGENT} and until is not None and current < int(until):
        return record
    return {"kind": OFF, "until": None, "owner": None, "source": SOURCE}


def status(tmux: Tmux, pane: str, *, now: int | None = None) -> dict[str, Any]:
    record = _active_record(tmux, pane, now=now)
    kind = str(record.get("kind") or OFF)
    until = record.get("until")
    marker = marker_for(kind)
    return {
        "kind": kind,
        "until": until,
        "owner": record.get("owner"),
        "active": kind != OFF and until is not None,
        "marker": marker,
    }



def write_record(
    tmux: Tmux,
    pane: str,
    *,
    kind: str,
    until: int | None,
    owner: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    normalized = _normalize_record({"kind": kind, "until": until, "owner": owner, "source": SOURCE})
    if normalized["kind"] == OFF:
        normalized["until"] = None
        normalized["owner"] = None
    marker = marker_for(str(normalized["kind"]))
    set_option(
        tmux,
        pane,
        GUARD_JSON_OPTION,
        json.dumps(normalized, separators=(",", ":"), sort_keys=True),
    )
    set_option(tmux, pane, GUARD_UNTIL_OPTION, str(normalized["until"] or 0))
    set_option(tmux, pane, GUARD_KIND_OPTION, str(normalized["kind"]))
    set_option(tmux, pane, GUARD_MARKER_OPTION, marker)
    return status(tmux, pane, now=now)


def mark_client_activity(
    *, client: str | None, term: str | None, pid: str | None, session: str | None
) -> None:
    if not client:
        return
    args = ["activity", "--client", client, "--reason", "key"]
    if term:
        args.extend(["--term", term])
    if pid:
        args.extend(["--pid", pid])
    if session:
        args.extend(["--session", session])
    try:
        from tmux_client_lease import main as client_lease_main

        client_lease_main(args)
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
) -> dict[str, Any]:
    current = status(tmux, pane, now=now)
    if current["kind"] == HUMAN and current["active"]:
        disable_any_binding(tmux)
        return current
    mark_client_activity(client=client, term=term, pid=pid, session=session)
    result = write_record(tmux, pane, kind=HUMAN, until=now + int(seconds), owner=None, now=now)
    disable_any_binding(tmux)
    return result


def pending(tmux: Tmux, pane: str, *, seconds: int, now: int) -> dict[str, Any]:
    result = write_record(tmux, pane, kind=PENDING, until=now + int(seconds), owner=None, now=now)
    enable_any_binding(tmux)
    return result


def hold(
    tmux: Tmux,
    pane: str,
    *,
    seconds: int,
    now: int,
    owner: str | None = None,
) -> str | None:
    current = status(tmux, pane, now=now)
    if current["active"]:
        return None
    token = (owner or str(uuid.uuid4())).strip()
    write_record(tmux, pane, kind=AGENT, until=now + int(seconds), owner=token, now=now)
    return token


def release(tmux: Tmux, pane: str, *, now: int | None = None, owner: str | None = None) -> bool:
    current = status(tmux, pane, now=now)
    if current["kind"] != AGENT or not current["active"]:
        expire_pane(tmux, pane, now=now)
        return False
    current_owner = current.get("owner")
    if current_owner and (not owner or str(owner).strip() != current_owner):
        return False
    write_record(tmux, pane, kind=OFF, until=None, owner=None, now=now)
    return True


def expire_pane(tmux: Tmux, pane: str, *, now: int | None = None) -> dict[str, Any]:
    current = status(tmux, pane, now=now)
    if current["active"]:
        # Re-project from canonical JSON.
        set_option(tmux, pane, GUARD_UNTIL_OPTION, str(current["until"] or 0))
        set_option(tmux, pane, GUARD_KIND_OPTION, str(current["kind"]))
        set_option(tmux, pane, GUARD_MARKER_OPTION, str(current["marker"] or ""))
        return current
    write_record(tmux, pane, kind=OFF, until=None, owner=None, now=now)
    return status(tmux, pane, now=now)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Daemon-owned tmux typing-guard state helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--pane", default="", help="target pane id (default: $TMUX_PANE)")
        p.add_argument("--now", default=None, help="epoch to use as now (default: wall clock)")

    p_arm = sub.add_parser("arm", help="set HUMAN guard")
    add_common(p_arm)
    p_arm.add_argument("--seconds", type=int, default=300)
    p_arm.add_argument("--client", default=None)
    p_arm.add_argument("--term", default=None)
    p_arm.add_argument("--pid", default=None)
    p_arm.add_argument("--session", default=None)

    p_pending = sub.add_parser("pending", help="set PENDING guard")
    add_common(p_pending)
    p_pending.add_argument("--seconds", type=int, required=True)

    p_hold = sub.add_parser("hold", help="set AGENT guard and create/record owner token")
    add_common(p_hold)
    p_hold.add_argument("--seconds", type=int, default=8)
    p_hold.add_argument("--owner", default=None)

    p_release = sub.add_parser("release", help="clear an AGENT guard if owner matches")
    add_common(p_release)
    p_release.add_argument("--owner", default=None)

    p_expire = sub.add_parser("expire-pane", help="clear stale guard projection for one pane")
    add_common(p_expire)

    p_status = sub.add_parser("status", help="print structured guard status")
    add_common(p_status)

    p_rehydrate = sub.add_parser("rehydrate", help="rehydrate root Any hook from projections")
    add_common(p_rehydrate)

    sub.add_parser("enable-any", help="enable root Any ordinary-key hook")
    sub.add_parser("disable-any", help="disable root Any ordinary-key hook")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    pane = args.pane or os.environ.get("TMUX_PANE", "")
    pane_optional_commands = {"enable-any", "disable-any", "rehydrate"}
    if not pane and args.cmd not in pane_optional_commands:
        if args.cmd == "status":
            sys.stdout.write(json.dumps(status(Tmux(tmux_binary()), pane)))
        return 0
    tmux = Tmux(tmux_binary())
    current = now_epoch(args.now)
    try:
        result: Any = None
        if args.cmd == "arm":
            result = arm(
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
            result = pending(tmux, pane, seconds=args.seconds, now=current)
        elif args.cmd == "hold":
            owner = hold(tmux, pane, seconds=args.seconds, now=current, owner=args.owner)
            result = status(tmux, pane, now=current)
            result["acquired"] = bool(owner)
            if owner:
                result["owner"] = owner
        elif args.cmd == "release":
            released = release(tmux, pane, now=current, owner=args.owner)
            result = status(tmux, pane, now=current)
            result["released"] = released
        elif args.cmd == "expire-pane":
            result = expire_pane(tmux, pane, now=current)
        elif args.cmd == "status":
            result = expire_pane(tmux, pane, now=current)
        elif args.cmd == "rehydrate":
            result = rehydrate_any_binding(tmux, pane, now=current)
        elif args.cmd == "enable-any":
            result = enable_any_binding(tmux)
        elif args.cmd == "disable-any":
            result = disable_any_binding(tmux)
        if result is not None:
            sys.stdout.write(json.dumps(result, separators=(",", ":"), sort_keys=True))
    except Exception:
        return 0  # fail-open: state projection must never break typing
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
