#!/usr/bin/env python3
"""Database query CLI tool.

Secure database access for Cloud SQL with proxy and direct connection support.

Usage:
    db-query --env dev tables
    db-query --env dev describe users
    db-query --env dev --direct query "SELECT * FROM users LIMIT 5"
    db-query --env dev proxy status
    db-query --env dev proxy start
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from .query_runner import (
    DEFAULT_LIMIT,
    ENV_ALIASES,
    ENVIRONMENTS,
    QueryResult,
    add_limit_if_missing,
    check_proxy_status,
    describe_table,
    execute_query,
    format_results_json,
    format_results_table,
    get_env_config,
    is_write_query,
    list_tables,
    normalize_env,
    resolve_password,
    start_proxy,
    validate_query,
)


def _print_result(result: QueryResult, format_type: str = "table") -> None:
    """Print query result in the specified format."""
    if not result.success:
        print(f"Error: {result.error}")
        return

    if not result.rows or not result.columns:
        print("No results returned.")
        return

    if format_type == "json":
        print(format_results_json(result.columns, result.rows))
    else:
        print(format_results_table(result.columns, result.rows))


def cmd_tables(args: argparse.Namespace) -> int:
    """List all tables in the database."""
    env_config = _get_config_with_overrides(args)
    password = _get_password_or_exit(env_config)

    env_name = normalize_env(args.env)
    print(f"Listing tables in {env_name} ({env_config['database']})...\n")

    result = asyncio.run(list_tables(env_config, password))
    _print_result(result, args.format)

    return 0 if result.success else 1


def cmd_describe(args: argparse.Namespace) -> int:
    """Describe a table's structure."""
    env_config = _get_config_with_overrides(args)
    password = _get_password_or_exit(env_config)

    env_name = normalize_env(args.env)
    print(f"Describing table '{args.table}' in {env_name}...\n")

    result = asyncio.run(describe_table(env_config, args.table, password))
    _print_result(result, args.format)

    return 0 if result.success else 1


def cmd_query(args: argparse.Namespace) -> int:
    """Execute a SQL query."""
    env_config = _get_config_with_overrides(args)
    password = _get_password_or_exit(env_config)

    # Validate query
    is_valid, error = validate_query(args.sql, env_config)
    if not is_valid:
        print(f"Error: {error}")
        return 1

    # Check if write query requires --write flag
    if is_write_query(args.sql) and not getattr(args, "write", False):
        print("Error: Write operations require the --write flag.")
        print("This is a safety measure to prevent accidental data modifications.")
        print('\nUsage: db-query --env dev query --write "INSERT INTO ..."')
        print("\nNote: Production is always read-only regardless of flags.")
        return 1

    # Add limit if needed
    query = args.sql
    if not args.no_limit:
        query = add_limit_if_missing(query)

    # Show environment info
    env_name = normalize_env(args.env)
    access = "READ-ONLY" if env_config["read_only"] else "read/write"
    print(f"Environment: {env_name} ({access})")
    print(f"Database: {env_config['database']}")
    print(f"Query: {query}\n")

    result = asyncio.run(execute_query(env_config, query, password))
    _print_result(result, args.format)

    return 0 if result.success else 1


def cmd_proxy(args: argparse.Namespace) -> int:
    """Manage Cloud SQL Auth Proxy."""
    env_config = _get_config_with_overrides(args)

    if args.proxy_action == "status":
        status = check_proxy_status(env_config)
        if status["running"]:
            print(f"Proxy is RUNNING on port {status['port']}")
            print(f"Instance: {status['instance']}")
        else:
            print(f"Proxy is NOT running on port {status['port']}")
            print(f"Instance: {status['instance']}")
            print("\nStart it with: db-query --env {env} proxy start")
        return 0

    elif args.proxy_action == "start":
        result = start_proxy(env_config, background=not args.foreground)
        print(result["message"])
        return 0 if result["success"] else 1

    else:
        print(f"Unknown proxy action: {args.proxy_action}")
        return 1


def _get_config_with_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """Get environment config with command-line overrides applied."""
    env_config = get_env_config(args.env)

    # Handle direct connection via public IP
    if getattr(args, "direct", False):
        public_ip = env_config.get("public_ip")
        if not public_ip:
            env_name = normalize_env(args.env)
            print(f"Error: Direct connection not available for {env_name}.")
            print("Direct connections are only configured for development environment.")
            sys.exit(1)
        env_config["host"] = public_ip
        print(f"Using direct connection to {public_ip} (no proxy)")

    # Override port if specified
    if getattr(args, "port", None):
        env_config["port"] = args.port

    # Override database name if specified
    if getattr(args, "db", None):
        env_config["database"] = args.db

    # Override Cloud SQL instance connection name if specified
    if getattr(args, "instance", None):
        env_config["instance"] = args.instance
        if ":" in args.instance:
            env_config["project_id"] = args.instance.split(":", 1)[0]

    # Override host if specified
    if getattr(args, "host", None):
        env_config["host"] = args.host

    return env_config


