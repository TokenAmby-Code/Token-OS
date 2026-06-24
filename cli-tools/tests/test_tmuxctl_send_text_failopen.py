"""Send-text fail-open when the persona-assert correction is stuck-suppressed.

The live-enforcement blocker (`persona_assert_suppressed_stuck`): a correctly
resolved peer-send / enforcement intervention to the live Custodes pane returned
`action=persona_correction_suppressed` and the byte-bearing payload was never
delivered — every send to `%25` needed the `TMUX_GUARD_SKIP` raw hatch.

Contract pinned here: when `assert_instance` reports the live runtime is present
but the persona correction is stuck after bounded attempts (`deliverable=True`,
typically `action=persona_correction_failopen`), `send-text` must FAIL OPEN —
deliver the payload + emit a loud diagnostic — instead of suppressing it. A
genuinely dead pane (no `deliverable` flag) must still refuse delivery.
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
    return control


def _run_send_text(assertion: dict, *, text: str = "enforcement payload"):
    control = _fake_control()
    with (
        patch.object(cli, "TmuxControlPlane", return_value=control),
        patch("tmuxctl.assertions.assert_instance", return_value=assertion) as assert_inst,
    ):
        rc = cli.main(["send-text", "--pane", "council:custodes", "--text", text])
    return rc, control, assert_inst


def test_send_text_fails_open_and_delivers_when_assertion_deliverable():
    rc, control, _ = _run_send_text(
        {
            "ok": False,
            "action": "persona_correction_failopen",
            "deliverable": True,
            "reason": "persona_assert_failopen attempts=4",
            "pane": "%25",
        }
    )

    assert rc == 0
    # The byte-bearing payload reached the pane despite the stuck correction.
    control.adapter.send_text_then_submit.assert_called_once()
    sent_args = control.adapter.send_text_then_submit.call_args
    assert sent_args.args[0] == "council:custodes"
    assert sent_args.args[1] == "enforcement payload"


def test_send_text_still_refuses_when_runtime_dead():
    # No `deliverable` flag → genuinely no live runtime (launched/launch_failed/
    # unregistered): the payload must NOT be sent and the command fails.
    rc, control, _ = _run_send_text(
        {
            "ok": False,
            "action": "launch_failed",
            "reason": "dispatch rc=1",
        }
    )

    assert rc == 1
    control.adapter.send_text_then_submit.assert_not_called()


def test_send_text_still_holds_during_bounded_correction():
    # A fresh `persona_correction_sent` (not yet stuck) still asks the caller to
    # retry after settle — the bounded hold before fail-open is preserved.
    rc, control, _ = _run_send_text(
        {
            "ok": False,
            "action": "persona_correction_sent",
            "reason": "sent",
        }
    )

    assert rc == 1
    control.adapter.send_text_then_submit.assert_not_called()


def test_send_text_delivers_normally_when_ok():
    rc, control, _ = _run_send_text({"ok": True, "action": "none", "reason": "live"})

    assert rc == 0
    control.adapter.send_text_then_submit.assert_called_once()
