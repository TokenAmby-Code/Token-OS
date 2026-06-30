"""Human-facing render sanitization.

Raw tmux physical pane ids (``%NNN``) are internal routing handles.  Human
surfaces (reports, TTS, push, Discord, CLI render text) must render the stable
public pane role instead (``mechanicus:1``, ``council:custodes``, ...).

This module is intentionally render-layer only: it accepts already-rendered text
and never participates in command targeting, DB keys, or tmux lookups used by
programmatic paths.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from pane_surface import RAW_TMUX_PANE_RX

_UNRESOLVED = "unresolved"


def _cli_lib_path() -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[1]
    return root / "tmuxctld" / "lib", root / "cli-tools" / "lib"


def _translate_with_tmuxctl(text: str, *, unresolved: str = _UNRESOLVED) -> str:
    """Translate ``%NNN`` tokens by delegating to tmuxctl's public-id resolver.

    ``tmuxctl translate-ids`` is the existing single translation primitive.  We
    shell it here rather than duplicating pane resolution in Token-API.  Callers
    must catch failures and fail safe because rendering a report must never be
    black-holed by tmux being unavailable.
    """
    tmuxctld_lib, cli_lib = _cli_lib_path()
    proc = subprocess.run(
        [sys.executable, "-m", "tmuxctl.cli", "translate-ids", "--unresolved", unresolved],
        input=text,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": f"{tmuxctld_lib}{os.pathsep}{cli_lib}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        },
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "tmuxctl translate-ids failed").strip())
    return proc.stdout


def sanitize_human_render_text_sync(
    text: str | None, *, unresolved: str = _UNRESOLVED
) -> str | None:
    """Return human-facing text with raw tmux pane ids translated or redacted.

    Known ids become stable public pane names via ``tmuxctl translate-ids``.
    Unknown ids (or a resolver failure) become ``unresolved``.  Raw ``%NNN``
    tokens are never allowed through this render boundary.
    """
    if text is None:
        return None
    value = str(text)
    if not RAW_TMUX_PANE_RX.search(value):
        return value
    try:
        translated = _translate_with_tmuxctl(value, unresolved=unresolved)
        # The CLI preserves a trailing newline for stdin convenience.  This
        # helper is used on in-memory render strings, so preserve the caller's
        # original newline shape.
        if not value.endswith("\n") and translated.endswith("\n"):
            translated = translated[:-1]
    except Exception:
        translated = RAW_TMUX_PANE_RX.sub(unresolved, value)
    # Defense in depth for malformed resolver output or future regressions.
    return RAW_TMUX_PANE_RX.sub(unresolved, translated)


async def sanitize_human_render_text(
    text: str | None, *, unresolved: str = _UNRESOLVED
) -> str | None:
    """Async wrapper for render paths already running on the Token-API loop."""
    if text is None or not RAW_TMUX_PANE_RX.search(str(text)):
        return text
    return await asyncio.to_thread(
        sanitize_human_render_text_sync, str(text), unresolved=unresolved
    )
