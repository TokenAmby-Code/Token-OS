#!/usr/bin/env python3
"""Headless Deskflow client supervisor with bounded retry.

Runs ``deskflow-core client`` directly (no GUI — the GUI's modal dialogs
de-fullscreen the monitor) and bounds its hardcoded ~3s re-dial loop, which
deskflow v1.26 exposes no knob for: the retry interval is a compiled-in
constant, the no-retry branch is dead code, and ``--no-restart`` is
parse-noise. Bounded retry therefore lives here, outside the core:

  * Not yet connected: if no "connected to server" line arrives within
    CONNECT_WINDOW, kill the core and exit 0 ("went quiet").
  * On "disconnected from server": arm RECONNECT_WINDOW so transient blips
    self-heal fast; if the core has not reconnected by the deadline, kill it
    and exit 0.
  * While connected: block indefinitely.

Once quiet, only a WSL satellite invite (POST /api/kvm/start → launchctl
kickstart) or a manual start revives the client — by design. The satellite
owns retry from there (bounded ladder + eager boot invite).

The two marker lines are IPC-level output the Deskflow GUI itself parses
(marked must-not-change upstream) and print regardless of ``log/level``.

Runs under launchd as com.imperium.deskflow-client (KeepAlive=false,
RunAtLoad=false). Stdlib-only; /usr/bin/python3. The repo copy under Shell/
is the source of truth; token-api syncs it to
~/Library/Application Support/Imperium/ (launchd needs local exec — TCC
blocks NAS executables).
"""

import argparse
import os
import select
import signal
import subprocess
import sys
import time

DESKFLOW_CORE = "/Applications/Deskflow.app/Contents/MacOS/deskflow-core"
CONNECT_WINDOW = 20.0  # seconds to first connect before going quiet
RECONNECT_WINDOW = 15.0  # seconds to recover from a drop before going quiet
TERM_GRACE = 3.0  # seconds between SIGTERM and SIGKILL on the core

CONNECTED_MARKER = "connected to server"
DISCONNECTED_MARKER = "disconnected from server"


class Supervisor:
    """Pure deadline state machine — no process or clock of its own.

    ``handle_line(line, now)`` consumes one core stdout line; ``expired(now)``
    answers "should the core be killed?". The runner owns Popen/select; tests
    drive this class directly with real log strings and synthetic clocks.
    """

    def __init__(
        self,
        connect_window: float = CONNECT_WINDOW,
        reconnect_window: float = RECONNECT_WINDOW,
        start_time: float = 0.0,
    ):
        self.connect_window = connect_window
        self.reconnect_window = reconnect_window
        # Armed from birth: the core must connect within the connect window.
        self.deadline = start_time + connect_window
        self.connected = False
        self.connected_once = False

    def handle_line(self, line: str, now: float) -> None:
        # Order matters defensively, though "disconnected from server" does
        # not contain "connected to server" as a substring.
        if DISCONNECTED_MARKER in line:
            self.connected = False
            self.deadline = now + self.reconnect_window
        elif CONNECTED_MARKER in line:
            self.connected = True
            self.connected_once = True
            self.deadline = None

    @property
    def window_name(self) -> str:
        return "reconnect" if self.connected_once else "connect"

    def expired(self, now: float) -> bool:
        return self.deadline is not None and now >= self.deadline

    def timeout(self, now: float):
        """select() timeout until the armed deadline; None = block forever."""
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - now)


def _kill_core(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    deadline = time.monotonic() + TERM_GRACE
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    proc.kill()
    proc.wait()


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--connect-window", type=float, default=CONNECT_WINDOW)
    parser.add_argument("--reconnect-window", type=float, default=RECONNECT_WINDOW)
    parser.add_argument("--core", default=DESKFLOW_CORE)
    args = parser.parse_args(argv)

    env = dict(os.environ)
    # $XDG_CONFIG_HOME overrides ~/Library/Deskflow/Deskflow.conf resolution
    # on macOS — never let launchd env redirect the config.
    env.pop("XDG_CONFIG_HOME", None)

    proc = subprocess.Popen(
        [args.core, "client"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        env=env,
    )

    terminating = False

    def _on_term(signum, frame):
        nonlocal terminating
        terminating = True
        # Closes the core's stdout, which wakes the select loop with EOF.
        if proc.poll() is None:
            proc.terminate()

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    sup = Supervisor(args.connect_window, args.reconnect_window, time.monotonic())
    fd = proc.stdout.fileno()
    buf = b""

    while True:
        now = time.monotonic()
        if sup.expired(now):
            print(
                f"supervisor: {sup.window_name} window expired — killing core, going quiet",
                flush=True,
            )
            _kill_core(proc)
            return 0

        readable, _, _ = select.select([fd], [], [], sup.timeout(now))
        if not readable:
            continue  # loop re-checks expiry

        chunk = os.read(fd, 4096)
        if not chunk:
            # Core exited (or we terminated it). Reap and report.
            code = proc.wait()
            if terminating:
                print("supervisor: stopped by signal", flush=True)
                return 0
            print(f"supervisor: core exited on its own (code {code})", flush=True)
            return code

        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.decode("utf-8", errors="replace")
            print(line, flush=True)
            sup.handle_line(line, time.monotonic())


if __name__ == "__main__":
    sys.exit(run())
