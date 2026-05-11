from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.labels import canonical_pane_role


def test_palace_legacy_roles_collapse_to_h_layout():
    assert canonical_pane_role("palace:WW") == "palace:W"
    assert canonical_pane_role("palace:EE") == "palace:E"
    assert canonical_pane_role("palace:NW") == "palace:N"
    assert canonical_pane_role("palace:NE") == "palace:N"
    assert canonical_pane_role("palace:SW") == "palace:S"
    assert canonical_pane_role("palace:SE") == "palace:S"


def test_somnium_legacy_left_column_collapses_and_right_grid_survives():
    assert canonical_pane_role("somnium:NW") == "somnium:W"
    assert canonical_pane_role("somnium:SW") == "somnium:W"
    assert canonical_pane_role("somnium:NE") == "somnium:NE"
    assert canonical_pane_role("somnium:SE") == "somnium:SE"
