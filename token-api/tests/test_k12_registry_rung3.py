#!/usr/bin/env python3
"""k12-era registry rung 3 (satellite live) checks.

Covers: the box-conditional shed of stop_hook's direct-sqlite fallback —
k12 boxes enqueue the failed write-door intent into the durable retry outbox
(real subprocess, real outbox sqlite) and never touch agents.db directly;
the Mac keeps the pre-rung-3 direct-sqlite fallback (regression pin).

Run directly: uv run --directory token-api python tests/test_k12_registry_rung3.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Isolate every module-level DB path + point the API at a dead port BEFORE any
# token-api import (shared.py resolves both at import time).
_TMP = Path(tempfile.mkdtemp(prefix="k12-rung3-"))
_DB = _TMP / "agents.db"
_OUTBOX_DB = _TMP / "outbox.sqlite3"
os.environ["TOKEN_API_AGENTS_DB"] = str(_DB)
os.environ["TOKEN_API_URL"] = "http://localhost:1"
os.environ["GENERIC_TOKEN_API_DURABLE_RETRY_OUTBOX_DB"] = str(_OUTBOX_DB)
os.environ["GENERIC_TOKEN_API_DURABLE_RETRY_OUTBOX_LOG"] = str(_TMP / "outbox.log")

import db_schema  # noqa: E402

db_schema.init_database_sync(_DB)

import stop_hook  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        FAILURES.append(name)


def _seed_instance(instance_id: str, **overrides) -> None:
    fields = {"id": instance_id, "device_id": "test-box", "status": "working"}
    fields.update(overrides)
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    conn = sqlite3.connect(_DB)
    conn.execute(f"INSERT INTO instances ({cols}) VALUES ({marks})", tuple(fields.values()))
    conn.commit()
    conn.close()


def _instance_status(instance_id: str) -> str | None:
    conn = sqlite3.connect(_DB)
    row = conn.execute("SELECT status FROM instances WHERE id = ?", (instance_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def _outbox_rows() -> list[dict]:
    if not _OUTBOX_DB.exists():
        return []
    conn = sqlite3.connect(_OUTBOX_DB)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM hook_posts ORDER BY id")]
    conn.close()
    return rows


def _fresh_machine_detection(machine: str) -> bool:
    """Re-run _sheds_sqlite_fallback with IMPERIUM_MACHINE forced to `machine`."""
    os.environ["IMPERIUM_MACHINE"] = machine
    sys.modules.pop("imperium_config", None)
    try:
        return stop_hook._sheds_sqlite_fallback()
    finally:
        os.environ.pop("IMPERIUM_MACHINE", None)
        sys.modules.pop("imperium_config", None)


def test_shed_set_detection() -> None:
    print("shed-set machine detection:")
    check("k12-personal sheds the sqlite fallback", _fresh_machine_detection("k12-personal"))
    check(
        "k12-work sheds the sqlite fallback (R1: never grows a DB)",
        _fresh_machine_detection("k12-work"),
    )
    check("mac keeps the sqlite fallback", not _fresh_machine_detection("mac"))
    check("wsl keeps the sqlite fallback", not _fresh_machine_detection("wsl"))


def test_hub_box_enqueues_outbox_not_sqlite() -> None:
    print("k12 box + dead API → durable outbox, agents.db untouched:")
    _seed_instance("rung3-shed-box", origin_type="cron")
    os.environ["IMPERIUM_MACHINE"] = "k12-personal"
    sys.modules.pop("imperium_config", None)
    try:
        stop_hook.mark_cron_instance_stopped("rung3-shed-box")
        stop_hook.clear_human_anchor_on_stop("rung3-shed-box")
    finally:
        os.environ.pop("IMPERIUM_MACHINE", None)
        sys.modules.pop("imperium_config", None)

    check(
        "instance row NOT mutated by direct sqlite",
        _instance_status("rung3-shed-box") == "working",
        f"status={_instance_status('rung3-shed-box')}",
    )
    rows = _outbox_rows()
    actions = sorted(r["action_type"] for r in rows)
    check(
        "both intents enqueued to the outbox",
        actions == ["clear-human-anchor", "instance-stopped"],
        f"actions={actions}",
    )
    for r in rows:
        check(
            f"outbox row {r['action_type']} is pending with door URL",
            r["status"] == "pending" and r["url"].endswith(f"/api/hooks/{r['action_type']}"),
            f"status={r['status']} url={r['url']}",
        )
        payload = json.loads(r["payload"])
        check(
            f"outbox row {r['action_type']} carries the instance id",
            payload.get("instance_id") == "rung3-shed-box",
            f"payload={r['payload']}",
        )


def test_mac_keeps_sqlite_fallback() -> None:
    print("mac + dead API → direct sqlite fallback still lands (regression):")
    _seed_instance("rung3-mac-fallback", origin_type="cron")
    outbox_before = len(_outbox_rows())
    os.environ["IMPERIUM_MACHINE"] = "mac"
    sys.modules.pop("imperium_config", None)
    try:
        stop_hook.mark_cron_instance_stopped("rung3-mac-fallback")
    finally:
        os.environ.pop("IMPERIUM_MACHINE", None)
        sys.modules.pop("imperium_config", None)

    check(
        "instance row stopped via direct sqlite",
        _instance_status("rung3-mac-fallback") == "stopped",
        f"status={_instance_status('rung3-mac-fallback')}",
    )
    check(
        "no new outbox rows on the mac path",
        len(_outbox_rows()) == outbox_before,
        f"rows={len(_outbox_rows())} before={outbox_before}",
    )


def main() -> int:
    test_shed_set_detection()
    test_hub_box_enqueues_outbox_not_sqlite()
    test_mac_keeps_sqlite_fallback()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("\nall rung-3 checks green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
