from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.public_ids import physical_to_public_id_map, translate_physical_ids


class FakeAdapter:
    def run(self, *args: str, allow_failure: bool = False) -> str:
        assert args[:4] == ("list-panes", "-a", "-F", "#{pane_id}\t#{@PANE_ID}")
        return "%7\tpalace:E\n%8\t\n%9\tmechanicus:3\n"


def test_public_id_map_uses_only_live_public_pane_ids() -> None:
    assert physical_to_public_id_map(FakeAdapter()) == {
        "%7": "palace:E",
        "%9": "mechanicus:3",
    }


def test_translate_physical_ids_never_falls_through_to_raw_tmux_id() -> None:
    text = translate_physical_ids(
        "send %7 then %8 and %404", physical_to_public_id_map(FakeAdapter())
    )
    assert text == "send palace:E then unresolved and unresolved"
    assert "%" not in text
