"""The retired `legion` page must not survive as a tmux window/stack *target*.

The topology merge (palace, somnium, council, mechanicus, reservists) retired the
per-fleet `legion` page. The colon-label sweep fixed `legion:` labels but left
bare-word `legion` as live window/stack targets in token-api:

- `_get_or_create_legion_pane` allocated the Golden-Throne autonomous-resume
  worker via `tmuxctl stack add legion` — but `legion` is no longer a stack base
  (`STACK_BASES = mechanicus, mars, kreig, reservists`), so the call hard-errors.
  The merged worker stack is `mechanicus`.
- `_create_custodes_legion_pane` built a `main:legion` window to seat Custodes;
  Custodes is now a FIXED council seat (built by `builder.ensure_council_window`),
  so the helper is dead and its legion-window construction must not exist.
- `morning_session` pre-asserted the seat with `stack enforce --window
  main:legion` before resolving `council:custodes`; the legion stack is gone.

These tests assert the council/mechanicus targets with tmux fully mocked.
"""

from __future__ import annotations

import pytest


class _FakeProc:
    def __init__(self, stdout: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
        return self._stdout, b""


@pytest.mark.asyncio
async def test_golden_throne_resume_pane_uses_mechanicus_stack_base(app_env, monkeypatch):
    """Autonomous resume files into the canonical `mechanicus` worker stack, not
    the retired `legion` stack base."""
    main = app_env.main
    captured: dict[str, tuple] = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _FakeProc(stdout=b"%77\n", returncode=0)

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", fake_exec)

    # The renamed helper is the canonical entry point — the legion-named one is gone.
    pane = await main._get_or_create_mechanicus_pane()

    assert pane == "%77"
    args = captured["args"]
    assert "stack" in args and "add" in args, args
    assert "mechanicus" in args, args
    assert "legion" not in args, args


def test_dead_custodes_legion_window_helper_removed(app_env):
    """The legion-window Custodes seeder is dead (Custodes is a fixed council
    seat). The live recovery helper stays."""
    main = app_env.main
    assert not hasattr(main, "_create_custodes_legion_pane")
    assert hasattr(main, "_find_custodes_tmux_pane")


def test_morning_seat_resolution_never_targets_legion_window(app_env, monkeypatch):
    """Morning launch resolves `council:custodes` and never pre-asserts a retired
    `:legion` window/stack."""
    import morning_session

    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = "%42"
        stderr = ""

    def fake_run(argv, *a, **k):
        calls.append([str(x) for x in argv])
        return _Result()

    monkeypatch.setattr(morning_session.subprocess, "run", fake_run)

    # Renamed off the retired-page vocabulary; resolves the durable council seat.
    pane = morning_session.resolve_custodes_pane()

    assert pane == "%42"
    flat = [tok for call in calls for tok in call]
    assert not any(tok.endswith(":legion") for tok in flat), calls
    assert any("council:custodes" in tok for tok in flat), calls
