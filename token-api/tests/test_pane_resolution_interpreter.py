"""Regression: token-api must resolve tmux panes with its OWN interpreter.

Post-#134 (canonical instance registry) ``/api/instances`` stopped reading a
stored ``tmux_pane`` column and instead resolves every row live by shelling
``python3 -m tmuxctl.cli resolve-instance`` (``shared.resolve_instance_pane``)
and ``resolve-pane`` (``shared.resolve_tmux_pane_id``).

On the live host, bare ``python3`` is first resolved through the Imperium uv
shim (``cli-tools/bin/python3`` -> ``uv run -- python``). Spawned from the
token-api working dir, that re-syncs token-api's ``.venv``; when the venv is
corrupt (missing/invalid ``RECORD`` files, as happened during the deploy flurry)
``uv run`` exits non-zero with NO stdout, so ``resolve_instance_pane`` parses an
empty payload, ``found`` is false, and EVERY row returns ``live_pane=false`` /
``tmux_pane=null`` / ``pane_label=null``.

The fix: resolve with ``sys.executable`` (the interpreter already running
token-api), never re-entering the PATH/uv shim. tmuxctl's CLI is stdlib-only, so
the running interpreter runs it directly via ``PYTHONPATH``.

These tests model the live host: a fake subprocess runner that returns a valid
payload only for ``sys.executable`` and mimics the broken shim (rc!=0, empty
stdout) for bare ``"python3"``.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

_GOOD_INSTANCE_JSON = (
    b'{"instance_id": "u", "pane_id": "%103", "pane_role": "legion:custodes", "found": true}'
)


def _dispatching_offloop(captured: list):
    """Build a fake ``_run_subprocess_offloop`` that behaves like the live host.

    Only ``sys.executable`` runs the stdlib-only tmuxctl CLI successfully; bare
    ``"python3"`` hits the uv shim and dies with no stdout (corrupt venv).
    """

    async def fake_offloop(args, *, timeout=None, stdout=None, stderr=None, env=None):
        argv = list(args)
        captured.append(argv)
        if argv[0] != sys.executable:
            return subprocess.CompletedProcess(
                args=argv, returncode=2, stdout=b"", stderr=b"error: RECORD file is invalid"
            )
        if "resolve-instance" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=_GOOD_INSTANCE_JSON, stderr=b""
            )
        # resolve-pane prints "pane_id: %NN"
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=b"pane_id: %103\n", stderr=b""
        )

    return fake_offloop


def test_resolve_instance_pane_uses_own_interpreter(app_env, monkeypatch):
    """resolve_instance_pane must spawn sys.executable, not the bare 'python3' shim."""
    shared = sys.modules["shared"]
    captured: list = []
    monkeypatch.setattr(shared, "_run_subprocess_offloop", _dispatching_offloop(captured))

    pane, role = asyncio.run(shared.resolve_instance_pane("u"))

    assert captured, "resolve_instance_pane did not spawn a subprocess"
    argv = captured[0]
    assert argv[0] == sys.executable, (
        f"resolution must use sys.executable ({sys.executable!r}), not {argv[0]!r} "
        "— bare 'python3' re-enters the uv shim and fails closed on a corrupt venv"
    )
    assert argv[0] != "python3"
    # With the correct interpreter the live pane resolves (the /api/instances
    # runtime block sets live_pane=bool(pane) directly from this return).
    assert pane == "%103"
    assert role == "legion:custodes"


def test_resolve_instance_pane_null_when_forced_through_python3_shim(app_env, monkeypatch):
    """Documents the regression: routing through bare 'python3' yields null panes.

    This is what /api/instances returned for every stamped seat before the fix.
    """
    shared = sys.modules["shared"]

    async def shim_only(args, *, timeout=None, stdout=None, stderr=None, env=None):
        # Simulate every spawn going through the broken shim regardless of argv.
        return subprocess.CompletedProcess(
            args=list(args), returncode=2, stdout=b"", stderr=b"error: RECORD file is invalid"
        )

    monkeypatch.setattr(shared, "_run_subprocess_offloop", shim_only)
    assert asyncio.run(shared.resolve_instance_pane("u")) == (None, None)


def test_resolve_tmux_pane_id_uses_own_interpreter(app_env, monkeypatch):
    """resolve_tmux_pane_id (resolve-pane) must also use sys.executable for non-% targets."""
    shared = sys.modules["shared"]
    captured: list = []
    monkeypatch.setattr(shared, "_run_subprocess_offloop", _dispatching_offloop(captured))
    # A non-% target forces the tmuxctl resolve-pane subprocess (a "%" id resolves
    # directly without shelling out).
    pane = asyncio.run(shared.resolve_tmux_pane_id("legion:custodes"))

    assert captured, "resolve_tmux_pane_id did not spawn a subprocess"
    argv = captured[0]
    assert argv[0] == sys.executable, (
        f"resolve-pane must use sys.executable ({sys.executable!r}), not {argv[0]!r}"
    )
    assert argv[0] != "python3"
    assert pane == "%103"
