#!/usr/bin/env python3
"""k12-era registry rung 2 (R6 write door) checks.

Covers: the /api/hooks/instance-stopped and /api/hooks/clear-human-anchor
write-door endpoints (empty-id guard, not-found shape, seeded-row mutation
with API actor attribution, where-guard idempotency), and stop_hook's
API-first-with-sqlite-fallback selection (dead API port → direct sqlite
still lands the write with the direct actor).

Run directly: uv run --directory token-api python tests/test_k12_registry_rung2.py
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Isolate every module-level DB path + point the API at a dead port BEFORE any
# token-api import (shared.py resolves both at import time).
_TMP = Path(tempfile.mkdtemp(prefix="k12-rung2-"))
_DB = _TMP / "agents.db"
os.environ["TOKEN_API_AGENTS_DB"] = str(_DB)
os.environ["TOKEN_API_URL"] = "http://localhost:1"

import db_schema  # noqa: E402

db_schema.init_database_sync(_DB)

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import stop_hook  # noqa: E402
from routes import hooks as hooks_routes  # noqa: E402

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


def _instance_row(instance_id: str) -> dict | None:
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM instances WHERE id = ?", (instance_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _mutations(instance_id: str) -> list[dict]:
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM instance_mutations WHERE instance_id = ? ORDER BY id",
        (instance_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def test_instance_stopped_endpoint() -> None:
    # Missing instance_id refused by the request model (422 at the HTTP layer).
    try:
        hooks_routes.HookInstanceWriteRequest()
        check("missing instance_id refused", False, "model accepted empty construction")
    except ValidationError:
        check("missing instance_id refused", True)

    # Empty/whitespace instance_id → 400, not a write.
    try:
        asyncio.run(
            hooks_routes.hook_instance_stopped(
                hooks_routes.HookInstanceWriteRequest(instance_id="   ")
            )
        )
        check("empty instance_id → 400", False, "no exception raised")
    except HTTPException as e:
        check("empty instance_id → 400", e.status_code == 400, str(e))

    # Unknown instance: not-found shape, not a 500.
    result = asyncio.run(
        hooks_routes.hook_instance_stopped(
            hooks_routes.HookInstanceWriteRequest(instance_id="no-such-instance")
        )
    )
    check(
        "unknown instance → not_found shape",
        result.get("success") is True and result.get("action") == "not_found",
        str(result),
    )

    # Seeded row gets stopped through the door, with the API actor logged.
    _seed_instance("rung2-api-stop")
    result = asyncio.run(
        hooks_routes.hook_instance_stopped(
            hooks_routes.HookInstanceWriteRequest(instance_id="rung2-api-stop")
        )
    )
    check(
        "seeded row stopped via door",
        result.get("success") is True and result.get("rows") == 1,
        str(result),
    )
    row = _instance_row("rung2-api-stop")
    check(
        "row status/stopped_at written",
        row is not None and row["status"] == "stopped" and row["stopped_at"],
        str(row),
    )
    muts = _mutations("rung2-api-stop")
    check(
        "mutation logged with API actor",
        len(muts) == 1
        and muts[0]["actor"] == "stop-hook:api"
        and muts[0]["write_source"] == "stop_hook"
        and muts[0]["mutation_type"] == "instance_stopped",
        str(muts),
    )

    # Where-guard: a second call matches no rows (already stopped).
    result = asyncio.run(
        hooks_routes.hook_instance_stopped(
            hooks_routes.HookInstanceWriteRequest(instance_id="rung2-api-stop")
        )
    )
    check(
        "second call reports 0 rows",
        result.get("rows") == 0 and result.get("action") == "already_stopped",
        str(result),
    )


def test_clear_human_anchor_endpoint() -> None:
    _seed_instance(
        "rung2-api-anchor",
        human_anchored_at="2026-07-15T10:00:00",
        human_anchor_source="auq",
    )
    result = asyncio.run(
        hooks_routes.hook_clear_human_anchor(
            hooks_routes.HookInstanceWriteRequest(instance_id="rung2-api-anchor")
        )
    )
    check(
        "anchor cleared via door",
        result.get("success") is True and result.get("rows") == 1,
        str(result),
    )
    row = _instance_row("rung2-api-anchor")
    check(
        "anchor fields nulled",
        row is not None and row["human_anchored_at"] is None and row["human_anchor_source"] is None,
        str(row),
    )
    muts = _mutations("rung2-api-anchor")
    check(
        "mutation logged with API actor",
        len(muts) == 1 and muts[0]["actor"] == "stop-hook:clear-human-anchor:api",
        str(muts),
    )

    result = asyncio.run(
        hooks_routes.hook_clear_human_anchor(
            hooks_routes.HookInstanceWriteRequest(instance_id="no-such-instance")
        )
    )
    check(
        "unknown instance → not_found shape",
        result.get("success") is True and result.get("action") == "not_found",
        str(result),
    )


def test_stop_hook_fallback() -> None:
    # TOKEN_API_URL points at a dead port (set before import), so the write
    # door is unreachable and both functions must land via direct sqlite.
    check(
        "stop_hook sees dead API port",
        stop_hook.TOKEN_API_URL == "http://localhost:1",
        stop_hook.TOKEN_API_URL,
    )
    check("stop_hook DB isolated", str(stop_hook.DB_PATH) == str(_DB), str(stop_hook.DB_PATH))

    _seed_instance("rung2-fallback-stop")
    stop_hook.mark_cron_instance_stopped("rung2-fallback-stop")
    row = _instance_row("rung2-fallback-stop")
    check(
        "fallback still stops the row",
        row is not None and row["status"] == "stopped" and row["stopped_at"],
        str(row),
    )
    muts = _mutations("rung2-fallback-stop")
    check(
        "fallback logs the direct actor",
        len(muts) == 1 and muts[0]["actor"] == "stop-hook",
        str(muts),
    )

    _seed_instance(
        "rung2-fallback-anchor",
        human_anchored_at="2026-07-15T10:00:00",
        human_anchor_source="auq",
    )
    stop_hook.clear_human_anchor_on_stop("rung2-fallback-anchor")
    row = _instance_row("rung2-fallback-anchor")
    check(
        "fallback still clears the anchor",
        row is not None and row["human_anchored_at"] is None,
        str(row),
    )
    muts = _mutations("rung2-fallback-anchor")
    check(
        "fallback logs the direct anchor actor",
        len(muts) == 1 and muts[0]["actor"] == "stop-hook:clear-human-anchor",
        str(muts),
    )


def main() -> int:
    for test in (
        test_instance_stopped_endpoint,
        test_clear_human_anchor_endpoint,
        test_stop_hook_fallback,
    ):
        print(f"— {test.__name__}")
        test()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("\nall green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
