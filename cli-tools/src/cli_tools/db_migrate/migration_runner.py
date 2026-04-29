"""Core migration execution logic.

All connections use the Cloud SQL Python Connector with the active gcloud
account token. No proxy binary or public IP management needed.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

try:
    import asyncpg
except ImportError:
    asyncpg = None  # type: ignore[assignment]

try:
    from google.cloud.sql.connector import Connector, IPTypes

    CLOUD_SQL_CONNECTOR_AVAILABLE = True
except ImportError:
    CLOUD_SQL_CONNECTOR_AVAILABLE = False
    Connector = None  # type: ignore[assignment, misc]
    IPTypes = None  # type: ignore[assignment, misc]


@dataclass
class MigrationResult:
    """Result of a migration run."""

    success: bool
    statements_executed: int = 0
    error: str | None = None
    duration_ms: float = 0
    messages: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------


def _sql_has_transaction_control(sql: str) -> bool:
    """Check if SQL already contains BEGIN/COMMIT/ROLLBACK statements."""
    upper = sql.upper()
    return bool(re.search(r"\bBEGIN\b", upper) or re.search(r"\bCOMMIT\b", upper))


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def _get_gcloud_credentials() -> Any:
    """Get credentials from the active gcloud account.

    Uses `gcloud auth print-access-token` which has broader permissions than
    Application Default Credentials (ADC). ADC uses a restricted OAuth client
    that may lack Cloud SQL scopes on cross-project instances.
    """
    import shutil

    import google.oauth2.credentials

    gcloud = shutil.which("gcloud") or "/snap/bin/gcloud"
    result = subprocess.run(
        [gcloud, "auth", "print-access-token"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    token = result.stdout.strip()
    if not token or result.returncode != 0:
        raise RuntimeError(f"Failed to get gcloud access token: {result.stderr.strip()}")
    return google.oauth2.credentials.Credentials(token=token)


async def _connect_connector(env_config: dict[str, Any], password: str | None) -> tuple[Any, Any]:
    """Connect via Cloud SQL Python Connector.

    Uses the active gcloud account token (not ADC) for cross-project
    compatibility.

    Returns (connection, connector) — caller must close both.
    """
    creds = _get_gcloud_credentials()

    loop = asyncio.get_running_loop()
    connector = Connector(loop=loop, credentials=creds, quota_project="")

    conn = await connector.connect_async(
        env_config["instance"],
        "asyncpg",
        user=env_config["user"],
        password=password,
        db=env_config["database"],
        ip_type=IPTypes.PUBLIC,
    )
    return conn, connector


# ---------------------------------------------------------------------------
# Migration execution
# ---------------------------------------------------------------------------


async def _execute_migration(
    conn: Any,
    sql_content: str,
    dry_run: bool,
) -> MigrationResult:
    """Run migration SQL on an already-established connection."""
    start = time.monotonic()
    has_txn = _sql_has_transaction_control(sql_content)
    messages: list[str] = []

    try:
        if dry_run:
            await conn.execute("BEGIN")
            messages.append("DRY RUN: Transaction started")
            try:
                result = await conn.execute(sql_content)
                messages.append(f"Executed successfully: {result}")
            except Exception as e:
                messages.append(f"Error during execution: {e}")
                await conn.execute("ROLLBACK")
                messages.append("DRY RUN: Rolled back")
                elapsed = (time.monotonic() - start) * 1000
                return MigrationResult(
                    success=False, error=str(e), duration_ms=elapsed, messages=messages
                )
            await conn.execute("ROLLBACK")
            messages.append("DRY RUN: Rolled back (no changes applied)")
        elif has_txn:
            messages.append("SQL contains transaction control, executing as-is")
            result = await conn.execute(sql_content)
            messages.append(f"Result: {result}")
        else:
            await conn.execute("BEGIN")
            messages.append("Transaction started")
            try:
                result = await conn.execute(sql_content)
                messages.append(f"Result: {result}")
                await conn.execute("COMMIT")
                messages.append("Transaction committed")
            except Exception as e:
                await conn.execute("ROLLBACK")
                messages.append(f"Transaction rolled back due to error: {e}")
                elapsed = (time.monotonic() - start) * 1000
                return MigrationResult(
                    success=False, error=str(e), duration_ms=elapsed, messages=messages
                )

        elapsed = (time.monotonic() - start) * 1000
        return MigrationResult(
            success=True, statements_executed=1, duration_ms=elapsed, messages=messages
        )
    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        return MigrationResult(success=False, error=str(e), duration_ms=elapsed, messages=messages)


async def run_migration(
    env_config: dict[str, Any],
    sql_content: str,
    password: str | None,
    dry_run: bool = False,
    use_connector: bool = True,
) -> MigrationResult:
    """Execute a migration against the database via Cloud SQL connector."""
    if asyncpg is None:
        return MigrationResult(
            success=False,
            error="asyncpg is required. Install with: pip install asyncpg",
        )
    if not CLOUD_SQL_CONNECTOR_AVAILABLE:
        return MigrationResult(
            success=False,
            error="Cloud SQL Connector not available. Install with: pip install cloud-sql-python-connector[asyncpg]",
        )

    start = time.monotonic()
    conn = None
    connector = None

    try:
        conn, connector = await _connect_connector(env_config, password)
        return await _execute_migration(conn, sql_content, dry_run)

    except asyncio.TimeoutError:
        elapsed = (time.monotonic() - start) * 1000
        return MigrationResult(
            success=False, error="Connection timed out after 30s", duration_ms=elapsed
        )
    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        return MigrationResult(success=False, error=str(e), duration_ms=elapsed)
    finally:
        if conn:
            await conn.close()
        if connector:
            await connector.close_async()


async def run_verify_query(
    env_config: dict[str, Any],
    verify_sql: str,
    password: str | None,
    use_connector: bool = True,
) -> list[dict[str, Any]]:
    """Run a verification SELECT query and return rows as dicts."""
    if asyncpg is None:
        return []

    conn = None
    connector = None
    try:
        conn, connector = await _connect_connector(env_config, password)
        rows = await conn.fetch(verify_sql)
        return [dict(row) for row in rows]
    except Exception as e:
        print(f"  Verification query failed: {e}")
        return []
    finally:
        if conn:
            await conn.close()
        if connector:
            await connector.close_async()
