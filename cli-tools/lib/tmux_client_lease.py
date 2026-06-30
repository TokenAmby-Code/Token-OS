"""Soft lease policy for attached tmux clients.

Keeps mobile/desktop clients from staying attached at the same time unless the
operator explicitly protects one role. Unknown clients are never detached.
"""

from __future__ import annotations

# 410 GONE tombstone: tmux CLI exterminatus 2026-06-30.
import sys as _tmux_410_sys

_tmux_410_sys.stderr.write(
    "410 GONE: cli-tools/lib/tmux_client_lease.py (tmux_client_lease.py) is tombstoned by the 2026-06-30 tmux CLI exterminatus.\\nThis cold tmux feature surface must not be used as an active runtime/control path.\\nDaemon-native replacement: tmuxctld client lease/event route TBD.\\nOriginal body is retained below this early-return as the emergency restore lever; lift only this tombstone block to prove an active blocker, build/cut over the daemon-native replacement, then restore the 410.\\n"
)
raise SystemExit(410)

# --- ORIGINAL BODY BELOW: emergency restore lever, intentionally dead under the 410. ---

import argparse
import os
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass

ROLE_DESKTOP = "desktop"
ROLE_MOBILE = "mobile"
ROLE_UNKNOWN = "unknown"
ROLES = {ROLE_DESKTOP, ROLE_MOBILE, ROLE_UNKNOWN}

PROTECT_OPTION = "@IMPERIUM_TMUX_LEASE_PROTECT_{role}_UNTIL"
PROTECT_REASON_OPTION = "@IMPERIUM_TMUX_LEASE_PROTECT_{role}_REASON"
CLIENT_ROLE_OPTION_PREFIX = "@IMPERIUM_TMUX_LEASE_ROLE_"
LAST_ROLE_OPTION = "@IMPERIUM_TMUX_LEASE_LAST_ROLE"
LAST_CLIENT_OPTION = "@IMPERIUM_TMUX_LEASE_LAST_CLIENT"
LAST_REASON_OPTION = "@IMPERIUM_TMUX_LEASE_LAST_REASON"
LAST_AT_OPTION = "@IMPERIUM_TMUX_LEASE_LAST_AT"


@dataclass(frozen=True)
class Client:
    tty: str
    termname: str = ""
    pid: str = ""
    session: str = ""
    activity: str = ""
    role_marker: str = ""

    @property
    def role_key(self) -> str:
        return role_option_key(self.tty)


@dataclass(frozen=True)
class LeaseDecision:
    detach_ttys: tuple[str, ...]
    role: str
    reason: str


