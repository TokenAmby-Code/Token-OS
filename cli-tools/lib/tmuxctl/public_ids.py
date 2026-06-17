from __future__ import annotations

import re

from .labels import canonical_pane_role
from .tmux_adapter import TmuxAdapter

RAW_TMUX_ID_RE = re.compile(r"%\d+")


def physical_to_public_id_map(adapter: TmuxAdapter) -> dict[str, str]:
    """Return the live ``%pane_id -> @PANE_ID`` public-id map.

    This is the single tmuxctl translation primitive for text that may otherwise
    print volatile tmux ids. Panes with no public ``@PANE_ID`` are intentionally
    omitted; callers must fail closed rather than echoing the raw id.
    """
    raw = adapter.run(
        "list-panes",
        "-a",
        "-F",
        "\t".join(["#{pane_id}", "#{@PANE_ID}"]),
        allow_failure=True,
    )
    mapping: dict[str, str] = {}
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        physical, public = parts[0].strip(), parts[1].strip()
        if not physical or not public or physical == public:
            continue
        canonical = canonical_pane_role(public)
        if canonical and not canonical.startswith("%"):
            mapping[physical] = canonical
    return mapping


def translate_physical_ids(
    text: str,
    mapping: dict[str, str] | None = None,
    *,
    unresolved: str = "unresolved",
) -> str:
    """Translate every raw tmux ``%NNN`` in text to a public pane id.

    Unknown raw ids are replaced with ``unresolved``. They are never allowed to
    pass through to human-facing output.
    """
    table = mapping or {}

    def repl(match: re.Match[str]) -> str:
        return table.get(match.group(0), unresolved)

    return RAW_TMUX_ID_RE.sub(repl, text)


def translate_stdin_to_stdout(adapter: TmuxAdapter, stdin_text: str) -> str:
    return translate_physical_ids(stdin_text, physical_to_public_id_map(adapter))
