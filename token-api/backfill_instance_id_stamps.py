#!/usr/bin/env python3
"""One-shot: stamp @INSTANCE_ID on live agent panes from the existing tmux_pane column.

Phase 2 of the "tmuxctl owns instance_id -> pane resolution" migration. Run once
on each host, while the columns still exist, so already-running agents become
resolvable by `tmuxctl resolve-instance` without waiting for them to re-register.

Idempotent and fail-soft: only stamps live, local, non-stopped instances whose
pane still exists; skips remote-hosted panes (their tmux server is elsewhere) and
panes that have vanished. Run with --dry-run to preview.

Usage:
    python3 backfill_instance_id_stamps.py [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

DB_PATH = Path(os.environ.get("TOKEN_API_DB", Path.home() / ".claude" / "agents.db"))


def _local_device_name() -> str:
    """Best-effort local device name (matches token-api's cfg('device_name'))."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli-tools" / "lib"))
        from imperium_config import imperium_cfg  # type: ignore

        return str(imperium_cfg("device_name") or "")
    except Exception:
        return ""


def _pane_exists(pane: str) -> bool:
    try:
        proc = subprocess.run(
            ["tmux", "display-message", "-t", pane, "-p", "#{pane_id}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return proc.returncode == 0 and proc.stdout.strip().startswith("%")
    except Exception:
        return False


def _stamp(pane: str, instance_id: str) -> bool:
    try:
        proc = subprocess.run(
            ["tmux", "set-option", "-p", "-t", pane, "@INSTANCE_ID", instance_id],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return proc.returncode == 0
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview without stamping")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}", file=sys.stderr)
        return 1

    local = _local_device_name()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, tmux_pane, device_id, status
        FROM claude_instances
        WHERE COALESCE(tmux_pane, '') != ''
          AND COALESCE(status, '') != 'stopped'
        ORDER BY last_activity DESC
        """
    ).fetchall()
    conn.close()

    stamped = skipped_remote = skipped_dead = 0
    for row in rows:
        pane = row["tmux_pane"]
        instance_id = row["id"]
        device_id = row["device_id"] or ""
        if local and device_id and device_id != local:
            skipped_remote += 1
            continue
        if not _pane_exists(pane):
            skipped_dead += 1
            continue
        if args.dry_run:
            print(f"WOULD stamp {pane} <- {instance_id}")
            stamped += 1
            continue
        if _stamp(pane, instance_id):
            print(f"stamped {pane} <- {instance_id}")
            stamped += 1
        else:
            skipped_dead += 1

    verb = "would stamp" if args.dry_run else "stamped"
    print(
        f"\n{verb} {stamped}; skipped {skipped_remote} remote-hosted, "
        f"{skipped_dead} vanished/failed (of {len(rows)} live candidates)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
