"""Phase 2-A1 — timer status de-poll: @TIMER_SEG push + format fidelity.

The status bar used to fork `#(tmux-status)` every status-interval (a blocking
GET /api/timer over SMB on each render). Token-API's timer worker now pushes a
pre-formatted segment to the GLOBAL tmux option @TIMER_SEG, read in-format with
zero fork. These tests pin the two halves of that swap:

  1. `_format_timer_status_segment()` reproduces cli-tools/bin/tmux-status's
     default (no-flag) output BYTE-FOR-BYTE — cross-checked by actually running the
     legacy script's main() with a synthetic /api/timer payload and diffing stdout.
     If the bar formatting ever drifts, this fails.
  2. `_timer_push_segment()` delivers `tmux set-option -g @TIMER_SEG <seg>` through
     the off-loop subprocess runner and fails closed (never raises).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# Repo root: token-api/tests/<this> -> parents[2].
_TMUX_STATUS_PATH = Path(__file__).resolve().parents[2] / "cli-tools" / "bin" / "tmux-status"


def _load_legacy_tmux_status() -> Any:
    """Import the extensionless cli-tools/bin/tmux-status as a module."""
    loader = SourceFileLoader("legacy_tmux_status", str(_TMUX_STATUS_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _api_timer_payload(mode: str, break_balance_ms: int) -> dict:
    """Shape a /api/timer dict the way main.get_timer_state() does, for the fields
    tmux-status reads — so the legacy script formats from the same inputs the live
    helper derives straight off the engine."""
    return {
        "current_mode": mode,
        "is_in_backlog": break_balance_ms < 0,
        "break_backlog_ms": abs(min(0, break_balance_ms)),
        "accumulated_break_seconds": round(max(0, break_balance_ms) / 1000),
    }


def _legacy_segment(legacy: Any, monkeypatch: Any, mode: str, break_balance_ms: int) -> str:
    """Run the legacy tmux-status main() (no flags, as the live bar invoked it) and
    return its printed segment, trailing newline stripped (tmux strips it too)."""
    monkeypatch.setattr(legacy, "get_timer", lambda: _api_timer_payload(mode, break_balance_ms))
    monkeypatch.setattr(legacy.sys, "argv", ["tmux-status"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        legacy.main()
    return buf.getvalue().rstrip("\n")


# (mode, break_balance_ms) cases spanning every icon and balance branch, plus the
# rounding/colour boundaries where a naive reimplementation would drift:
#   - colour thresholds at exactly 1800s / 1801s / 0 / sub-second
#   - hours formatting with and without trailing minutes
#   - sub-millisecond backlog that rounds to "+0s"
#   - non-balance modes (idle/sleeping) and an unknown mode
_CASES = [
    ("working", 2000 * 1000),  # green  +33m
    ("working", 1800 * 1000),  # yellow +30m  (boundary: not > 1800)
    ("working", 1801 * 1000),  # green  +30m  (boundary: > 1800)
    ("working", 840 * 1000),  # yellow +14m
    ("working", 0),  # red    +0s
    ("working", 999),  # yellow +1s   (rounds up to 1s)
    ("working", 1),  # red    +0s   (rounds to 0s)
    ("multitasking", 3700 * 1000),  # green  +1h01m
    ("multitasking", 3600 * 1000),  # green  +1h
    ("break", -120 * 1000),  # red    -2m
    ("break", -45 * 1000),  # red    -45s
    ("break", -1),  # red    +0s   (backlog rounds to 0)
    ("distracted", -7200 * 1000),  # red    -2h
    ("idle", 5000 * 1000),  # icon only (idle not a balance mode)
    ("sleeping", -5000 * 1000),  # icon only
    ("bogus-mode", 0),  # "?" fallback icon, no balance
]


@pytest.mark.parametrize(("mode", "break_balance_ms"), _CASES)
def test_segment_matches_legacy_tmux_status(
    app_env: Any, monkeypatch: Any, mode: str, break_balance_ms: int
) -> None:
    main = app_env.main
    legacy = _load_legacy_tmux_status()

    expected = _legacy_segment(legacy, monkeypatch, mode, break_balance_ms)
    actual = main._format_timer_status_segment(mode, break_balance_ms)

    assert actual == expected, (
        f"mode={mode} bal_ms={break_balance_ms}: ported segment {actual!r} "
        f"!= legacy tmux-status {expected!r}"
    )


def test_segment_golden_values(app_env: Any) -> None:
    """A few explicit goldens so the rendered contract is legible in the diff."""
    main = app_env.main
    assert (
        main._format_timer_status_segment("working", 2000 * 1000) == "💼 #[fg=green]+33m#[default]"
    )
    assert main._format_timer_status_segment("break", -120 * 1000) == "☕ #[fg=red]-2m#[default]"
    assert main._format_timer_status_segment("idle", 9 * 1000) == "⏸"
    assert main._format_timer_status_segment("sleeping", 0) == "🌙"


def test_case_insensitive_mode(app_env: Any) -> None:
    """The engine yields lowercase mode values, but the helper lower-cases anyway
    (matching tmux-status) so an upper/mixed-case mode still resolves its icon."""
    main = app_env.main
    assert (
        main._format_timer_status_segment("WORKING", 840 * 1000) == "💼 #[fg=yellow]+14m#[default]"
    )


def _capture_offloop(main: Any, monkeypatch: Any) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    async def _fake_offloop(cmd, **kwargs):
        calls.append(tuple(cmd))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(main, "_run_subprocess_offloop", _fake_offloop)
    return calls


async def test_push_segment_sets_global_option(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    calls = _capture_offloop(main, monkeypatch)

    seg = "💼 #[fg=green]+14m#[default]"
    await main._timer_push_segment(seg)

    assert calls == [("tmux", "set-option", "-g", "@TIMER_SEG", seg)]


async def test_push_segment_fails_closed(app_env: Any, monkeypatch: Any) -> None:
    """A tmux failure (no server, timeout) must never propagate out of the worker."""
    main = app_env.main

    async def _boom(cmd, **kwargs):
        raise RuntimeError("no tmux server")

    monkeypatch.setattr(main, "_run_subprocess_offloop", _boom)

    # Must not raise.
    await main._timer_push_segment("💼 #[fg=green]+14m#[default]")
