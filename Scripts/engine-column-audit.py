#!/usr/bin/env python3
"""One-shot audit for instances.engine population on live panes.

Canonical engine values are exactly: 'claude', 'codex'. Anything else is a bug.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

CANONICAL_ENGINES = {"claude", "codex"}


def _tmux_current_command(pane: str | None) -> tuple[str | None, bool]:
    if not pane:
        return None, False
    try:
        proc = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{pane_current_command}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except Exception:
        return None, False
    if proc.returncode != 0:
        return None, False
    return proc.stdout.strip(), True


def _infer_engine(command: str | None) -> str | None:
    cmd = (command or "").strip().lower()
    if cmd in CANONICAL_ENGINES:
        return cmd
    if "claude" in cmd:
        return "claude"
    if "codex" in cmd:
        return "codex"
    return None


def audit(db_path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in conn.execute(
            "SELECT id, name AS tab_name, status, engine, tmux_pane, last_activity FROM instances"
        )]
    finally:
        conn.close()

    non_stopped = [
        row for row in rows if (row.get("status") or "") not in {"stopped", "archived"}
    ]
    null_all = [row for row in rows if not (row.get("engine") or "").strip()]
    populated_all = [row for row in rows if (row.get("engine") or "").strip()]
    null_non_stopped = [row for row in non_stopped if not (row.get("engine") or "").strip()]
    populated_non_stopped = [row for row in non_stopped if (row.get("engine") or "").strip()]

    live_rows: list[dict[str, Any]] = []
    null_live: list[dict[str, Any]] = []
    for row in non_stopped:
        command, pane_exists = _tmux_current_command(row.get("tmux_pane"))
        if not pane_exists:
            continue
        annotated = dict(row)
        annotated["pane_current_command"] = command
        annotated["inferred_engine"] = _infer_engine(command)
        live_rows.append(annotated)
        if not (row.get("engine") or "").strip():
            null_live.append(annotated)

    engine_counts = Counter((row.get("engine") or "<NULL>").strip() or "<NULL>" for row in rows)
    status_counts = Counter(row.get("status") or "<NULL>" for row in rows)
    invalid_engine_counts = {
        key: value
        for key, value in engine_counts.items()
        if key != "<NULL>" and key not in CANONICAL_ENGINES
    }
    live_null_percent = (len(null_live) / len(live_rows) * 100) if live_rows else 0.0

    return {
        "db": str(db_path),
        "canonical_engine_values": sorted(CANONICAL_ENGINES),
        "total_rows": len(rows),
        "all_engine_null": len(null_all),
        "all_engine_populated": len(populated_all),
        "non_stopped_rows": len(non_stopped),
        "non_stopped_engine_null": len(null_non_stopped),
        "non_stopped_engine_populated": len(populated_non_stopped),
        "live_tmux_panes_non_stopped": len(live_rows),
        "live_engine_null": len(null_live),
        "live_engine_populated": len(live_rows) - len(null_live),
        "live_engine_null_percent": round(live_null_percent, 2),
        "migration_needed": live_null_percent > 5.0,
        "engine_counts": dict(engine_counts),
        "invalid_engine_counts": invalid_engine_counts,
        "status_counts": dict(status_counts),
        "null_live_inferences": [
            {
                "id": row.get("id"),
                "tab_name": row.get("tab_name"),
                "status": row.get("status"),
                "tmux_pane": row.get("tmux_pane"),
                "pane_current_command": row.get("pane_current_command"),
                "inferred_engine": row.get("inferred_engine"),
            }
            for row in null_live
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get("TOKEN_API_DB", str(Path.home() / ".claude" / "agents.db")),
        help="Path to agents.db (default: TOKEN_API_DB or ~/.claude/agents.db)",
    )
    args = parser.parse_args()
    print(json.dumps(audit(Path(args.db).expanduser()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
