"""Core query execution logic for database operations.

This module provides secure, controlled access to Cloud SQL databases
using either Cloud SQL Auth Proxy or direct connections.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import asyncpg
except ImportError:
    asyncpg = None  # type: ignore[assignment]

# Cloud SQL Connector for IAM-authenticated connections
try:
    from google.cloud.sql.connector import Connector, IPTypes

    CLOUD_SQL_CONNECTOR_AVAILABLE = True
except ImportError:
    CLOUD_SQL_CONNECTOR_AVAILABLE = False
    Connector = None  # type: ignore[assignment, misc]
    IPTypes = None  # type: ignore[assignment, misc]


def _get_gcloud_credentials() -> Any:
    """Get credentials from the active gcloud account.

    Uses `gcloud auth print-access-token` which has broader permissions than
    Application Default Credentials (ADC).
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


# Defaults used when the askCivic deploy YAML checkout is not present on this
# machine. Keep these aligned with the deployed Cloud SQL instances/databases.
DEFAULT_ENV_CONFIGS: dict[str, dict[str, Any]] = {
    "development": {
        "instance": "pax-dev-469018:us-central1:pax-sql",
        "database": "pax-sql",
        "user": "postgres",
        "host": "localhost",
        "public_ip": "34.60.168.98",
        "port": 5432,
        "read_only": False,
    },
    "staging": {
        "instance": "pax-staging-008732:us-central1:pax-sql",
        "database": "pax-db-staging",
        "user": "postgres",
        "host": "localhost",
        "public_ip": None,
        "port": 5432,
        "read_only": False,
    },
    "production": {
        "instance": "pax-prod-467920:us-central1:pax-sql",
        "database": "pax-sql",
        "user": "postgres",
        "host": "localhost",
        "public_ip": None,
        "port": 5432,
        "read_only": True,
    },
}

DEFAULT_SECRET_NAME = "db-password"


# Deploy YAML directory — search known locations
def _find_deploy_dir() -> Path:
    candidates = [
        Path.home() / "ProcAgentDir" / "ProcurementAgentAI" / "deploy",
        Path.home() / "worktrees" / "askCivic" / "wt-main" / "deploy",
        Path.home() / "worktrees" / "askCivic" / "wt-command-system" / "deploy",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]  # fallback to original


DEPLOY_DIR = _find_deploy_dir()

# Mapping of environment names to YAML files
ENV_TO_YAML = {
    "development": "pax-development.yaml",
    "staging": "pax-staging.yaml",
    "production": "pax-production.yaml",
}


def _parse_yaml_env_vars(yaml_path: Path) -> dict[str, str]:
    """Parse environment variables from a Cloud Run YAML file."""
    try:
        import yaml
    except ImportError:
        return {}

    if not yaml_path.exists():
        return {}

    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    env_vars = {}
    try:
        containers = data["spec"]["template"]["spec"]["containers"]
        for container in containers:
            for env in container.get("env", []):
                name = env.get("name")
                value = env.get("value")
                if name and value is not None:
                    env_vars[name] = value
    except (KeyError, TypeError):
        pass

    return env_vars


def _load_environments() -> dict[str, dict[str, Any]]:
    """Load environment configurations from deploy YAML files.

    Reads PROJECT_ID, INSTANCE_CONNECTION_NAME, DB_NAME, DB_USER from
    the pax-*.yaml files in the askCivic deploy directory, when present.
    If that checkout is absent, uses built-in production-safe defaults that
    target the pax-sql Cloud SQL instance/database instead of the retired
    pax-db name.

    DB_HOST (public IP for dev) comes from .env file.
    """
    # Load .env for DB_HOST (public IP for direct connections)
    public_ip = None
    try:
        from dotenv import load_dotenv

        project_env = DEPLOY_DIR.parent / ".env"
        if project_env.exists():
            load_dotenv(project_env)
        public_ip = os.environ.get("DB_HOST")
    except ImportError:
        pass

    environments = {}

    for env_name, yaml_file in ENV_TO_YAML.items():
        yaml_path = DEPLOY_DIR / yaml_file
        env_vars = _parse_yaml_env_vars(yaml_path)

        defaults = DEFAULT_ENV_CONFIGS[env_name]
        instance = env_vars.get("INSTANCE_CONNECTION_NAME", defaults["instance"])
        project_id = env_vars.get("PROJECT_ID") or (
            instance.split(":", 1)[0] if ":" in instance else defaults["instance"].split(":", 1)[0]
        )
        environments[env_name] = {
            "project_id": project_id,
            "instance": instance,
            "database": env_vars.get("DB_NAME", defaults["database"]),
            "user": env_vars.get("DB_USER", defaults["user"]),
            "host": env_vars.get("DB_HOST", defaults["host"]),
            "public_ip": (public_ip or defaults["public_ip"])
            if env_name == "development"
            else defaults["public_ip"],
            "port": int(env_vars.get("DB_PORT", str(defaults["port"]))),
            "read_only": defaults["read_only"],
        }

    return environments


