"""CLI surface for the periodic persona-pane registration sweep.

`tmuxctl assert-personas` runs the same per-pane assert_instance the restart path
runs, once per PERSONA_LABELS entry, WITHOUT a teardown — the periodic reconciler
the cron engine fires so a silently-dropped registration self-heals within minutes
instead of staying dead until the next `tx restart`. It always exits 0: an
unregistered/absent persona pane is the expected state this sweep NOTES and heals,
not a hard failure that should mark every idempotent cron run as an error.
"""

from __future__ import annotations

import json
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


def _run(argv, sweep_results, capsys):
    control = _fake_control()
    with (
        patch.object(cli, "TmuxControlPlane", return_value=control),
        patch("tmuxctl.assertions.sweep_persona_panes", return_value=sweep_results) as sweep,
    ):
        rc = cli.main(argv)
    out = capsys.readouterr().out
    return rc, out, sweep


def test_assert_personas_json_exits_zero(capsys):
    results = [
        {"ok": True, "pane_label": "council:custodes", "action": "none", "reason": "live"},
        {
            "ok": False,
            "pane_label": "council:administratum",
            "action": "persona_unregistered_noted",
            "reason": "persona_unregistered_live_runtime",
        },
    ]
    rc, out, sweep = _run(["assert-personas"], results, capsys)

    assert rc == 0  # an unregistered pane is informational, never a hard failure
    sweep.assert_called_once()
    assert json.loads(out) == results


def test_assert_personas_text_format(capsys):
    results = [
        {"ok": True, "pane_label": "council:custodes", "action": "none", "reason": "live"},
        {
            "ok": False,
            "pane_label": "council:administratum",
            "action": "persona_unregistered_noted",
            "reason": "live_runtime_no_row",
        },
    ]
    rc, out, _ = _run(["assert-personas", "--format", "text"], results, capsys)

    assert rc == 0
    lines = out.strip().splitlines()
    assert lines[0] == "council:custodes\tok\tnone\tlive"
    assert lines[1] == "council:administratum\tFAIL\tpersona_unregistered_noted\tlive_runtime_no_row"
