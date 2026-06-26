"""Regression tests for the tmux PATH shim self-fork-bomb (P0).

Background: when ``IMPERIUM_TMUX_BIN`` / ``REAL_TMUX`` pointed at the shim
itself, ``find_real_tmux`` resolved ``_REAL_TMUX`` to the shim. The preamble
then re-exported the poisoned value and re-invoked the shim (both via
``exec`` and via ``$(...)`` command-subs like ``display-message`` /
``show-options``), exponentially exhausting the process table and bricking
the machine.

Two defenses are tested here:
  1. ``find_real_tmux`` must REJECT a self-pointing ``IMPERIUM_TMUX_BIN`` /
     ``REAL_TMUX`` and fall through to real-binary discovery.
  2. A hard, resolution-independent recursion fuse
     (``IMPERIUM_TMUX_SHIM_DEPTH``) must abort LOUDLY with a non-zero exit
     once nesting exceeds a small bound, so no future resolution bug can
     ever fork-bomb the machine again.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
TMUX_SHIM = ROOT / "bin" / "tmux"

MARKER = "REAL_TMUX_RAN"


def _fake_real_tmux(tmp_path: pathlib.Path) -> pathlib.Path:
    """A stand-in 'real' tmux that echoes a marker and exits once."""
    fake = tmp_path / "real-tmux"
    fake.write_text(f'#!/usr/bin/env bash\necho "{MARKER} $*"\nexit 0\n')
    fake.chmod(0o755)
    return fake


def _path_tmux_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """A PATH directory whose `tmux` is a real (non-shim) binary.

    Used so that when a self-pointing override is correctly rejected,
    resolution falls through to a deterministic 'real' tmux instead of
    whatever system tmux happens to be installed.
    """
    bindir = tmp_path / "pathbin"
    bindir.mkdir()
    tmux = bindir / "tmux"
    tmux.write_text(f'#!/usr/bin/env bash\necho "{MARKER} $*"\nexit 0\n')
    tmux.chmod(0o755)
    return bindir


def _base_env(tmp_path: pathlib.Path) -> dict[str, str]:
    env = {**os.environ}
    # Keep the passthrough path clean; resolution runs regardless of RAW.
    env["IMPERIUM_TMUX_RAW"] = "1"
    for key in (
        "IMPERIUM_TMUX_BIN",
        "REAL_TMUX",
        "IMPERIUM_TMUX_SHIM_DEPTH",
        "IMPERIUM_TMUX_AUTOMATION",
        "TOKEN_API_INTERNAL_DISPATCH",
    ):
        env.pop(key, None)
    return env


def _run(env: dict[str, str], *args: str, timeout: int = 30):
    return subprocess.run(
        [str(TMUX_SHIM), *args],
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
    )


def test_self_pointing_imperium_tmux_bin_falls_through(tmp_path) -> None:
    """A self-pointing IMPERIUM_TMUX_BIN must be rejected, not exec'd."""
    env = _base_env(tmp_path)
    bindir = _path_tmux_dir(tmp_path)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    # Poison: point the override at the shim itself.
    env["IMPERIUM_TMUX_BIN"] = str(TMUX_SHIM)

    proc = _run(env, "display-message", "-p", "hello")

    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    # Resolution fell through to the real (PATH) tmux exactly once.
    assert proc.stdout.count(MARKER) == 1, proc.stdout
    assert "display-message" in proc.stdout


def test_self_pointing_real_tmux_falls_through(tmp_path) -> None:
    """A self-pointing REAL_TMUX must be rejected, not exec'd."""
    env = _base_env(tmp_path)
    bindir = _path_tmux_dir(tmp_path)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    # IMPERIUM_TMUX_BIN unset (popped in _base_env); poison REAL_TMUX.
    env["REAL_TMUX"] = str(TMUX_SHIM)

    proc = _run(env, "display-message", "-p", "hello")

    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert proc.stdout.count(MARKER) == 1, proc.stdout
    assert "display-message" in proc.stdout


def test_depth_fuse_aborts_loudly_above_bound(tmp_path) -> None:
    """Pre-seeding the depth counter above the bound must abort loudly.

    This simulates a deep nested invocation without actually forking.
    With a valid real tmux available, the shim would normally pass the
    command through; the fuse must pre-empt that and refuse.
    """
    env = _base_env(tmp_path)
    env["IMPERIUM_TMUX_BIN"] = str(_fake_real_tmux(tmp_path))
    env["IMPERIUM_TMUX_SHIM_DEPTH"] = "99"

    proc = _run(env, "display-message", "-p", "hello")

    # The recursion fuse aborts with the documented EX_SOFTWARE (70); lock the
    # contract to that exact code, not merely "any non-zero".
    assert proc.returncode == 70, (proc.returncode, proc.stdout, proc.stderr)
    # The real tmux must NOT have been invoked — the fuse pre-empted it.
    assert MARKER not in proc.stdout, proc.stdout
    # Loud: a clear recursion error on stderr.
    assert "recursion" in proc.stderr.lower() or "depth" in proc.stderr.lower(), proc.stderr


def test_depth_fuse_fails_closed_on_malformed_counter(tmp_path) -> None:
    """A non-integer / negative inherited counter must trip the fuse.

    A negative pre-seed (e.g. -100) would otherwise keep ``> max`` false for
    many re-entries and silently defeat the fork-bomb guard, so the shim must
    reject any value that is not a bare non-negative integer.
    """
    for bad in ("-100", "abc", "1; rm", "3.5"):
        env = _base_env(tmp_path)
        env["IMPERIUM_TMUX_BIN"] = str(_fake_real_tmux(tmp_path))
        env["IMPERIUM_TMUX_SHIM_DEPTH"] = bad

        proc = _run(env, "display-message", "-p", "hello")

        assert proc.returncode == 70, (bad, proc.returncode, proc.stdout, proc.stderr)
        assert MARKER not in proc.stdout, (bad, proc.stdout)
        assert "IMPERIUM_TMUX_SHIM_DEPTH" in proc.stderr, (bad, proc.stderr)


def test_depth_fuse_allows_normal_single_invocation(tmp_path) -> None:
    """The fuse must not interfere with an ordinary (depth 1) call."""
    env = _base_env(tmp_path)
    env["IMPERIUM_TMUX_BIN"] = str(_fake_real_tmux(tmp_path))

    proc = _run(env, "display-message", "-p", "hello")

    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert proc.stdout.count(MARKER) == 1, proc.stdout