ENVIRONMENTS: dict[str, dict[str, Any]] = _load_environments()

# Aliases for environment names
ENV_ALIASES: dict[str, str] = {
    "dev": "development",
    "prod": "production",
    "stg": "staging",
}

# Dangerous query patterns blocked in production
BLOCKED_PATTERNS = [
    r"\bDROP\b",
    r"\bDELETE\b",
    r"\bTRUNCATE\b",
    r"\bALTER\b",
    r"\bUPDATE\b",
    r"\bINSERT\b",
    r"\bCREATE\b",
    r"\bGRANT\b",
    r"\bREVOKE\b",
]

# Safe query patterns allowed in production
ALLOWED_PATTERNS = [
    r"^\s*SELECT\b",
    r"^\s*EXPLAIN\b",
    r"^\s*SHOW\b",
    r"^\s*WITH\b",
]

DEFAULT_LIMIT = 100
QUERY_TIMEOUT = 600  # 10 minutes — increased for large dblink transfers


@dataclass
class QueryResult:
    """Result of a database query."""

    success: bool
    data: list[dict[str, Any]] | None = None
    columns: list[str] | None = None
    rows: list[tuple[Any, ...]] | None = None
    row_count: int = 0
    error: str | None = None


@dataclass
class PasswordResolutionResult:
    """Result of database password lookup."""

    password: str | None
    source: str | None = None
    error: str | None = None


def normalize_env(env: str) -> str:
    """Normalize environment name from aliases."""
    return ENV_ALIASES.get(env.lower(), env.lower())


def is_write_query(query: str) -> bool:
    """Check if a query is a write operation (INSERT, UPDATE, DELETE, etc.)."""
    query_upper = query.upper().strip()
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, query_upper, re.IGNORECASE):
            return True
    return False


def get_env_config(env: str) -> dict[str, Any]:
    """Get environment configuration."""
    env_name = normalize_env(env)
    if env_name not in ENVIRONMENTS:
        raise ValueError(
            f"Unknown environment '{env}'. Valid environments: {', '.join(ENVIRONMENTS.keys())}"
        )
    return ENVIRONMENTS[env_name].copy()


def validate_query(query: str, env_config: dict[str, Any]) -> tuple[bool, str]:
    """Validate a query for safety.

    Returns:
        Tuple of (is_valid, error_message)
    """
    query_upper = query.upper().strip()

    # For read-only environments, enforce strict validation
    if env_config.get("read_only"):
        # Check for blocked patterns
        for pattern in BLOCKED_PATTERNS:
            if re.search(pattern, query_upper, re.IGNORECASE):
                # Extract keyword from pattern (e.g., r"\bDROP\b" -> "DROP")
                keyword = pattern.replace("\\b", "").replace("\\", "")
                return False, f"Query blocked: {keyword} statements not allowed in production"

        # Verify it matches at least one allowed pattern
        allowed = False
        for pattern in ALLOWED_PATTERNS:
            if re.search(pattern, query_upper, re.IGNORECASE):
                allowed = True
                break

        if not allowed:
            return False, (
                "Query blocked: Only SELECT, EXPLAIN, SHOW, and WITH "
                "statements allowed in production"
            )

    return True, ""


def add_limit_if_missing(query: str, limit: int = DEFAULT_LIMIT) -> str:
    """Add LIMIT clause to SELECT queries if not present."""
    query_upper = query.upper().strip()

    # Only add LIMIT to SELECT queries without one
    if query_upper.startswith("SELECT") and "LIMIT" not in query_upper:
        query = query.rstrip(";").strip()
        return f"{query} LIMIT {limit}"

    return query


