"""Inter-persona communication primitives — `talk` (two-way) and `brief` (one-shot).

Trinity Chunk 1 (see Mars/Tasks/trinity-chunk-1-talk-cli.md and
Terra/Ultramar/Inter-Persona Communication.md).

Two CLI primitives talk to this module via Token-API endpoints in main.py:

* ``talk --pane <Y> "ping"``  →  POST /api/talk/send (blocks via /await long-poll)
* ``brief --pane <Y> "FYI"``  →  POST /api/brief/send (fire-and-forget)

State is held in-memory per the Inter-Persona Comm spec ("ephemeral first; persist
if needed"). Slash-copy of a target's final response is performed by reading the
target's Claude Code JSONL transcript at stop-hook time and concatenating the
assistant text blocks emitted after the talk payload was injected.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from pane_surface import RAW_TMUX_PANE_RX
from shared import DB_PATH, instance_id_for_pane

# --- in-memory state ----------------------------------------------------------

# talk_id -> dict (see _new_talk for shape)
_TALKS: dict[str, dict[str, Any]] = {}
# (caller_pane, target_pane) -> talk_id for the currently-open pair
_PAIR_INDEX: dict[tuple[str, str], str] = {}
# target_pane -> list of talk_ids waiting on this pane's natural stop
_TARGET_INDEX: dict[str, list[str]] = {}
_LOCK = asyncio.Lock()

TALK_OPEN = "open"
TALK_RETURNED = "returned"
TALK_EXPIRED = "expired"
TALK_CANCELLED = "cancelled"

DEFAULT_TALK_TIMEOUT = 600  # seconds


def _now_iso() -> str:
    return datetime.now().isoformat()


def _normalize_pane(value: str | None) -> str:
    return (value or "").strip()


_PUBLIC_PANE_ID_RX = re.compile(r"^[^:%\s]+:[^:%\s]+$")


def _is_public_pane_id(value: str | None) -> bool:
    normalized = _normalize_pane(value)
    return bool(normalized and _PUBLIC_PANE_ID_RX.fullmatch(normalized))


async def _tmux_list_panes() -> list[dict[str, str]]:
    """Return all tmux panes with pane_id + @PANE_ID (the position id)."""
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#D|#{@PANE_ID}|#{session_name}|#{window_index}|#{window_name}",
            ],
            env={**os.environ, "IMPERIUM_TMUX_RAW": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    rows: list[dict[str, str]] = []
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split("|", 4)
        if len(parts) != 5:
            continue
        pane_id, position_id, session, window_index, window_name = parts
        if not pane_id:
            continue
        rows.append(
            {
                "pane_id": pane_id,
                "position_id": position_id,
                "session": session,
                "window_index": window_index,
                "window_name": window_name,
            }
        )
    return rows


async def _public_pane_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for row in await _tmux_list_panes():
        pane_id = _normalize_pane(row.get("pane_id"))
        position_id = _normalize_pane(row.get("position_id"))
        if pane_id.startswith("%") and _is_public_pane_id(position_id):
            mapping[pane_id] = position_id
    return mapping


def _public_pane_id(pane_id: str | None, mapping: dict[str, str]) -> str:
    value = _normalize_pane(pane_id)
    if not value:
        return ""
    if value.startswith("%"):
        return mapping.get(value, "unresolved")
    if _is_public_pane_id(value):
        return value
    sanitized = RAW_TMUX_PANE_RX.sub("unresolved", value)
    return sanitized if sanitized != value else "unresolved"


def publicize_pane_payload(payload: Any, mapping: dict[str, str]) -> Any:
    """Return a public copy of an API payload with no raw ``%NNN`` ids."""
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for key, value in payload.items():
            if key in {"pane_id", "tmux_pane", "target_pane", "caller_pane", "returned_by_pane"}:
                if value is None:
                    out[key] = None
                elif isinstance(value, str):
                    out[key] = _public_pane_id(value, mapping)
                else:
                    out[key] = value
            else:
                out[key] = publicize_pane_payload(value, mapping)
        return out
    if isinstance(payload, list):
        return [publicize_pane_payload(item, mapping) for item in payload]
    if isinstance(payload, str):
        return RAW_TMUX_PANE_RX.sub(lambda m: mapping.get(m.group(0), "unresolved"), payload)
    return payload


async def publicize_payload(payload: Any) -> Any:
    return publicize_pane_payload(payload, await _public_pane_map())


async def resolve_pane(identifier: str) -> str | None:
    """Resolve a public pane identifier to an internal raw tmux pane id.

    Accepts:
      * position ids (``somnium:SE``)
      * fully qualified ``session:window:position`` (``main:2:SE`` → ``somnium:SE``)
    """
    raw = _normalize_pane(identifier)
    if not raw:
        return None
    if raw.startswith("%"):
        return raw
    panes = await _tmux_list_panes()

    # main:2:SE -> match by session+window_index+position_id-suffix
    if raw.count(":") == 2:
        session, window_index, pos = raw.split(":", 2)
        for p in panes:
            if (
                p["session"] == session
                and p["window_index"] == window_index
                and p["position_id"].endswith(f":{pos}")
            ):
                return p["pane_id"]

    for p in panes:
        if p["position_id"] == raw:
            return p["pane_id"]
    return None


async def lookup_instance_for_pane(pane_id: str) -> dict[str, Any] | None:
    instance_id = await instance_id_for_pane(pane_id)
    if not instance_id:
        return await _rowless_live_instance_for_pane(pane_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, working_dir, engine, name AS tab_name, status, last_activity
            FROM instances
            WHERE id = ?
              AND status NOT IN ('archived')
            ORDER BY last_activity DESC
            LIMIT 1
            """,
            (instance_id,),
        )
        row = await cursor.fetchone()
    if not row:
        return None
    result = dict(row)
    result["tmux_pane"] = pane_id
    for pane in await _tmux_list_panes():
        if pane.get("pane_id") == pane_id and pane.get("position_id"):
            result["pane_label"] = pane["position_id"]
            break
    return result


