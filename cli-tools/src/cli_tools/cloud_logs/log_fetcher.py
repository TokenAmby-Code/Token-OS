"""Core log fetching logic for Google Cloud Platform.

Handles gcloud CLI interactions and log parsing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


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


def _load_environments() -> dict[str, dict[str, str]]:
    """Load environment configurations from deploy YAML files.

    Reads PROJECT_ID from pax-*.yaml files in
    ~/ProcAgentDir/ProcurementAgentAI/deploy/
    """
    environments = {}

    for env_name, yaml_file in ENV_TO_YAML.items():
        yaml_path = DEPLOY_DIR / yaml_file
        env_vars = _parse_yaml_env_vars(yaml_path)

        environments[env_name] = {
            "project": env_vars.get("PROJECT_ID", ""),
            "region": "us-central1",
        }

    return environments


ENVIRONMENTS = _load_environments()

ENV_ALIASES = {
    "dev": "development",
    "prod": "production",
    "prd": "production",
    "stg": "development",  # staging uses dev project
    "staging": "development",
}

# Known services
SERVICES = {
    "pax-chat": "Main backend service",
    "pax-widget-proxy": "Widget proxy service",
    "pax-google-chat-proxy": "Google Chat integration proxy",
}

DEFAULT_SERVICE = "pax-chat"


@dataclass
class LogEntry:
    """Represents a single log entry."""

    timestamp: str
    severity: str
    message: str
    service: str
    revision: str | None = None
    trace_id: str | None = None
    raw: dict | None = None


@dataclass
class LogResult:
    """Result of a log fetch operation."""

    success: bool
    entries: list[LogEntry]
    error: str | None = None
    query_used: str | None = None
    count: int = 0


def normalize_env(env: str) -> str:
    """Normalize environment name to canonical form."""
    env_lower = env.lower()
    return ENV_ALIASES.get(env_lower, env_lower)


def get_project_for_env(env: str) -> str:
    """Get GCP project ID for environment."""
    env_name = normalize_env(env)
    if env_name not in ENVIRONMENTS:
        raise ValueError(f"Unknown environment: {env}. Use: development, production")
    return ENVIRONMENTS[env_name]["project"]


def parse_duration(duration_str: str) -> timedelta:
    """Parse duration string like '1h', '30m', '2d' into timedelta.

    Supports:
        - Xm: minutes
        - Xh: hours
        - Xd: days
    """
    match = re.match(r"^(\d+)([mhd])$", duration_str.lower())
    if not match:
        raise ValueError(
            f"Invalid duration: {duration_str}. Use format like '1h', '30m', '2d'"
        )

    value = int(match.group(1))
    unit = match.group(2)

    if unit == "m":
        return timedelta(minutes=value)
    elif unit == "h":
        return timedelta(hours=value)
    elif unit == "d":
        return timedelta(days=value)
    else:
        raise ValueError(f"Unknown duration unit: {unit}")


def get_latest_revision(project: str, service: str, region: str = "us-central1") -> str | None:
    """Get the latest revision name for a service."""
    try:
        result = subprocess.run(
            [
                "gcloud",
                "run",
                "revisions",
                "list",
                f"--service={service}",
                f"--region={region}",
                f"--project={project}",
                "--limit=1",
                "--format=value(REVISION)",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return None
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        return None


def get_recent_deployment_time(
    project: str, service: str, region: str = "us-central1"
) -> datetime | None:
    """Get the deployment time of the most recent revision."""
    try:
        result = subprocess.run(
            [
                "gcloud",
                "run",
                "revisions",
                "list",
                f"--service={service}",
                f"--region={region}",
                f"--project={project}",
                "--limit=1",
                "--format=value(DEPLOYED)",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Parse the timestamp (format: 2024-01-15T10:30:00Z or similar)
            timestamp_str = result.stdout.strip()
            # Handle various formats
            for fmt in ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%dT%H:%M:%S%z"]:
                try:
                    return datetime.strptime(timestamp_str, fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
            # Try ISO format as fallback
            try:
                return datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            except ValueError:
                pass
        return None
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        return None


def _build_log_filter(
    service: str,
    severity: str | None = None,
    since: timedelta | None = None,
    revision: str | None = None,
    pattern: str | None = None,
    http_only: bool = False,
) -> str:
    """Build a gcloud logging filter string."""
    filters = [
        'resource.type="cloud_run_revision"',
        f'resource.labels.service_name="{service}"',
    ]

    if severity:
        severity_upper = severity.upper()
        if severity_upper == "ERROR":
            filters.append("severity>=ERROR")
        elif severity_upper == "WARNING":
            filters.append("severity>=WARNING")
        elif severity_upper == "INFO":
            filters.append("severity>=INFO")
        elif severity_upper == "DEBUG":
            filters.append("severity>=DEBUG")
        else:
            filters.append(f'severity="{severity_upper}"')

    if since:
        # Calculate timestamp
        cutoff = datetime.now(timezone.utc) - since
        timestamp = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        filters.append(f'timestamp>="{timestamp}"')

    if revision:
        filters.append(f'resource.labels.revision_name="{revision}"')

    if pattern:
        # Escape quotes in pattern
        escaped_pattern = pattern.replace('"', '\\"')
        filters.append(f'jsonPayload.message=~"{escaped_pattern}"')

    if http_only:
        filters.append('httpRequest.requestUrl!=""')

    return " AND ".join(filters)


def _parse_log_entry(entry: dict, service: str) -> LogEntry:
    """Parse a raw log entry into a LogEntry object."""
    # Extract timestamp
    timestamp = entry.get("timestamp", entry.get("receiveTimestamp", ""))

    # Extract severity
    severity = entry.get("severity", "DEFAULT")

    # Extract message - try multiple locations
    message = ""
    json_payload = entry.get("jsonPayload", {})
    text_payload = entry.get("textPayload", "")

    if isinstance(json_payload, dict):
        message = json_payload.get("message", "")
        if not message:
            # Try other common fields
            message = json_payload.get("msg", "")
        if not message:
            # Try to get the whole payload as string
            message = json.dumps(json_payload, default=str)
    elif text_payload:
        message = text_payload

    # Check for HTTP request info
    http_request = entry.get("httpRequest", {})
    if http_request and not message:
        status = http_request.get("status", "")
        method = http_request.get("requestMethod", "")
        url = http_request.get("requestUrl", "")
        message = f"{method} {url} -> {status}"

    # Extract revision
    resource = entry.get("resource", {})
    labels = resource.get("labels", {})
    revision = labels.get("revision_name")

    # Extract trace ID
    trace_id = entry.get("trace", "")
    if trace_id and "/" in trace_id:
        trace_id = trace_id.split("/")[-1]

    return LogEntry(
        timestamp=timestamp,
        severity=severity,
        message=message,
        service=service,
        revision=revision,
        trace_id=trace_id,
        raw=entry,
    )


def fetch_logs(
    env: str,
    service: str = DEFAULT_SERVICE,
    severity: str | None = None,
    since: timedelta | None = None,
    limit: int = 50,
    pattern: str | None = None,
    http_only: bool = False,
    revision: str | None = None,
    since_deployment: bool = False,
) -> LogResult:
    """Fetch logs from Google Cloud Logging.

    Args:
        env: Environment name (dev, prod, development, production)
        service: Cloud Run service name
        severity: Minimum severity level (ERROR, WARNING, INFO, DEBUG)
        since: Time duration to look back (e.g., timedelta(hours=1))
        limit: Maximum number of log entries to return
        pattern: Regex pattern to filter messages
        http_only: Only show HTTP request logs
        revision: Specific revision to filter by
        since_deployment: If True, get logs since the most recent deployment

    Returns:
        LogResult with log entries and metadata
    """
    try:
        project = get_project_for_env(env)
    except ValueError as e:
        return LogResult(success=False, entries=[], error=str(e))

    # Handle since_deployment option
    if since_deployment:
        deploy_time = get_recent_deployment_time(project, service)
        if deploy_time:
            since = datetime.now(timezone.utc) - deploy_time
            # Add a small buffer (5 minutes before deployment)
            since = since + timedelta(minutes=5)
        else:
            # Fall back to 1 hour if we can't get deployment time
            since = timedelta(hours=1)

    # Build the filter
    log_filter = _build_log_filter(
        service=service,
        severity=severity,
        since=since,
        revision=revision,
        pattern=pattern,
        http_only=http_only,
    )

    # Build gcloud command
    cmd = [
        "gcloud",
        "logging",
        "read",
        log_filter,
        f"--project={project}",
        f"--limit={limit}",
        "--format=json",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() or "Unknown error"
            return LogResult(
                success=False,
                entries=[],
                error=f"gcloud command failed: {error_msg}",
                query_used=log_filter,
            )

        # Parse JSON output
        output = result.stdout.strip()
        if not output or output == "[]":
            return LogResult(
                success=True,
                entries=[],
                query_used=log_filter,
                count=0,
            )

        try:
            raw_entries = json.loads(output)
        except json.JSONDecodeError as e:
            return LogResult(
                success=False,
                entries=[],
                error=f"Failed to parse log output: {e}",
                query_used=log_filter,
            )

        # Parse entries
        entries = [_parse_log_entry(entry, service) for entry in raw_entries]

        return LogResult(
            success=True,
            entries=entries,
            query_used=log_filter,
            count=len(entries),
        )

    except subprocess.TimeoutExpired:
        return LogResult(
            success=False,
            entries=[],
            error="gcloud command timed out after 60 seconds",
            query_used=log_filter,
        )
    except subprocess.SubprocessError as e:
        return LogResult(
            success=False,
            entries=[],
            error=f"Failed to run gcloud: {e}",
            query_used=log_filter,
        )


def get_service_status(env: str, service: str = DEFAULT_SERVICE) -> dict[str, Any]:
    """Get the current status of a Cloud Run service."""
    try:
        project = get_project_for_env(env)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    try:
        result = subprocess.run(
            [
                "gcloud",
                "run",
                "services",
                "describe",
                service,
                "--region=us-central1",
                f"--project={project}",
                "--format=json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            return {
                "success": False,
                "error": result.stderr.strip() or "Failed to get service status",
            }

        data = json.loads(result.stdout)
        status = data.get("status", {})
        conditions = status.get("conditions", [])

        return {
            "success": True,
            "service": service,
            "url": status.get("url", ""),
            "latest_revision": status.get("latestReadyRevisionName", ""),
            "conditions": [
                {"type": c.get("type"), "status": c.get("status")}
                for c in conditions
            ],
        }

    except (subprocess.TimeoutExpired, subprocess.SubprocessError, json.JSONDecodeError) as e:
        return {"success": False, "error": str(e)}
