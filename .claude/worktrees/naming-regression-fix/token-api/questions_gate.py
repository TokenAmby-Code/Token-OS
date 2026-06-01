"""Generic questions[] trials-clear gate for session docs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from session_doc_helpers import read_frontmatter


def _frontmatter_had_parse_failure(path: Path, body: str) -> bool:
    content = path.read_text(encoding="utf-8")
    return content.startswith("---") and body == content


def _normalize_question(entry: Any, index: int) -> dict[str, Any]:
    if isinstance(entry, dict):
        q = dict(entry)
    else:
        q = {"question": str(entry), "state": None, "importance": 0}
    q["_index"] = index
    return q


def _importance(entry: dict[str, Any]) -> int:
    try:
        return int(entry.get("importance") or 0)
    except (TypeError, ValueError):
        return 0


def trials_clear(note_path: str | Path) -> tuple[bool, list[dict]]:
    """Return (is_clear, blockers).

    is_clear: True iff `questions:` is missing, empty, or every entry has state == "closed".
    blockers: list of non-closed entries, sorted by importance descending then by original index.

    Vacuous-clear: missing key or empty array both return (True, []).
    Raises FileNotFoundError / ValueError per session_doc_helpers conventions.
    """
    path = Path(note_path)
    fm, body = read_frontmatter(path)
    if _frontmatter_had_parse_failure(path, body):
        raise ValueError(f"Malformed frontmatter: {path}")

    questions = fm.get("questions")
    if not questions:
        return True, []
    if not isinstance(questions, list):
        raise ValueError(f"questions must be a list: {path}")

    blockers = [
        _normalize_question(entry, idx)
        for idx, entry in enumerate(questions)
        if not (isinstance(entry, dict) and entry.get("state") == "closed")
    ]
    blockers.sort(key=lambda q: (-_importance(q), q.get("_index", 0)))
    return not blockers, blockers


def trials_report(note_path: str | Path) -> dict:
    """Return {clear: bool, total: int, closed: int, blockers: [...], path: str}."""
    path = Path(note_path)
    fm, body = read_frontmatter(path)
    if _frontmatter_had_parse_failure(path, body):
        raise ValueError(f"Malformed frontmatter: {path}")

    questions = fm.get("questions") or []
    if not isinstance(questions, list):
        raise ValueError(f"questions must be a list: {path}")

    clear, blockers = trials_clear(path)
    closed = sum(
        1 for entry in questions if isinstance(entry, dict) and entry.get("state") == "closed"
    )
    return {
        "clear": clear,
        "total": len(questions),
        "closed": closed,
        "blockers": blockers,
        "path": str(path),
    }


def _trunc(text: Any, limit: int = 80) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _print_human(report: dict) -> None:
    print(f"trials_clear: {str(report['clear']).lower()}")
    print(
        f"  total: {report['total']}   closed: {report['closed']}   blockers: {len(report['blockers'])}"
    )
    if report["blockers"]:
        print("\nblockers (by importance, highest first):")
        for blocker in report["blockers"]:
            imp = _importance(blocker)
            state = str(blocker.get("state") or "")
            question = _trunc(blocker.get("question"), 80)
            print(f"  [{imp:2d}] {state:<12} {question}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate session-doc questions[] trials-clear gate"
    )
    parser.add_argument("note_path")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    report = trials_report(args.note_path)
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 0 if report["clear"] else 1


if __name__ == "__main__":
    sys.exit(main())