async def _resolve_agent_for_pane(pane_id: str) -> str | None:
    """Return claude/codex for a live rowless pane via tmuxctl's ps detector."""
    cli_lib = Path(__file__).resolve().parents[1] / "cli-tools" / "lib"
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "tmuxctl.cli",
                "resolve-agent",
                "--pane",
                pane_id,
                "--agent",
                "auto",
                "--default",
                "auto",
            ],
            env={
                **os.environ,
                "PYTHONPATH": f"{cli_lib}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    engine = (proc.stdout or "").strip().lower()
    return engine if engine in {"claude", "codex"} else None


async def _rowless_live_instance_for_pane(pane_id: str) -> dict[str, Any] | None:
    engine = await _resolve_agent_for_pane(pane_id)
    if not engine:
        return None
    result: dict[str, Any] = {
        "id": None,
        "working_dir": None,
        "engine": engine,
        "tab_name": None,
        "status": "live_rowless",
        "last_activity": None,
        "tmux_pane": pane_id,
        "rowless_live": True,
    }
    for pane in await _tmux_list_panes():
        if pane.get("pane_id") == pane_id and pane.get("position_id"):
            result["pane_label"] = pane["position_id"]
            break
    return result


# --- talk pair lifecycle ------------------------------------------------------


def _pair_key(caller: str, target: str) -> tuple[str, str]:
    return (caller, target)


async def register_talk(
    *,
    caller_pane: str,
    target_pane: str,
    payload: str,
    target_instance: dict[str, Any] | None,
    engine: str | None = None,
) -> dict[str, Any]:
    talk_id = str(uuid.uuid4())
    event = asyncio.Event()
    now = _now_iso()
    resolved_engine = (
        engine or (target_instance.get("engine") if target_instance else None) or "claude"
    )
    record = {
        "talk_id": talk_id,
        "caller_pane": caller_pane,
        "target_pane": target_pane,
        "target_instance_id": target_instance.get("id") if target_instance else None,
        "target_working_dir": target_instance.get("working_dir") if target_instance else None,
        "target_engine": resolved_engine,
        "payload": payload,
        "payload_sent_at": time.time(),
        "payload_sent_iso": now,
        "status": TALK_OPEN,
        "turn": "target",
        "result_text": None,
        "result_kind": None,
        "returned_by_pane": None,
        "event": event,
        "created_at": now,
        "updated_at": now,
    }
    async with _LOCK:
        _TALKS[talk_id] = record
        _PAIR_INDEX[_pair_key(caller_pane, target_pane)] = talk_id
        _TARGET_INDEX.setdefault(target_pane, []).append(talk_id)
    return record


async def cancel_talk(talk_id: str, reason: str = "cancelled") -> dict[str, Any] | None:
    async with _LOCK:
        record = _TALKS.get(talk_id)
        if not record or record["status"] != TALK_OPEN:
            return None
        record["status"] = TALK_CANCELLED
        record["result_kind"] = reason
        record["updated_at"] = _now_iso()
        _PAIR_INDEX.pop(_pair_key(record["caller_pane"], record["target_pane"]), None)
        target_list = _TARGET_INDEX.get(record["target_pane"], [])
        if talk_id in target_list:
            target_list.remove(talk_id)
        event: asyncio.Event = record["event"]
    event.set()
    return record


def _public_view(record: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in record.items() if k != "event"}
    return out


async def get_talk(talk_id: str) -> dict[str, Any] | None:
    record = _TALKS.get(talk_id)
    if not record:
        return None
    return _public_view(record)


async def await_talk(talk_id: str, *, timeout: float) -> dict[str, Any] | None:
    record = _TALKS.get(talk_id)
    if not record:
        return None
    if record["status"] != TALK_OPEN:
        return _public_view(record)
    event: asyncio.Event = record["event"]
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except TimeoutError:
        return _public_view(record)
    return _public_view(record)


async def return_talk(
    *,
    caller_pane: str,
    target_pane: str,
    payload: str,
) -> dict[str, Any] | None:
    """Explicit return: target_pane calls ``talk --pane caller "..."``.

    Looks up the open pair where ``caller_pane`` is the caller and
    ``target_pane`` is the turn-holder. Caller passes its OWN pane as
    ``target_pane`` of this call (the new "target" — the original caller).

    So when B returns to A:
      * A's ``talk`` registered: caller_pane=A, target_pane=B (turn on B).
      * B's ``talk --pane A``  triggers ``register_talk`` with
        caller=B, target=A — but BEFORE registering, we check whether
        the SWAPPED pair (caller=A, target=B) is open. If so, this is a
        return, not a new outbound talk.
    """
    async with _LOCK:
        # B is calling with caller=B, target=A. The original pair is keyed
        # (A, B): caller=A, target=B.
        original_key = _pair_key(target_pane, caller_pane)
        talk_id = _PAIR_INDEX.get(original_key)
        if not talk_id:
            return None
        record = _TALKS.get(talk_id)
        if not record or record["status"] != TALK_OPEN:
            return None
        record["status"] = TALK_RETURNED
        record["result_text"] = payload
        record["result_kind"] = "explicit"
        record["returned_by_pane"] = caller_pane
        record["updated_at"] = _now_iso()
        _PAIR_INDEX.pop(original_key, None)
        target_list = _TARGET_INDEX.get(record["target_pane"], [])
        if talk_id in target_list:
            target_list.remove(talk_id)
        event: asyncio.Event = record["event"]
    event.set()
    return _public_view(record)


# --- slash-copy ---------------------------------------------------------------


def _project_dir_for_path(working_dir: str | None) -> str | None:
    if not working_dir:
        return None
    return working_dir.replace("/", "-")


def _codex_jsonl_path(session_id: str) -> Path | None:
    """Find a codex rollout JSONL for ``session_id``.

    Codex stores transcripts at
    ``~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ISO_TS>-<session_uuid>.jsonl``
    (UTC dates). The session_id is the trailing UUID in the filename. We bound
    the disk walk to today + yesterday UTC because slash-copy fires within
    seconds of the assistant turn ending, so the rollout file must have been
    created very recently — at most across a date boundary.
    """
    if not session_id:
        return None
    base = Path.home() / ".codex" / "sessions"
    if not base.exists():
        return None
    today = datetime.utcnow()
    yesterday_ts = today.timestamp() - 86400
    yesterday = datetime.utcfromtimestamp(yesterday_ts)
    for day in (today, yesterday):
        day_dir = base / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
        if not day_dir.exists():
            continue
        matches = list(day_dir.glob(f"rollout-*{session_id}*.jsonl"))
        if matches:
            return matches[0]
    return None


def _claude_jsonl_path(session_id: str, working_dir: str | None) -> Path | None:
    if not session_id:
        return None
    base = Path.home() / ".claude" / "projects"
    project_dir = _project_dir_for_path(working_dir)
    if project_dir:
        candidate = base / project_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
    # Fall back to glob across all projects.
    matches = list(base.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def _parse_ts(value: Any) -> float | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _extract_assistant_text_after(path: Path, since_ts: float) -> str:
    """Return the final assistant text emitted at or after ``since_ts``.

    Strategy: walk the JSONL, group consecutive assistant entries into runs,
    keep only runs whose first timestamp is >= since_ts, return the LAST such
    run's concatenated text. This isolates the response to the most recent
    injected prompt.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""

    runs: list[list[str]] = []
    current: list[str] = []
    current_ts: float | None = None
    last_role: str | None = None

    def flush() -> None:
        nonlocal current, current_ts, last_role
        if current and current_ts is not None and current_ts >= since_ts:
            runs.append(current)
        current = []
        current_ts = None
        last_role = None

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg = event.get("message") or {}
        role = msg.get("role") or event.get("role")
        if role != "assistant":
            # Boundary: any non-assistant event closes the current run.
            if last_role == "assistant":
                flush()
            last_role = role
            continue

        ts = _parse_ts(event.get("timestamp"))
        content = msg.get("content")
        text_parts: list[str] = []
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_value = block.get("text", "")
                    if isinstance(text_value, str) and text_value.strip():
                        text_parts.append(text_value)

        if not text_parts:
            last_role = "assistant"
            continue

        if last_role != "assistant" or current_ts is None:
            current_ts = ts
        current.extend(text_parts)
        last_role = "assistant"

    if last_role == "assistant":
        flush()

    if not runs:
        return ""
    return "\n\n".join(runs[-1]).strip()


def _extract_codex_assistant_text_after(path: Path, since_ts: float) -> str:
    """Return the codex target's final assistant text at/after ``since_ts``.

    Codex rollouts have a flat event schema: each line is
    ``{"timestamp", "type": "response_item", "payload": {...}}``. Assistant
    text lives in ``payload.type=="message"`` with ``role=="assistant"`` and
    content blocks of ``type=="output_text"``. Non-message payloads
    (``reasoning``, ``function_call``, ``function_call_output``) interleave
    with assistant messages within a turn; treat them as run boundaries so the
    final assistant message of the most recent turn is isolated, mirroring
    the Claude extractor strategy.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""

    runs: list[list[str]] = []
    current: list[str] = []
    current_ts: float | None = None
    in_assistant_run = False

    def flush() -> None:
        nonlocal current, current_ts, in_assistant_run
        if current and current_ts is not None and current_ts >= since_ts:
            runs.append(current)
        current = []
        current_ts = None
        in_assistant_run = False

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "response_item":
            if in_assistant_run:
                flush()
            continue
        payload = event.get("payload") or {}
        is_assistant_msg = payload.get("type") == "message" and payload.get("role") == "assistant"
        if not is_assistant_msg:
            if in_assistant_run:
                flush()
            continue

        ts = _parse_ts(event.get("timestamp"))
        content = payload.get("content")
        text_parts: list[str] = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "output_text":
                    text_value = block.get("text", "")
                    if isinstance(text_value, str) and text_value.strip():
                        text_parts.append(text_value)
        if not text_parts:
            continue

        if not in_assistant_run or current_ts is None:
            current_ts = ts
        current.extend(text_parts)
        in_assistant_run = True

    if in_assistant_run:
        flush()

    if not runs:
        return ""
    return "\n\n".join(runs[-1]).strip()


def _capture_pane_fallback(pane_id: str) -> str:
    """Last-resort: capture visible pane content for slash-copy."""
    try:
        proc = subprocess.run(
            ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-200"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="replace").rstrip()


async def slash_copy_target(
    record: dict[str, Any],
    *,
    transcript_path: str | None = None,
) -> str:
    """Extract the target's final assistant response for slash-copy.

    Engine-aware. For ``claude`` targets, prefer the Stop hook's
    ``transcript_path`` (authoritative), fall back to project-dir resolution
    by session_id, then a cross-project glob. For ``codex`` targets, resolve
    the rollout file under ``~/.codex/sessions/`` by session_id (the Stop
    hook payload doesn't carry transcript_path for codex today). Final
    fallback for either engine is ``tmux capture-pane``.
    """
    since_ts = float(record.get("payload_sent_at") or 0)
    engine = (record.get("target_engine") or "claude").lower()
    path: Path | None = None
    extractor = _extract_assistant_text_after

    if engine == "codex":
        extractor = _extract_codex_assistant_text_after
        session_id = record.get("target_instance_id")
        path = await asyncio.to_thread(_codex_jsonl_path, session_id or "")
    else:
        if transcript_path:
            try:
                candidate = Path(transcript_path)
                if candidate.exists():
                    path = candidate
            except (OSError, ValueError):
                path = None
        if path is None:
            session_id = record.get("target_instance_id")
            working_dir = record.get("target_working_dir")
            path = await asyncio.to_thread(_claude_jsonl_path, session_id or "", working_dir)

    text = ""
    if path:
        # The Stop hook can fire before the assistant turn is flushed to the
        # JSONL transcript (same race on claude and codex). Retry briefly.
        for _attempt in range(8):
            text = await asyncio.to_thread(extractor, path, since_ts)
            if text:
                break
            await asyncio.sleep(0.25)
    if not text:
        text = await asyncio.to_thread(_capture_pane_fallback, record.get("target_pane") or "")
    return text


async def resolve_open_talks_for_target(target_pane: str) -> list[dict[str, Any]]:
    """Look up open talk pairs awaiting natural-stop slash-copy."""
    async with _LOCK:
        ids = list(_TARGET_INDEX.get(target_pane, []))
    candidates: list[dict[str, Any]] = []
    for talk_id in ids:
        record = _TALKS.get(talk_id)
        if record and record["status"] == TALK_OPEN and record["turn"] == "target":
            candidates.append(record)
    return candidates


async def fire_slash_copy_for_pane(
    target_pane: str,
    *,
    transcript_path: str | None = None,
) -> list[dict[str, Any]]:
    """Called on activity=stop for ``target_pane``. Slash-copies + resolves.

    If the caller (Stop hook) has the Claude transcript_path in hand, pass it
    through — it pins us to the exact JSONL of the session that just stopped.
    """
    candidates = await resolve_open_talks_for_target(target_pane)
    resolved: list[dict[str, Any]] = []
    for record in candidates:
        # Re-check status under lock and capture event for waking.
        async with _LOCK:
            current = _TALKS.get(record["talk_id"])
            if not current or current["status"] != TALK_OPEN:
                continue
        text = await slash_copy_target(record, transcript_path=transcript_path)
        async with _LOCK:
            current = _TALKS.get(record["talk_id"])
            if not current or current["status"] != TALK_OPEN:
                continue
            current["status"] = TALK_RETURNED
            current["result_text"] = text
            current["result_kind"] = "slash_copy"
            current["returned_by_pane"] = current["target_pane"]
            current["updated_at"] = _now_iso()
            _PAIR_INDEX.pop(_pair_key(current["caller_pane"], current["target_pane"]), None)
            target_list = _TARGET_INDEX.get(current["target_pane"], [])
            if current["talk_id"] in target_list:
                target_list.remove(current["talk_id"])
            event: asyncio.Event = current["event"]
        event.set()
        resolved.append(_public_view(current))
    return resolved


# --- brief --------------------------------------------------------------------


PAGE_WINDOW_NAMES = {
    "palace": "palace",
    "somnium": "somnium",
    "legion": "legion",
    "mechanicus": "mechanicus",
}


async def resolve_brief_targets(
    *,
    panes: list[str] | None,
    pages: list[str] | None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Resolve --pane and --page selectors into a deduped target list.

    Returns ``(resolved_targets, unresolved_specs)``. Each resolved target is
    a dict with ``pane_id`` plus context (``source``: pane|page, ``spec``).
    """
    seen: set[str] = set()
    resolved: list[dict[str, str]] = []
    unresolved: list[dict[str, str]] = []

    tmux_panes = await _tmux_list_panes()
    panes_by_id: dict[str, dict[str, str]] = {p["pane_id"]: p for p in tmux_panes}

    for spec in panes or []:
        raw = _normalize_pane(spec)
        if not raw:
            continue
        pane_id = await resolve_pane(raw)
        if not pane_id:
            unresolved.append({"source": "pane", "spec": raw, "reason": "no_match"})
            continue
        if pane_id in seen:
            continue
        seen.add(pane_id)
        meta = panes_by_id.get(pane_id, {})
        resolved.append(
            {
                "pane_id": pane_id,
                "position_id": meta.get("position_id", ""),
                "source": "pane",
                "spec": raw,
            }
        )

    for page in pages or []:
        raw = (page or "").strip().lower()
        if not raw:
            continue
        matches: list[dict[str, str]] = []
        if raw == "all":
            matches = tmux_panes
        elif raw in PAGE_WINDOW_NAMES:
            wanted = PAGE_WINDOW_NAMES[raw]
            matches = [p for p in tmux_panes if p["window_name"].rstrip("-") == wanted]
        else:
            try:
                idx = str(int(raw))
                matches = [p for p in tmux_panes if p["window_index"] == idx]
            except ValueError:
                pass
        if not matches:
            unresolved.append({"source": "page", "spec": raw, "reason": "no_match"})
            continue
        for p in matches:
            pane_id = p["pane_id"]
            if pane_id in seen:
                continue
            seen.add(pane_id)
            resolved.append(
                {
                    "pane_id": pane_id,
                    "position_id": p["position_id"],
                    "source": "page",
                    "spec": raw,
                }
            )

    return resolved, unresolved
