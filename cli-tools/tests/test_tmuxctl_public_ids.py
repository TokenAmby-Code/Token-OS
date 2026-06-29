from __future__ import annotations

import os
import pathlib
import subprocess
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


def test_translate_ids_cli_reads_stdin_and_writes_public_ids(tmp_path: pathlib.Path) -> None:
    fake_tmux = tmp_path / "tmux"
    fake_tmux.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2 $3 $4" = "list-panes -a -F #{pane_id}\t#{@PANE_ID}" ]; then\n'
        "  printf '%%7\tpalace:E\n%%9\tmechanicus:3\n'\n"
        "  exit 0\n"
        "fi\n"
        "printf 'unexpected: %s\n' \"$*\" >&2\n"
        "exit 64\n"
    )
    fake_tmux.chmod(0o755)
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "lib"),
        "IMPERIUM_TMUX_BIN": str(fake_tmux),
    }

    proc = subprocess.run(
        [sys.executable, "-m", "tmuxctl.cli", "translate-ids", "--unresolved", "missing"],
        input="focus %7 then %8",
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "focus palace:E then missing\n"
    assert "%" not in proc.stdout
