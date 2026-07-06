"""Canonical SQLite connection helpers for Token-API.

All async agents.db callers should enter through this module instead of calling
``aiosqlite.connect`` directly.  The helper applies the same WAL-compatible
pragmas everywhere and instruments lock failures with the call site, current
HTTP endpoint (when any), SQL operation, and elapsed wait time.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import sqlite3
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger("token_api")

BUSY_TIMEOUT_MS = int(os.environ.get("TOKEN_API_SQLITE_BUSY_TIMEOUT_MS", "5000"))
BUSY_TIMEOUT_SECONDS = max(BUSY_TIMEOUT_MS / 1000.0, 0.001)

RUNTIME_DATABASE_DIR = Path(
    os.environ.get("TOKEN_API_DATABASE_DIR", Path.home() / "runtimes" / "database")
).expanduser()
LEGACY_AGENTS_DB_PATH = Path.home() / ".claude" / "agents.db"


def _legacy_token_api_db_unless_live() -> str | None:
    value = os.environ.get("TOKEN_API_DB")
    if not value:
        return None
    path = Path(value).expanduser()
    if path.resolve() == LEGACY_AGENTS_DB_PATH.resolve():
        return None
    return value


def _split_db_path(env_name: str, filename: str) -> Path:
    value = os.environ.get(env_name)
    if value:
        return Path(value).expanduser()
    legacy = _legacy_token_api_db_unless_live()
    if legacy:
        # Preserve dev/test redirection without collapsing every split store into
        # the same file.  ``TOKEN_API_DB=/tmp/agents.db`` yields
        # ``/tmp/telemetry.db`` / ``/tmp/timer.db`` unless a store-specific
        # override is supplied.
        legacy_path = Path(legacy).expanduser()
        return legacy_path.with_name(filename)
    return RUNTIME_DATABASE_DIR / filename


def resolve_telemetry_db_path() -> Path:
    """Resolve the split high-frequency telemetry store path."""
    return _split_db_path("TOKEN_API_TELEMETRY_DB", "telemetry.db")


AGENTS_DB_PATH = Path(
    os.environ.get("TOKEN_API_AGENTS_DB")
    or _legacy_token_api_db_unless_live()
    or RUNTIME_DATABASE_DIR / "agents.db"
).expanduser()
TIMER_DB_PATH = _split_db_path("TOKEN_API_TIMER_DB", "timer.db")
TELEMETRY_DB_PATH = resolve_telemetry_db_path()

_CURRENT_ENDPOINT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "token_api_sqlite_endpoint", default=None
)


def set_sqlite_endpoint(endpoint: str | None) -> contextvars.Token[str | None]:
    return _CURRENT_ENDPOINT.set(endpoint)


def reset_sqlite_endpoint(token) -> None:
    _CURRENT_ENDPOINT.reset(token)


def _default_site() -> str:
    try:
        frame = sys._getframe(2)
    except ValueError:
        return "unknown"
    while frame:
        filename = frame.f_code.co_filename
        if not filename.endswith("db_connections.py"):
            return f"{Path(filename).name}:{frame.f_lineno}"
        frame = frame.f_back
    return "unknown"


def _operation_from_sql(args: tuple[Any, ...]) -> str:
    if not args:
        return "sqlite"
    sql = args[0]
    if not isinstance(sql, str):
        return "sqlite"
    stripped = sql.lstrip()
    if not stripped:
        return "sqlite"
    return stripped.split(None, 1)[0].upper()


def _log_locked(
    *,
    site: str,
    endpoint: str | None,
    operation: str,
    elapsed_ms: float,
    db_path: Path,
) -> None:
    logger.error(
        "sqlite database locked site=%s endpoint=%s operation=%s wait_ms=%.1f db=%s",
        site,
        endpoint or "-",
        operation,
        elapsed_ms,
        db_path,
    )


def _is_locked(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "database is locked" in str(exc).lower()


def _instrument_async_connection(
    conn: aiosqlite.Connection,
    *,
    db_path: Path,
    site: str,
    endpoint: str | None,
) -> aiosqlite.Connection:
    def wrap(name: str, operation: str | None = None) -> None:
        original = getattr(conn, name)

        async def wrapped(*args, **kwargs):
            start = time.monotonic()
            try:
                return await original(*args, **kwargs)
            except Exception as exc:
                if _is_locked(exc):
                    _log_locked(
                        site=site,
                        endpoint=endpoint,
                        operation=operation or _operation_from_sql(args),
                        elapsed_ms=(time.monotonic() - start) * 1000.0,
                        db_path=db_path,
                    )
                raise

        setattr(conn, name, wrapped)

    wrap("execute")
    wrap("executemany")
    wrap("executescript", "SCRIPT")
    wrap("commit", "COMMIT")
    return conn


async def _open_async_sqlite(
    db_path: Path | str,
    *,
    site: str | None = None,
    endpoint: str | None = None,
    timeout: float | None = None,
    isolation_level: str | None | object = Ellipsis,
    wal: bool = True,
) -> aiosqlite.Connection:
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {"timeout": timeout if timeout is not None else BUSY_TIMEOUT_SECONDS}
    if isolation_level is not Ellipsis:
        kwargs["isolation_level"] = isolation_level
    open_site = site or _default_site()
    current_endpoint = endpoint if endpoint is not None else _CURRENT_ENDPOINT.get()
    start = time.monotonic()
    try:
        conn = await aiosqlite.connect(path, **kwargs)
        conn = _instrument_async_connection(
            conn, db_path=path, site=open_site, endpoint=current_endpoint
        )
        await conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        await conn.execute("PRAGMA foreign_keys=ON")
        if wal:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
        return conn
    except Exception as exc:
        if _is_locked(exc):
            _log_locked(
                site=open_site,
                endpoint=current_endpoint,
                operation="CONNECT",
                elapsed_ms=(time.monotonic() - start) * 1000.0,
                db_path=path,
            )
        raise


class _AsyncConnectionFactory:
    def __init__(self, db_path: Path | str, **kwargs: Any):
        self.db_path = db_path
        self.kwargs = kwargs
        self._conn: aiosqlite.Connection | None = None

    def __await__(self):
        return _open_async_sqlite(self.db_path, **self.kwargs).__await__()

    async def __aenter__(self) -> aiosqlite.Connection:
        self._conn = await _open_async_sqlite(self.db_path, **self.kwargs)
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


def connect_sqlite(db_path: Path | str, **kwargs: Any) -> _AsyncConnectionFactory:
    if "site" not in kwargs:
        kwargs["site"] = _default_site()
    return _AsyncConnectionFactory(db_path, **kwargs)


def connect_agents_db(db_path: Path | str | None = None, **kwargs: Any) -> _AsyncConnectionFactory:
    return connect_sqlite(db_path or AGENTS_DB_PATH, **kwargs)


def connect_timer_db(db_path: Path | str | None = None, **kwargs: Any) -> _AsyncConnectionFactory:
    return connect_sqlite(db_path or TIMER_DB_PATH, **kwargs)


def connect_telemetry_db(
    db_path: Path | str | None = None, **kwargs: Any
) -> _AsyncConnectionFactory:
    return connect_sqlite(db_path or TELEMETRY_DB_PATH, **kwargs)


@contextlib.contextmanager
def connect_sqlite_sync(
    db_path: Path | str,
    *,
    site: str | None = None,
    endpoint: str | None = None,
    timeout: float | None = None,
    wal: bool = True,
) -> Iterator[sqlite3.Connection]:
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    open_site = site or _default_site()
    current_endpoint = endpoint if endpoint is not None else _CURRENT_ENDPOINT.get()
    start = time.monotonic()
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(
            path, timeout=timeout if timeout is not None else BUSY_TIMEOUT_SECONDS
        )
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
    except Exception as exc:
        if _is_locked(exc):
            _log_locked(
                site=open_site,
                endpoint=current_endpoint,
                operation="SYNC",
                elapsed_ms=(time.monotonic() - start) * 1000.0,
                db_path=path,
            )
        raise
    finally:
        if conn is not None:
            conn.close()


def connect_agents_db_sync(
    db_path: Path | str | None = None, **kwargs: Any
) -> contextlib.AbstractContextManager[sqlite3.Connection]:
    return connect_sqlite_sync(db_path or AGENTS_DB_PATH, **kwargs)
