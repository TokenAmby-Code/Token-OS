from __future__ import annotations

from dataclasses import replace
from typing import Any

from .assertions import PERSONA_LABELS, launch_persona_seat, persona_spec
from .resolver import resolve_pane
from .tmux_adapter import TmuxAdapter

ENGINES = {"claude", "codex"}


def _normalize_engine(value: str) -> str:
    engine = (value or "").strip().lower()
    if engine not in ENGINES:
        raise ValueError("engine must be claude or codex")
    return engine


def _current_engine(adapter: TmuxAdapter, pane_id: str, fallback: str) -> str:
    value = (adapter.show_pane_option(pane_id, "@TOKEN_API_ENGINE") or "").strip().lower()
    if value in ENGINES:
        return value
    return _normalize_engine(fallback)


def rotate_persona_engine(
    adapter: TmuxAdapter,
    target: str,
    *,
    engine: str | None = None,
    toggle: bool = False,
    session: str | None = None,
) -> dict[str, Any]:
    """Respawn a protected persona singleton seat with another engine.

    This is the attended hot-swap primitive behind the tmux keystroke: it never
    edits the registry directly. The old engine is killed by tmux ``respawn-pane``;
    WrapperEnd/SessionStart remain the lifecycle source of truth.
    """
    if engine and toggle:
        raise ValueError("use either --engine or --toggle, not both")

    resolved = resolve_pane(adapter, target, session_name=session)
    pane_id = resolved.pane_id
    pane_label = resolved.pane_role or adapter.show_pane_option(pane_id, "@PANE_ID")
    if pane_label not in PERSONA_LABELS:
        raise ValueError(f"pane is not a protected persona seat: {pane_label or pane_id}")

    spec = persona_spec(pane_label)
    current = _current_engine(adapter, pane_id, spec.engine)
    target_engine = (
        _normalize_engine(engine) if engine else ("codex" if current == "claude" else "claude")
    )
    if not toggle and not engine:
        raise ValueError("must pass --engine or --toggle")

    launch_spec = replace(spec, engine=target_engine)
    ok, reason = launch_persona_seat(adapter, pane_id, launch_spec, session=session)
    return {
        "ok": ok,
        "pane": pane_id,
        "pane_label": pane_label,
        "persona": spec.persona,
        "previous_engine": current,
        "engine": target_engine,
        "action": "engine_rotated" if ok else "engine_rotate_failed",
        "reason": reason,
    }
