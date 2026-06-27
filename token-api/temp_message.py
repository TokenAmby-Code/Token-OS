"""Temporary side-channel message dispatch for orchestrator polls."""

from __future__ import annotations

import asyncio
import inspect
import re
import subprocess
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from shared import DB_PATH

TEMP_MESSAGE_SOURCE = "temp_message"
TEMP_MESSAGE_PURPOSE = "orchestrator_poll"
POLL_TTL_MINUTES = 10

QueueSender = Callable[..., Awaitable[dict[str, Any]]]
QueueDrainer = Callable[..., Awaitable[list[dict[str, Any]]]]


class SelectorError(ValueError):
    """Raised when a temp-message selector is invalid."""


def temp_command_for_engine(engine: str | None, payload: str) -> tuple[str, str]:
    normalized = (engine or "").strip().lower()
    if normalized == "claude":
        return f"/btw {payload}", "side_channel"
    if normalized == "codex":
        return f"/side {payload}", "side_channel"
    return payload, "direct_unknown_engine"


def temp_channel_metadata(engine: str | None) -> dict[str, Any]:
    normalized = (engine or "").strip().lower()
    if normalized == "claude":
        return {
            "command": "/btw",
            "ephemeral": True,
            "tool_calls_inert": True,
            "availability": "expected",
            "caveats": [],
        }
    if normalized == "codex":
        return {
            "command": "/side",
            "ephemeral": True,
            "tool_calls_inert": False,
            "availability": "not_preflighted",
            "caveats": [
                "requires_started_conversation",
                "unavailable_during_code_review",
                "unavailable_if_side_conversation_already_open",
            ],
        }
    return {
        "command": None,
        "ephemeral": False,
        "tool_calls_inert": False,
        "availability": "unknown_engine_direct_payload",
        "caveats": ["conversation_pollution"],
    }


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def send_temp_message(
    pane: str,
    payload: str,
    engine: str | None = None,
    *,
    instance_id: str | None = None,
    queue_sender: QueueSender | None = None,
    queue_drainer: QueueDrainer | None = None,
) -> dict[str, Any]:
    """Queue a temporary message for one tmux pane.

    Production callers pass ``main.enqueue_pane_write`` and
    ``main.process_pane_write_queue_once`` so dispatch uses the existing
    server-owned typing guard and send-keys implementation.
    """
    command_payload, mode = temp_command_for_engine(engine, payload)
    if queue_sender is None:
        raise RuntimeError("send_temp_message requires the existing pane-write queue sender")

    queued = await _maybe_await(
        queue_sender(
            instance_id=instance_id or pane,
            tmux_pane=pane,
            source=TEMP_MESSAGE_SOURCE,
            purpose=TEMP_MESSAGE_PURPOSE,
            payload=command_payload,
        )
    )
    result: dict[str, Any] = {
        "instance_id": instance_id,
        "tmux_pane": pane,
        "engine": engine,
        "mode": mode,
        "channel": temp_channel_metadata(engine),
        "queue_id": queued.get("id"),
        "queued_status": queued.get("status"),
        "payload": command_payload,
    }
    if queue_drainer and queued.get("id"):
        drained = await _maybe_await(queue_drainer(queued["id"]))
        result["dispatch"] = drained[0] if drained else None
        if drained:
            result["status"] = drained[0].get("status", queued.get("status"))
        else:
            result["status"] = queued.get("status")
    else:
        result["status"] = queued.get("status")
    return result


def preview_temp_message(
    pane: str,
    payload: str,
    engine: str | None = None,
    *,
    instance_id: str | None = None,
) -> dict[str, Any]:
    """Build a temp-message receipt without enqueueing or dispatching to tmux."""
    command_payload, mode = temp_command_for_engine(engine, payload)
    return {
        "instance_id": instance_id,
        "tmux_pane": pane,
        "engine": engine,
        "mode": mode,
        "channel": temp_channel_metadata(engine),
        "queue_id": None,
        "queued_status": None,
        "payload": command_payload,
        "dispatch": {"status": "skipped_dry_run"},
        "status": "previewed",
    }


def _split_session_window(value: str | None) -> tuple[str | None, str | None]:
    if not value or ":" not in value:
        return None, value or None
    session, window = value.split(":", 1)
    return session or None, window or None


async def _read_tmux_panes() -> dict[str, dict[str, str]]:
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{pane_id}|#{session_name}|#{window_name}|#{@INSTANCE_ID}|#{@PANE_ID}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    panes: dict[str, dict[str, str]] = {}
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split("|", 4)
        if len(parts) != 5:
            continue
        pane_id, session, window, instance_id, pane_label = parts
        if pane_id:
            panes[pane_id] = {
                "tmux_session": session,
                "tmux_window": window,
                "tmux_session_window": f"{session}:{window}",
                "instance_id": instance_id,
                "pane_label": pane_label,
            }
    return panes


