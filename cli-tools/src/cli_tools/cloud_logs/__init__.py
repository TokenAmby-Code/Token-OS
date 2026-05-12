"""Cloud Logs CLI tool for Google Cloud Platform.

Provides easy access to Cloud Run logs with smart defaults for deployment debugging.
"""

from .log_fetcher import (
    ENV_ALIASES,
    ENVIRONMENTS,
    SERVICES,
    fetch_logs,
    get_latest_revision,
    get_project_for_env,
    get_recent_deployment_time,
    normalize_env,
    parse_duration,
)

__all__ = [
    "ENVIRONMENTS",
    "ENV_ALIASES",
    "SERVICES",
    "fetch_logs",
    "get_latest_revision",
    "get_project_for_env",
    "get_recent_deployment_time",
    "normalize_env",
    "parse_duration",
]