def format_results_table(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    """Format query results as an ASCII table."""
    if not rows:
        return "No results returned."

    # Calculate column widths
    widths = [len(col) for col in columns]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val) if val is not None else "NULL"))

    # Cap column widths at 50 characters
    widths = [min(w, 50) for w in widths]

    # Build table
    lines = []

    # Header
    header = " | ".join(col.ljust(widths[i])[: widths[i]] for i, col in enumerate(columns))
    separator = "-+-".join("-" * w for w in widths)
    lines.append(header)
    lines.append(separator)

    # Data rows
    for row in rows:
        formatted_row = []
        for i, val in enumerate(row):
            str_val = str(val) if val is not None else "NULL"
            if len(str_val) > widths[i]:
                str_val = str_val[: widths[i] - 3] + "..."
            formatted_row.append(str_val.ljust(widths[i]))
        lines.append(" | ".join(formatted_row))

    lines.append(f"\n({len(rows)} row{'s' if len(rows) != 1 else ''})")

    return "\n".join(lines)


def format_results_json(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    """Format query results as JSON."""
    results = []
    for row in rows:
        results.append(dict(zip(columns, row, strict=False)))
    return json.dumps(results, indent=2, default=str)


def _project_id_from_config(env_config: dict[str, Any] | None) -> str | None:
    if not env_config:
        return None

    project_id = env_config.get("project_id")
    if isinstance(project_id, str) and project_id:
        return project_id

    instance = env_config.get("instance")
    if isinstance(instance, str) and ":" in instance:
        return instance.split(":", 1)[0]

    return None


def _access_secret_manager(secret_name: str, project_id: str) -> str:
    """Fetch a secret value from GCP Secret Manager with gcloud."""
    gcloud = shutil.which("gcloud") or "/snap/bin/gcloud"
    if not Path(gcloud).exists() and shutil.which("gcloud") is None:
        raise RuntimeError("gcloud not found on PATH")

    result = subprocess.run(
        [
            gcloud,
            "secrets",
            "versions",
            "access",
            "latest",
            "--secret",
            secret_name,
            "--project",
            project_id,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(stderr)

    secret = result.stdout.strip()
    if not secret:
        raise RuntimeError("Secret Manager returned an empty payload")

    return secret


def resolve_password(
    env_config: dict[str, Any] | None = None,
    *,
    secret_name: str = DEFAULT_SECRET_NAME,
) -> PasswordResolutionResult:
    """Resolve the database password without silent passwordless fallback.

    Checks in order:
    1. DB_PASSWORD environment variable
    2. .env file in current working directory
    3. .env file next to the discovered deploy directory
    4. .env file in the legacy ProcurementAgentAI project root
    5. GCP Secret Manager secret `db-password` in the target project
    """
    # First check environment variable
    password = os.environ.get("DB_PASSWORD")
    if password:
        return PasswordResolutionResult(password=password, source="DB_PASSWORD")

    # Try to load from .env files
    try:
        from dotenv import dotenv_values
    except ImportError:
        dotenv_values = None  # type: ignore[assignment]

    if dotenv_values is not None:
        env_files = [
            Path.cwd() / ".env",
            DEPLOY_DIR.parent / ".env",
            Path.home() / "ProcAgentDir" / "ProcurementAgentAI" / ".env",
        ]
        seen: set[Path] = set()
        for env_file in env_files:
            if env_file in seen:
                continue
            seen.add(env_file)
            if env_file.exists():
                values = dotenv_values(env_file)
                if values.get("DB_PASSWORD"):
                    return PasswordResolutionResult(
                        password=values["DB_PASSWORD"],
                        source=str(env_file),
                    )

    project_id = _project_id_from_config(env_config)
    if not project_id:
        return PasswordResolutionResult(
            password=None,
            error=(
                "DB_PASSWORD was not set, no .env DB_PASSWORD was found, and no "
                "target project/instance is configured for Secret Manager lookup."
            ),
        )

    try:
        return PasswordResolutionResult(
            password=_access_secret_manager(secret_name, project_id),
            source=f"Secret Manager {project_id}/{secret_name}",
        )
    except Exception as e:
        return PasswordResolutionResult(
            password=None,
            error=(
                "DB_PASSWORD was not set and no .env DB_PASSWORD was found. "
                f"Secret Manager lookup failed for {project_id}/{secret_name}: {e}"
            ),
        )


def get_password(env_config: dict[str, Any] | None = None) -> str | None:
    """Get database password from env/.env/Secret Manager.

    Prefer resolve_password(...) in CLI paths so failures can be reported
    explicitly instead of accidentally attempting passwordless auth.
    """
    return resolve_password(env_config).password


async def execute_query_with_connector(
    env_config: dict[str, Any],
    query: str,
    password: str | None = None,
) -> QueryResult:
    """Execute a query using Cloud SQL Python Connector.

    This is the preferred method as it handles IAM authentication automatically.
    """
    if not CLOUD_SQL_CONNECTOR_AVAILABLE:
        return QueryResult(
            success=False,
            error="Cloud SQL Connector not available. Install with: pip install cloud-sql-python-connector[asyncpg]",
        )

    if asyncpg is None:
        return QueryResult(
            success=False,
            error="asyncpg is required. Install with: pip install asyncpg",
        )

    instance_name = env_config.get("instance")
    if not instance_name:
        return QueryResult(
            success=False,
            error="No instance name configured for this environment",
        )

    connector = None
    conn = None
    try:
        loop = asyncio.get_running_loop()
        creds = _get_gcloud_credentials()
        connector = Connector(loop=loop, credentials=creds, quota_project="")

        async def getconn() -> Any:
            return await connector.connect_async(
                instance_name,
                "asyncpg",
                user=env_config["user"],
                password=password,
                db=env_config["database"],
                ip_type=IPTypes.PUBLIC,
            )

        conn = await asyncio.wait_for(getconn(), timeout=QUERY_TIMEOUT)

        # For read-only environments, start a read-only transaction
        if env_config.get("read_only"):
            await conn.execute("SET TRANSACTION READ ONLY")

        # Execute query with timeout
        rows = await asyncio.wait_for(
            conn.fetch(query),
            timeout=QUERY_TIMEOUT,
        )

        if not rows:
            return QueryResult(success=True, row_count=0)

        # Extract column names and data
        columns = list(rows[0].keys())
        data = [tuple(row.values()) for row in rows]

        return QueryResult(
            success=True,
            columns=columns,
            rows=data,
            row_count=len(data),
        )

    except TimeoutError:
        return QueryResult(
            success=False,
            error=f"Query timed out after {QUERY_TIMEOUT} seconds",
        )
    except Exception as e:
        if asyncpg and isinstance(e, asyncpg.PostgresError):
            return QueryResult(success=False, error=f"Database error: {e}")
        return QueryResult(success=False, error=f"Error: {e}")
    finally:
        if conn:
            await conn.close()
        if connector:
            await connector.close_async()


async def execute_query_direct(
    env_config: dict[str, Any],
    query: str,
    password: str | None = None,
) -> QueryResult:
    """Execute a query using direct TCP connection (for proxy or direct IP)."""
    if asyncpg is None:
        return QueryResult(
            success=False,
            error="asyncpg is required. Install with: pip install asyncpg",
        )

    conn = None
    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(
                host=env_config["host"],
                port=env_config["port"],
                database=env_config["database"],
                user=env_config["user"],
                password=password,
                timeout=QUERY_TIMEOUT,
            ),
            timeout=QUERY_TIMEOUT,
        )

        # For read-only environments, start a read-only transaction
        if env_config.get("read_only"):
            await conn.execute("SET TRANSACTION READ ONLY")

        # Execute query with timeout
        rows = await asyncio.wait_for(
            conn.fetch(query),
            timeout=QUERY_TIMEOUT,
        )

        if not rows:
            return QueryResult(success=True, row_count=0)

        # Extract column names and data
        columns = list(rows[0].keys())
        data = [tuple(row.values()) for row in rows]

        return QueryResult(
            success=True,
            columns=columns,
            rows=data,
            row_count=len(data),
        )

    except TimeoutError:
        return QueryResult(
            success=False,
            error=f"Query timed out after {QUERY_TIMEOUT} seconds",
        )
    except ConnectionRefusedError:
        return QueryResult(
            success=False,
            error=(
                "Could not connect to database.\n\n"
                "Options:\n"
                "1. Start Cloud SQL Auth Proxy: "
                "./scripts/start-sql-proxy.sh <environment> --background\n"
                "2. Use direct connection (dev only): --direct flag with DB_PASSWORD set"
            ),
        )
    except Exception as e:
        if asyncpg and isinstance(e, asyncpg.PostgresError):
            return QueryResult(success=False, error=f"Database error: {e}")
        return QueryResult(success=False, error=f"Error: {e}")
    finally:
        if conn:
            await conn.close()


async def execute_query(
    env_config: dict[str, Any],
    query: str,
    password: str | None = None,
    use_connector: bool = True,
) -> QueryResult:
    """Execute a query and return results.

    By default, uses Cloud SQL Python Connector for IAM-authenticated connections.
    Falls back to direct TCP connection if connector is not available or if
    use_connector=False is specified.
    """
    # If connector is available and requested, use it (preferred method)
    if use_connector and CLOUD_SQL_CONNECTOR_AVAILABLE and env_config.get("instance"):
        return await execute_query_with_connector(env_config, query, password)

    # Fall back to direct TCP connection
    return await execute_query_direct(env_config, query, password)


async def list_tables(
    env_config: dict[str, Any],
    password: str | None = None,
) -> QueryResult:
    """List all tables in the database."""
    query = """
        SELECT table_name, table_type
        FROM information_schema.tables
        WHERE table_schema = 'public'
        ORDER BY table_type, table_name
    """
    return await execute_query(env_config, query, password)


async def describe_table(
    env_config: dict[str, Any],
    table_name: str,
    password: str | None = None,
) -> QueryResult:
    """Describe a table's columns."""
    # Validate table name to prevent SQL injection
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", table_name):
        return QueryResult(success=False, error=f"Invalid table name '{table_name}'")

    query = f"""
        SELECT
            column_name,
            data_type,
            is_nullable,
            column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = '{table_name}'
        ORDER BY ordinal_position
    """
    return await execute_query(env_config, query, password)


def check_proxy_status(env_config: dict[str, Any]) -> dict[str, Any]:
    """Check if Cloud SQL Auth Proxy is running for the given environment."""
    instance = env_config.get("instance", "")
    port = env_config.get("port", 5432)

    # Check if we can connect to localhost on the proxy port
    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(("localhost", port))
        sock.close()
        is_running = result == 0
    except Exception:
        is_running = False

    return {
        "running": is_running,
        "instance": instance,
        "port": port,
    }


def start_proxy(env_config: dict[str, Any], background: bool = True) -> dict[str, Any]:
    """Start Cloud SQL Auth Proxy for the given environment.

    Returns status dict with success, message, and any process info.
    """
    instance = env_config.get("instance", "")
    port = env_config.get("port", 5432)

    # Check if already running
    status = check_proxy_status(env_config)
    if status["running"]:
        return {
            "success": True,
            "message": f"Proxy already running on port {port}",
            "already_running": True,
        }

    # Try to find cloud-sql-proxy or cloud_sql_proxy
    proxy_cmd = None
    for cmd in ["cloud-sql-proxy", "cloud_sql_proxy"]:
        try:
            subprocess.run(
                ["which", cmd],
                capture_output=True,
                check=True,
            )
            proxy_cmd = cmd
            break
        except subprocess.CalledProcessError:
            continue

    if not proxy_cmd:
        return {
            "success": False,
            "message": (
                "Cloud SQL Auth Proxy not found. Install it with:\n"
                "curl -o cloud-sql-proxy "
                "https://storage.googleapis.com/cloud-sql-connectors/"
                "cloud-sql-proxy/v2.8.1/cloud-sql-proxy.linux.amd64\n"
                "chmod +x cloud-sql-proxy\n"
                "sudo mv cloud-sql-proxy /usr/local/bin/"
            ),
        }

    # Build proxy command
    args = [
        proxy_cmd,
        f"--port={port}",
        instance,
    ]

    try:
        if background:
            # Start in background
            process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            # Wait a moment and check if it's running
            import time

            time.sleep(2)
            new_status = check_proxy_status(env_config)
            if new_status["running"]:
                return {
                    "success": True,
                    "message": f"Proxy started on port {port} (PID: {process.pid})",
                    "pid": process.pid,
                }
            else:
                return {
                    "success": False,
                    "message": "Proxy process started but connection failed. Check gcloud auth.",
                }
        else:
            # Run in foreground (blocking)
            subprocess.run(args)
            return {"success": True, "message": "Proxy stopped"}

    except Exception as e:
        return {
            "success": False,
            "message": f"Failed to start proxy: {e}",
        }
