"""Golden Throne no-op response classification helpers.

This module is deliberately transport-free: the dispatcher stores a cheap
worktree fingerprint before firing a GT ping, and the Stop hook compares that
snapshot with the post-response transcript/fingerprint to decide whether the
agent did work or merely spent another billed turn saying nothing changed.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

NO_OP_THRESHOLD = 3
MAX_SUMMARIES = 3
SUMMARY_CHARS = 240


@dataclass(frozen=True)
class GTResponseClassification:
    outcome: str  # active | no_op | victory_declare
    summary: str
    has_tool_calls: bool
    has_delta: bool
    reason: str


def _snippet(text: str, limit: int = SUMMARY_CHARS) -> str:
    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return ""
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _json_records_from_tail(transcript_tail: str | None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in (transcript_tail or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except Exception:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def load_transcript_records(
    transcript_tail: str | None = None,
    transcript_path: str | None = None,
    *,
    max_lines: int = 200,
) -> list[dict[str, Any]]:
    records = _json_records_from_tail(transcript_tail)
    if records or not transcript_path:
        return records
    try:
        path = Path(transcript_path)
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
    except Exception:
        return []
    return _json_records_from_tail("\n".join(lines))


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _tool_name(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("name", "tool_name", "tool", "function"):
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        if isinstance(raw, dict):
            nested = raw.get("name")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def has_tool_calls(records: list[dict[str, Any]]) -> bool:
    for record in records:
        for node in _walk(record):
            if not isinstance(node, dict):
                continue
            node_type = str(node.get("type") or "").lower()
            if node_type in {"tool_use", "tool_call", "function_call"}:
                return True
            if node.get("tool_use_id") or node.get("tool_call_id"):
                return True
            if node.get("tool_name"):
                return True
    return False


def declares_victory(records: list[dict[str, Any]]) -> bool:
    needles = ("victory_declare", "declare_victory")
    for record in records:
        for node in _walk(record):
            if isinstance(node, dict):
                name = _tool_name(node).lower().replace("-", "_")
                if any(needle in name for needle in needles):
                    return True
            elif isinstance(node, str):
                lowered = node.lower().replace("-", "_")
                if any(needle in lowered for needle in needles):
                    return True
    return False


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def last_assistant_summary(records: list[dict[str, Any]]) -> str:
    for record in reversed(records):
        message = record.get("message") if isinstance(record.get("message"), dict) else record
        role = message.get("role") or record.get("role")
        if role != "assistant":
            continue
        text = _content_text(message.get("content"))
        if text.strip():
            return _snippet(text)
    return "[no assistant text]"


def append_summary(existing_json: str | None, summary: str) -> list[str]:
    try:
        current = json.loads(existing_json or "[]")
        if not isinstance(current, list):
            current = []
    except Exception:
        current = []
    current.append(_snippet(summary) or "[empty]")
    return [str(item) for item in current[-MAX_SUMMARIES:]]


def worktree_fingerprint(working_dir: str | None) -> str | None:
    """Return a cheap git worktree fingerprint, or None when unavailable.

    The fingerprint includes HEAD and porcelain status (tracked modifications +
    untracked files). It intentionally does not shell through a login shell and
    has a short timeout so hooks cannot block on a bad filesystem.
    """
    if not working_dir:
        return None
    try:
        cwd = Path(working_dir).expanduser()
        if not cwd.exists() or not cwd.is_dir():
            return None
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
        if root.returncode != 0:
            return None
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
        if head.returncode != 0 or status.returncode != 0:
            return None
        digest = hashlib.sha256()
        digest.update(root.stdout.strip())
        digest.update(b"\0")
        digest.update(head.stdout.strip())
        digest.update(b"\0")
        digest.update(status.stdout)
        return digest.hexdigest()
    except Exception:
        return None


def classify_gt_response(
    *,
    transcript_tail: str | None = None,
    transcript_path: str | None = None,
    prior_fingerprint: str | None = None,
    current_fingerprint: str | None = None,
) -> GTResponseClassification:
    records = load_transcript_records(transcript_tail, transcript_path)
    summary = last_assistant_summary(records)
    if declares_victory(records):
        return GTResponseClassification("victory_declare", summary, False, False, "victory_declare")
    tools = has_tool_calls(records)
    delta = bool(
        prior_fingerprint and current_fingerprint and prior_fingerprint != current_fingerprint
    )
    if tools:
        return GTResponseClassification("active", summary, True, delta, "tool_calls")
    if delta:
        return GTResponseClassification("active", summary, False, True, "worktree_delta")
    return GTResponseClassification("no_op", summary, False, False, "text_only_no_delta")
