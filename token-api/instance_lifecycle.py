from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import shared
from instance_mutation import sanctioned_update_instance
from personas import BLACK_SHIELDS, seed_params
from session_doc_helpers import update_frontmatter

logger = logging.getLogger("token_api")

LIFECYCLE_ALIASES = {
    "stop": "stop",
    "retire": "retire",
    "retire-only": "retire",
    "archive": "archive-session-doc",
    "archive-doc": "archive-session-doc",
    "archive-session-doc": "archive-session-doc",
    "retire-and-archive": "archive-session-doc",
    "banish": "banish",
}


def normalize_instance_lifecycle(value: str | None) -> str | None:
    lifecycle = (value or "retire").strip().lower()
    return LIFECYCLE_ALIASES.get(lifecycle)


def _resolve_session_doc_path(raw_path: str | None) -> Path | None:
    text = (raw_path or "").strip()
    if not text:
        return None
    path = Path(text)
    if path.is_absolute():
        return path
    return shared._vault_root() / path  # noqa: SLF001 - shared owns lazy vault resolution.


async def _ensure_persona_seed(db, seed) -> str:
    await db.execute(
        """INSERT INTO personas
           (id, slug, display_name, default_rank, assignment_pool, assignment_order,
            pane_tint, chip_color, tts_voice, tts_rate, notification_sound)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             slug = excluded.slug,
             display_name = excluded.display_name,
             default_rank = excluded.default_rank,
             assignment_pool = excluded.assignment_pool,
             assignment_order = excluded.assignment_order,
             pane_tint = excluded.pane_tint,
             chip_color = excluded.chip_color,
             tts_voice = excluded.tts_voice,
             tts_rate = excluded.tts_rate,
             notification_sound = excluded.notification_sound""",
        seed_params(seed),
    )
    return seed.id


def _row_get(row, key: str):
    if hasattr(row, "keys"):
        return row[key]
    index = {
        "id": 0,
        "device_id": 1,
        "session_doc_id": 2,
        "commander_type": 3,
        "is_subagent": 4,
        "default_rank": 5,
    }[key]
    return row[index]


async def apply_instance_lifecycle(
    db,
    instance_id: str,
    *,
    lifecycle: str = "retire",
    write_source: str = "api",
    actor: str | None = None,
) -> dict[str, Any]:
    """Apply one canonical instance lifecycle transition.

    Token-API owns DB/session-doc state. tmuxctl owns pane runtime state.
    This function intentionally does not mutate tmux.
    """
    normalized = normalize_instance_lifecycle(lifecycle)
    if not normalized:
        return {"status": "failed", "reason": "unsupported_lifecycle", "lifecycle": lifecycle}

    cursor = await db.execute(
        """SELECT i.id, i.device_id, i.session_doc_id, i.commander_type,
                  COALESCE(i.is_subagent, 0) AS is_subagent, p.default_rank
           FROM instances i
           LEFT JOIN personas p ON p.id = i.persona_id
           WHERE i.id = ?""",
        (instance_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return {"status": "failed", "reason": "instance_not_found", "instance_id": instance_id}

    now = datetime.now().isoformat()
    updates: dict[str, Any] = {"input_lock": None, "stopped_at": now, "golden_throne": None}
    mutation_type = "instance_stopped"
    event_type = "instance_stopped"
    result: dict[str, Any] = {
        "instance_id": instance_id,
        "lifecycle": normalized,
        "device_id": _row_get(row, "device_id"),
        "is_subagent": int(_row_get(row, "is_subagent") or 0),
        "session_doc_id": _row_get(row, "session_doc_id"),
    }

    if normalized == "stop":
        updates["status"] = "stopped"
        actor = actor or "stop-instance"
    elif normalized == "retire":
        updates.update({"status": "stopped", "rank": "retired"})
        mutation_type = "instance_retired"
        event_type = "instance_retired"
        actor = actor or "retire-instance"
        result.update({"rank": "retired"})
    elif normalized == "archive-session-doc":
        doc_id = _row_get(row, "session_doc_id")
        if doc_id:
            cursor = await db.execute(
                "SELECT file_path FROM session_documents WHERE id = ?",
                (doc_id,),
            )
            doc_row = await cursor.fetchone()
            if not doc_row:
                return {
                    "status": "failed",
                    "reason": "session_doc_not_found",
                    "session_doc_id": doc_id,
                    "lifecycle": normalized,
                }
            doc_path_text = doc_row[0]
            await db.execute(
                "UPDATE session_documents SET status = 'archived', updated_at = ? WHERE id = ?",
                (now, doc_id),
            )
            doc_path = _resolve_session_doc_path(doc_path_text)
            if doc_path and doc_path.exists():
                try:
                    await asyncio.to_thread(update_frontmatter, doc_path, {"status": "archived"})
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "archive-session-doc frontmatter update failed for %s: %s", doc_path, exc
                    )
        updates.update({"status": "archived", "rank": "retired"})
        mutation_type = "instance_archived"
        event_type = "instance_session_doc_archived"
        actor = actor or "archive-session-doc"
        result.update({"rank": "retired"})
    elif normalized == "banish":
        updates["status"] = "stopped"
        banished_to = None
        if (
            _row_get(row, "commander_type") == "chapter"
            and (_row_get(row, "default_rank") or "astartes") == "astartes"
        ):
            updates["persona_id"] = await _ensure_persona_seed(db, BLACK_SHIELDS)
            updates["commander_type"] = "emperor"
            updates["commander_id"] = None
            banished_to = BLACK_SHIELDS.slug
        mutation_type = "instance_banished"
        event_type = "instance_banished"
        actor = actor or "banish-instance"
        result["persona"] = banished_to

    await sanctioned_update_instance(
        db,
        instance_id=instance_id,
        updates=updates,
        mutation_type=mutation_type,
        write_source=write_source,
        actor=actor or normalized,
    )
    result.update(
        {
            "status": updates["status"],
            "mutation_type": mutation_type,
            "event_type": event_type,
        }
    )
    return result
