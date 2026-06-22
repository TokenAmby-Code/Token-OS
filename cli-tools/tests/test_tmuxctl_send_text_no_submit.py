"""`send-text --no-submit` is the attended-safe draft path.

The clobber's auto-submit is the double C-m in `send_text_then_submit`. The
insert-only primitive (`insert_text`, tmux `-l` with no C-m) already exists and is
correct; `--no-submit` exposes it through `send-text` so a draft can be landed in
a pane WITHOUT ever pressing Enter over a human's in-progress input. The submit
path keeps its C-m semantics unchanged for the default (no-flag) case.
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import cli


def _fake_control():
    control = SimpleNamespace()
    control.adapter = MagicMock(name="adapter")
    control.insert_text = MagicMock(name="insert_text")
    return control


def _run(argv: list[str], assertion: dict | None = None):
    control = _fake_control()
    assertion = assertion or {"ok": True, "action": "none", "reason": "live"}
    with (
        patch.object(cli, "TmuxControlPlane", return_value=control),
        patch("tmuxctl.assertions.assert_instance", return_value=assertion),
    ):
        rc = cli.main(argv)
    return rc, control


def test_no_submit_routes_to_insert_only_never_submits():
    rc, control = _run(
        ["send-text", "--pane", "legion:custodes", "--text", "draft body", "--no-submit"]
    )

    assert rc == 0
    # Insert-only: the draft goes through insert_text (no C-m) ...
    control.insert_text.assert_called_once_with("legion:custodes", "draft body")
    # ... and the auto-submitting primitive is NEVER touched.
    control.adapter.send_text_then_submit.assert_not_called()


def test_default_send_text_still_submits():
    rc, control = _run(["send-text", "--pane", "legion:custodes", "--text", "go now"])

    assert rc == 0
    control.adapter.send_text_then_submit.assert_called_once()
    control.insert_text.assert_not_called()


def test_no_submit_is_mutually_exclusive_with_clear_prompt():
    # --clear-prompt issues C-u (a mutation); combining it with insert-only draft
    # mode is contradictory and must be refused (rc=1) before any pane write.
    rc, control = _run(
        [
            "send-text",
            "--pane",
            "legion:custodes",
            "--text",
            "x",
            "--no-submit",
            "--clear-prompt",
        ]
    )

    assert rc == 1
    control.insert_text.assert_not_called()
    control.adapter.send_text_then_submit.assert_not_called()
