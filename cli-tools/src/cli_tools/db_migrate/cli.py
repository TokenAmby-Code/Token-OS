#!/usr/bin/env python3
"""Database migration CLI tool.

Run SQL migrations against Cloud SQL environments via the Cloud SQL Python
Connector. No proxy, no public IP management — just direct secure connections.

Usage:
    db-migrate apply migration.sql --env dev
    db-migrate apply migration.sql --env prod
    db-migrate apply migration.sql --env dev --dry-run
    db-migrate preview migration.sql
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from cli_tools.db_query.query_runner import (
    ENVIRONMENTS,
    ENV_ALIASES,
    get_env_config,
    get_password,
    normalize_env,
)

from .migration_runner import MigrationResult, run_migration, run_verify_query


def _read_sql_file(path: str) -> str:
    """Read and return SQL file contents."""
    sql_path = Path(path)
    if not sql_path.exists():
        print(f"Error: File not found: {path}")
        sys.exit(1)
    if not sql_path.suffix.lower() == ".sql":
        print(f"Warning: File does not have .sql extension: {path}")
    return sql_path.read_text()


def _print_sql_preview(sql_content: str, file_path: str) -> None:
    """Display SQL file content with metadata."""
    lines = sql_content.strip().splitlines()
    print(f"File: {file_path}")
    print(f"Lines: {len(lines)}")
    print(f"Size: {len(sql_content)} bytes")
    print("-" * 60)
    print(sql_content.strip())
    print("-" * 60)


def _print_result(result: MigrationResult) -> None:
    """Display migration result."""
    if result.success:
        print(f"\nMigration completed successfully in {result.duration_ms:.0f}ms")
    else:
        print(f"\nMigration FAILED after {result.duration_ms:.0f}ms")
        print(f"Error: {result.error}")

    if result.messages:
        print("\nLog:")
        for msg in result.messages:
            print(f"  {msg}")


def cmd_preview(args: argparse.Namespace) -> int:
    """Show SQL file contents without executing."""
    sql_content = _read_sql_file(args.file)
    _print_sql_preview(sql_content, args.file)
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    """Apply a SQL migration file."""
    sql_content = _read_sql_file(args.file)
    env_name = normalize_env(args.env)
    env_config = get_env_config(args.env)
    is_prod = env_name == "production"

    # Show what we're about to do
    print(f"Environment: {env_name}")
    print(f"Database: {env_config['database']}")
    if args.dry_run:
        print("Mode: DRY RUN (will rollback)")
    print()
    _print_sql_preview(sql_content, args.file)
    print()

    # Production requires explicit confirmation
    if is_prod:
        print("=" * 60)
        print("  PRODUCTION MIGRATION")
        print("  This will modify the production database.")
        if args.dry_run:
            print("  (dry-run: changes will be rolled back)")
        print("=" * 60)
        print()
        if getattr(args, "pre_approved", False):
            print("Proceeding (--pre-approved flag set by user instruction).")
        else:
            confirmation = input("Type YES to proceed: ")
            if confirmation != "YES":
                print("Aborted.")
                return 1
        print()

    # Get password
    password = get_password()
    if not password:
        print("Error: DB_PASSWORD required. Set in environment or .env file.")
        return 1

    # Run migration via Cloud SQL connector (all environments)
    verify_sql = getattr(args, "verify", None)

    result = asyncio.run(
        run_migration(env_config, sql_content, password, args.dry_run, use_connector=True)
    )

    # Run verification if requested and migration succeeded
    if verify_sql and result.success and not args.dry_run:
        print("\n  Running verification query...")
        rows = asyncio.run(run_verify_query(env_config, verify_sql, password, use_connector=True))
        if rows:
            result.messages.append(f"Verification: {len(rows)} row(s) found")
            for row in rows:
                result.messages.append(f"  {row}")
        else:
            result.messages.append("Verification: no rows returned")

    _print_result(result)
    return 0 if result.success else 1


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="db-migrate",
        description="Database Migration Tool for Cloud SQL",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  db-migrate preview migration.sql
  db-migrate apply migration.sql --env dev
  db-migrate apply migration.sql --env dev --dry-run
  db-migrate apply migration.sql --env prod

All connections use the Cloud SQL Python Connector with the active
gcloud account. No proxy or public IP management needed.

Production migrations require typing YES to confirm.
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # preview command
    preview_parser = subparsers.add_parser(
        "preview",
        help="Show SQL file contents without executing",
    )
    preview_parser.add_argument("file", help="Path to SQL migration file")
    preview_parser.set_defaults(func=cmd_preview)

    # apply command
    apply_parser = subparsers.add_parser(
        "apply",
        help="Apply a SQL migration file",
    )
    apply_parser.add_argument("file", help="Path to SQL migration file")
    apply_parser.add_argument(
        "--env",
        "-e",
        choices=list(ENVIRONMENTS.keys()) + list(ENV_ALIASES.keys()),
        default="development",
        help="Target environment (default: development)",
    )
    apply_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run migration in a transaction then rollback (no changes applied)",
    )
    apply_parser.add_argument(
        "--verify",
        type=str,
        default=None,
        help="SQL SELECT to run after migration to verify results",
    )
    apply_parser.add_argument(
        "--pre-approved",
        action="store_true",
        help="Skip interactive confirmation. Only use when explicitly instructed by the user — never autonomously.",
    )
    apply_parser.set_defaults(func=cmd_apply)

    return parser


def main(argv: list[str] | None = None) -> None:
    """Main entry point."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    exit_code = args.func(args)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