def role_option_key(client_tty: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", (client_tty or "unknown").strip("/"))
    return f"{CLIENT_ROLE_OPTION_PREFIX}{safe}"


def classify_client(
    client: Client,
    *,
    explicit_role: str | None = None,
    process_chain: Sequence[str] | None = None,
) -> str:
    """Classify one tmux client as desktop/mobile/unknown.

    Explicit tmux markers win. Ghostty is the default desktop. The phone attach
    path uses a grouped session named ``phone`` as the durable mobile marker.
    Unknown/rescue clients stay manual.
    """

    for candidate in (explicit_role, client.role_marker):
        role = (candidate or "").strip().lower()
        if role in (ROLE_DESKTOP, ROLE_MOBILE):
            return role

    if (client.session or "").strip().lower() == "phone":
        return ROLE_MOBILE

    term = (client.termname or "").lower()
    if term == "xterm-ghostty" or "ghostty" in term:
        return ROLE_DESKTOP

    chain = " ".join(process_chain or ()).lower()
    if "ghostty" in chain:
        return ROLE_DESKTOP

    return ROLE_UNKNOWN


def is_protected(role: str, now: float, protected_until: dict[str, float]) -> bool:
    return protected_until.get(role, 0.0) > now


def lease_decision(
    clients: Sequence[Client],
    active_role: str,
    *,
    now: float | None = None,
    protected_until: dict[str, float] | None = None,
) -> LeaseDecision:
    """Return opposite-role clients that should be detached.

    The policy is intentionally conservative:
    - never detach the last remaining client;
    - never detach unknown clients;
    - never detach protected roles;
    - only desktop activity detaches mobile and mobile activity detaches desktop.
    """

    now = time.time() if now is None else now
    protected_until = protected_until or {}
    if active_role not in (ROLE_DESKTOP, ROLE_MOBILE) or len(clients) <= 1:
        return LeaseDecision((), active_role, "noop")

    opposite = ROLE_MOBILE if active_role == ROLE_DESKTOP else ROLE_DESKTOP
    if is_protected(opposite, now, protected_until):
        return LeaseDecision((), active_role, f"protected:{opposite}")

    detach: list[str] = []
    for client in clients:
        role = classify_client(client)
        if role == opposite:
            detach.append(client.tty)

    # Never detach all clients even if classification data is somehow wrong.
    if len(detach) >= len(clients):
        return LeaseDecision((), active_role, "would_detach_all")

    return LeaseDecision(tuple(detach), active_role, f"{active_role}_activity_detaches_{opposite}")


class Tmux:
    def __init__(self, tmux_bin: str | None = None, dry_run: bool = False):
        self.tmux_bin = tmux_bin or os.environ.get("IMPERIUM_TMUX_BIN") or "tmux"
        self.dry_run = dry_run

    def run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        if self.dry_run and args and args[0] in {"detach-client", "set-option"}:
            print("DRY-RUN tmux", " ".join(args), file=sys.stderr)
            return subprocess.CompletedProcess([self.tmux_bin, *args], 0, "", "")
        return subprocess.run(
            [self.tmux_bin, *args],
            check=check,
            capture_output=True,
            text=True,
        )

    def get_option(self, option: str) -> str:
        proc = self.run("show-options", "-gqv", option)
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def set_option(self, option: str, value: str) -> None:
        self.run("set-option", "-gq", option, value)

    def list_clients(self, session: str | None = None) -> list[Client]:
        fmt = "\t".join(
            [
                "#{client_tty}",
                "#{client_termname}",
                "#{client_pid}",
                "#{session_name}",
                "#{client_activity}",
            ]
        )
        args = ["list-clients", "-F", fmt]
        if session:
            args[1:1] = ["-t", session]
        proc = self.run(*args)
        if proc.returncode != 0:
            return []
        clients: list[Client] = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            parts += [""] * (5 - len(parts))
            tty, term, pid, sess, activity = parts[:5]
            marker = self.get_option(role_option_key(tty)) if tty else ""
            clients.append(Client(tty, term, pid, sess, activity, marker))
        return clients

    def protected_until(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for role in (ROLE_DESKTOP, ROLE_MOBILE):
            raw = self.get_option(PROTECT_OPTION.format(role=role))
            try:
                out[role] = float(raw)
            except (TypeError, ValueError):
                out[role] = 0.0
        return out

    def detach(self, tty: str) -> None:
        if tty:
            self.run("detach-client", "-t", tty)


def process_chain(pid: str, *, limit: int = 8) -> tuple[str, ...]:
    chain: list[str] = []
    current = str(pid or "").strip()
    for _ in range(limit):
        if not current or not current.isdigit() or current == "1":
            break
        proc = subprocess.run(
            ["ps", "-o", "ppid=", "-o", "comm=", "-p", current],
            capture_output=True,
            text=True,
            check=False,
        )
        line = proc.stdout.strip()
        if not line:
            break
        fields = line.split(None, 1)
        if len(fields) == 1:
            break
        ppid, comm = fields
        chain.append(comm)
        current = ppid
    return tuple(chain)


def _client_from_args(args: argparse.Namespace, tmux: Tmux) -> Client:
    tty = args.client or ""
    if not tty:
        proc = tmux.run("display-message", "-p", "#{client_tty}")
        tty = proc.stdout.strip() if proc.returncode == 0 else ""
    return Client(
        tty=tty,
        termname=args.term or "",
        pid=args.pid or "",
        session=args.session or "",
        activity="",
        role_marker=tmux.get_option(role_option_key(tty)) if tty else "",
    )


def _enforce(tmux: Tmux, active_role: str, reason: str, session: str | None = None) -> int:
    del session  # lease scope is all clients, including grouped main/phone sessions.
    clients = tmux.list_clients()
    decision = lease_decision(
        clients,
        active_role,
        protected_until=tmux.protected_until(),
    )
    now = str(int(time.time()))
    tmux.set_option(LAST_ROLE_OPTION, active_role)
    tmux.set_option(LAST_REASON_OPTION, reason or decision.reason)
    tmux.set_option(LAST_AT_OPTION, now)
    for tty in decision.detach_ttys:
        tmux.detach(tty)
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    tmux = Tmux(dry_run=args.dry_run)
    client = _client_from_args(args, tmux)
    role = classify_client(client, explicit_role=args.role, process_chain=process_chain(client.pid))
    if client.tty and role in (ROLE_DESKTOP, ROLE_MOBILE):
        tmux.set_option(client.role_key, role)
        tmux.set_option(LAST_CLIENT_OPTION, client.tty)
    if role in (ROLE_DESKTOP, ROLE_MOBILE):
        return _enforce(tmux, role, args.reason or "attach", session=client.session or args.session)
    return 0


def cmd_activity(args: argparse.Namespace) -> int:
    tmux = Tmux(dry_run=args.dry_run)
    client = _client_from_args(args, tmux)
    role = classify_client(client, explicit_role=args.role, process_chain=process_chain(client.pid))
    if client.tty and role in (ROLE_DESKTOP, ROLE_MOBILE):
        tmux.set_option(client.role_key, role)
        tmux.set_option(LAST_CLIENT_OPTION, client.tty)
        return _enforce(
            tmux, role, args.reason or "activity", session=client.session or args.session
        )
    return 0


def cmd_detach(args: argparse.Namespace) -> int:
    tmux = Tmux(dry_run=args.dry_run)
    tty = args.client or ""
    if tty:
        tmux.run("set-option", "-gqu", role_option_key(tty))
    return 0


def cmd_away(args: argparse.Namespace) -> int:
    tmux = Tmux(dry_run=args.dry_run)
    clients = tmux.list_clients()
    decision = lease_decision(
        clients,
        ROLE_MOBILE,
        protected_until=tmux.protected_until(),
    )
    tmux.set_option(LAST_ROLE_OPTION, ROLE_MOBILE)
    tmux.set_option(LAST_REASON_OPTION, args.reason or "away")
    tmux.set_option(LAST_AT_OPTION, str(int(time.time())))
    for tty in decision.detach_ttys:
        tmux.detach(tty)
    return 0


def cmd_protect(args: argparse.Namespace) -> int:
    role = args.role.lower()
    if role not in (ROLE_DESKTOP, ROLE_MOBILE):
        print("role must be desktop or mobile", file=sys.stderr)
        return 64
    minutes = max(0, int(args.minutes))
    until = int(time.time() + minutes * 60)
    tmux = Tmux(dry_run=args.dry_run)
    tmux.set_option(PROTECT_OPTION.format(role=role), str(until))
    tmux.set_option(PROTECT_REASON_OPTION.format(role=role), f"manual:{minutes}m")
    print(f"protected {role} until {until} ({minutes}m)")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    tmux = Tmux(dry_run=args.dry_run)
    now = time.time()
    print("tmux client lease")
    print(
        f"last: role={tmux.get_option(LAST_ROLE_OPTION) or '-'} client={tmux.get_option(LAST_CLIENT_OPTION) or '-'} reason={tmux.get_option(LAST_REASON_OPTION) or '-'} at={tmux.get_option(LAST_AT_OPTION) or '-'}"
    )
    for role in (ROLE_DESKTOP, ROLE_MOBILE):
        until_raw = tmux.get_option(PROTECT_OPTION.format(role=role))
        try:
            remaining = int(float(until_raw) - now)
        except (TypeError, ValueError):
            remaining = 0
        if remaining > 0:
            print(f"protect {role}: {remaining}s remaining")
    clients = tmux.list_clients(session=args.session)
    for client in clients:
        role = classify_client(client, process_chain=process_chain(client.pid))
        print(
            f"client {client.tty or '-'} role={role} session={client.session or '-'} term={client.termname or '-'} pid={client.pid or '-'}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tmux-client-lease")
    parser.add_argument("--dry-run", action="store_true", help="log tmux writes/detaches")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_client_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--client", default="")
        p.add_argument("--term", default="")
        p.add_argument("--pid", default="")
        p.add_argument("--session", default="")
        p.add_argument("--role", choices=[ROLE_DESKTOP, ROLE_MOBILE, ROLE_UNKNOWN], default=None)
        p.add_argument("--reason", default="")

    p_attach = sub.add_parser("attach")
    add_client_args(p_attach)
    p_attach.set_defaults(func=cmd_attach)

    p_activity = sub.add_parser("activity")
    add_client_args(p_activity)
    p_activity.set_defaults(func=cmd_activity)

    p_detach = sub.add_parser("detach")
    p_detach.add_argument("--client", default="")
    p_detach.set_defaults(func=cmd_detach)

    p_away = sub.add_parser("away")
    p_away.add_argument("--session", default="")
    p_away.add_argument("--reason", default="away")
    p_away.set_defaults(func=cmd_away)

    p_protect = sub.add_parser("protect")
    p_protect.add_argument("role")
    p_protect.add_argument("minutes", type=int)
    p_protect.set_defaults(func=cmd_protect)

    p_status = sub.add_parser("status")
    p_status.add_argument("--session", default="")
    p_status.set_defaults(func=cmd_status)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