def _get_password_or_exit(env_config: dict[str, Any]) -> str:
    """Resolve password or exit loudly; never attempt passwordless auth."""
    result = resolve_password(env_config)
    if not result.password:
        print("Error: Database password required.")
        if result.error:
            print(result.error)
        print(
            "Resolution order: DB_PASSWORD, .env DB_PASSWORD, then GCP Secret Manager db-password."
        )
        print(
            "For Secret Manager, authenticate gcloud and ensure secretAccessor on the target project."
        )
        sys.exit(1)

    return result.password


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="db-query",
        description="Secure Database Query Tool for Cloud SQL",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  db-query --env dev tables
  db-query --env dev describe users
  db-query --env dev --direct query "SELECT * FROM users LIMIT 5"
  db-query --env prod --db pax-sql query "SELECT 1"
  db-query --env prod --instance pax-prod-467920:us-central1:pax-sql query "SELECT 1"
  db-query --env production query "SELECT COUNT(*) FROM tickets"
  db-query --env dev proxy status
  db-query --env dev proxy start

Defaults:
  dev         pax-dev-469018:us-central1:pax-sql      database pax-sql
  staging     pax-staging-008732:us-central1:pax-sql  database pax-db-staging
  production  pax-prod-467920:us-central1:pax-sql     database pax-sql (read-only)

Deploy YAMLs, when present, override these defaults. --db and --instance can
override the final target explicitly.

Environment Variables:
  DB_PASSWORD     Database password (loaded from .env if not set)

Password Resolution:
  DB_PASSWORD → .env DB_PASSWORD → GCP Secret Manager db-password in the target
  project (derived from INSTANCE_CONNECTION_NAME/--instance). If none resolves,
  db-query exits before connecting; it never silently attempts passwordless auth.
        """,
    )

    # Global options
    parser.add_argument(
        "--env",
        "-e",
        choices=list(ENVIRONMENTS.keys()) + list(ENV_ALIASES.keys()),
        default="development",
        help="Target environment (default: development)",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Use direct connection via public IP (dev only, no proxy needed)",
    )
    parser.add_argument(
        "--format",
        "-f",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        help="Override proxy port (default: 5432)",
    )
    parser.add_argument(
        "--db",
        type=str,
        help="Override database name (default: pax-sql for dev/prod, pax-db-staging for staging)",
    )
    parser.add_argument(
        "--instance",
        type=str,
        help="Override Cloud SQL instance connection name (project:region:instance)",
    )
    parser.add_argument(
        "--host",
        type=str,
        help="Override database host",
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # tables command
    tables_parser = subparsers.add_parser(
        "tables",
        help="List all tables in the database",
    )
    tables_parser.set_defaults(func=cmd_tables)

    # describe command
    describe_parser = subparsers.add_parser(
        "describe",
        help="Describe a table's structure",
    )
    describe_parser.add_argument(
        "table",
        help="Table name to describe",
    )
    describe_parser.set_defaults(func=cmd_describe)

    # query command
    query_parser = subparsers.add_parser(
        "query",
        help="Execute a SQL query",
    )
    query_parser.add_argument(
        "sql",
        help="SQL query to execute",
    )
    query_parser.add_argument(
        "--no-limit",
        action="store_true",
        help=f"Don't auto-add LIMIT {DEFAULT_LIMIT} to SELECT queries",
    )
    query_parser.add_argument(
        "--write",
        action="store_true",
        help="Allow write operations (INSERT, UPDATE, DELETE, etc.) - required for migrations",
    )
    query_parser.set_defaults(func=cmd_query)

    # proxy command
    proxy_parser = subparsers.add_parser(
        "proxy",
        help="Manage Cloud SQL Auth Proxy",
    )
    proxy_parser.add_argument(
        "proxy_action",
        choices=["status", "start"],
        help="Proxy action to perform",
    )
    proxy_parser.add_argument(
        "--foreground",
        action="store_true",
        help="Run proxy in foreground (blocking)",
    )
    proxy_parser.set_defaults(func=cmd_proxy)

    return parser


def main(argv: list[str] | None = None) -> None:
    """Main entry point."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Run the subcommand
    exit_code = args.func(args)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