async def _candidate_rows(
    db_path: Path, tmux_panes: dict[str, dict[str, str]]
) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id AS instance_id, engine, name AS tab_name, working_dir, status
            FROM instances
            WHERE status NOT IN ('stopped', 'archived')
            ORDER BY last_activity DESC
            """
        )
        rows = [dict(row) for row in await cursor.fetchall()]

    pane_by_instance = {
        meta.get("instance_id"): (pane_id, meta)
        for pane_id, meta in tmux_panes.items()
        if meta.get("instance_id")
    }
    for row in rows:
        pane_entry = pane_by_instance.get(row["instance_id"])
        if pane_entry:
            pane, pane_meta = pane_entry
            row["tmux_pane"] = pane
            row.update(pane_meta)
            continue
        # No live tmux state was available. Keep the row selectable so callers
        # using an instance-aware queue sender can defer/record a poll; the
        # production drainer will fail or defer if it cannot resolve a pane.
        row["tmux_pane"] = row["instance_id"]
    return rows


def _parse_selector(selector: str) -> list[tuple[str, str]]:
    selector = (selector or "").strip()
    if not selector:
        raise SelectorError("selector is required")
    parts = [part.strip() for part in selector.split("&") if part.strip()]
    if not parts:
        raise SelectorError("selector is required")
    parsed: list[tuple[str, str]] = []
    for part in parts:
        if part == "all":
            parsed.append(("all", ""))
        elif part.startswith("engine="):
            parsed.append(("engine", part.removeprefix("engine=").strip().lower()))
        elif part.startswith("session="):
            parsed.append(("session", part.removeprefix("session=").strip()))
        elif part.startswith("window="):
            parsed.append(("window", part.removeprefix("window=").strip()))
        elif part.startswith("tab_name~="):
            pattern = part.removeprefix("tab_name~=").strip()
            try:
                re.compile(pattern)
            except re.error as exc:
                raise SelectorError(f"invalid tab_name regex: {exc}") from exc
            parsed.append(("tab_name_regex", pattern))
        else:
            raise SelectorError(f"unsupported selector component: {part}")
    return parsed


def _row_matches(row: dict[str, Any], selector_parts: list[tuple[str, str]]) -> bool:
    for kind, value in selector_parts:
        if kind == "all":
            continue
        if kind == "engine":
            if (row.get("engine") or "").strip().lower() != value:
                return False
        elif kind == "session":
            if (row.get("tmux_session") or row.get("dispatch_target") or "") != value:
                return False
        elif kind == "window":
            if (row.get("tmux_window") or row.get("dispatch_window") or "") != value:
                return False
        elif kind == "tab_name_regex":
            if not re.search(value, row.get("tab_name") or ""):
                return False
    return True


async def record_pending_poll(
    *,
    poll_id: str,
    instance_id: str,
    selector: str,
    payload: str,
    db_path: Path = DB_PATH,
    ttl_minutes: int = POLL_TTL_MINUTES,
) -> None:
    now = datetime.now()
    expires_at = now + timedelta(minutes=ttl_minutes)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO pending_polls (
                poll_id, instance_id, selector, payload, status, created_at, expires_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                poll_id,
                instance_id,
                selector,
                payload,
                now.isoformat(),
                expires_at.isoformat(),
            ),
        )
        await db.commit()


async def broadcast_temp_message(
    selector: str,
    payload: str,
    *,
    idempotency_key: str | None = None,
    db_path: Path = DB_PATH,
    queue_sender: QueueSender | None = None,
    queue_drainer: QueueDrainer | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    selector_parts = _parse_selector(selector)
    poll_id = (idempotency_key or "").strip() or str(uuid.uuid4())
    tmux_panes = await _read_tmux_panes()
    rows = await _candidate_rows(db_path, tmux_panes)
    targets = [row for row in rows if _row_matches(row, selector_parts)]

    receipts: list[dict[str, Any]] = []
    for target in targets:
        try:
            if dry_run:
                receipt = preview_temp_message(
                    target["tmux_pane"],
                    payload,
                    target.get("engine"),
                    instance_id=target["instance_id"],
                )
            else:
                receipt = await send_temp_message(
                    target["tmux_pane"],
                    payload,
                    target.get("engine"),
                    instance_id=target["instance_id"],
                    queue_sender=queue_sender,
                    queue_drainer=queue_drainer,
                )
            receipt.update(
                {
                    "poll_id": poll_id,
                    "selector": selector,
                    "tab_name": target.get("tab_name"),
                    "tmux_session": target.get("tmux_session"),
                    "tmux_window": target.get("tmux_window"),
                }
            )
            if receipt.get("status") in {"pending", "sent"}:
                await record_pending_poll(
                    poll_id=poll_id,
                    instance_id=target["instance_id"],
                    selector=selector,
                    payload=payload,
                    db_path=db_path,
                )
            receipts.append(receipt)
        except Exception as exc:
            receipts.append(
                {
                    "poll_id": poll_id,
                    "selector": selector,
                    "instance_id": target.get("instance_id"),
                    "tmux_pane": target.get("tmux_pane"),
                    "engine": target.get("engine"),
                    "status": "failed",
                    "error": str(exc),
                }
            )
    return receipts
