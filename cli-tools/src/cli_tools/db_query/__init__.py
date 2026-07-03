"""Database query utilities for Cloud SQL access."""

from .query_runner import (
    ENV_ALIASES,
    ENVIRONMENTS,
    QueryResult,
    describe_table,
    execute_query,
    get_env_config,
    get_password,
    list_tables,
    normalize_env,
    resolve_password,
    validate_query,
)


def main(*args, **kwargs):
    """Run the db-query CLI entry point without eager-importing the CLI module."""
    from .cli import main as _main

    return _main(*args, **kwargs)


__all__ = [
    "ENVIRONMENTS",
    "ENV_ALIASES",
    "QueryResult",
    "describe_table",
    "execute_query",
    "get_env_config",
    "get_password",
    "list_tables",
    "main",
    "normalize_env",
    "resolve_password",
    "validate_query",
]
