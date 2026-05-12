#!/usr/bin/env python3
"""Cloud Logs CLI tool.

Simple access to Google Cloud Run logs with smart defaults.

Usage:
    cloud-logs errors                    # Errors from dev since last deployment
    cloud-logs errors --env prod         # Errors from production
    cloud-logs recent                    # Recent logs from dev
    cloud-logs recent --since 30m        # Last 30 minutes
    cloud-logs http                      # HTTP request logs
    cloud-logs search "pattern"          # Search for pattern in logs
    cloud-logs status                    # Service status
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from rich.console import Console
from rich.table import Table
from rich.text import Text

from .log_fetcher import (
    DEFAULT_SERVICE,
    ENV_ALIASES,
    ENVIRONMENTS,
    SERVICES,
    LogEntry,
    LogResult,
    fetch_logs,
    get_service_status,
    normalize_env,
    parse_duration,
)

console = Console()


def _severity_style(severity: str) -> str:
    """Get Rich style for severity level."""
    styles = {
        "ERROR": "bold red",
        "WARNING": "yellow",
        "INFO": "green",
        "DEBUG": "dim",
        "DEFAULT": "white",
    }
    return styles.get(severity.upper(), "white")


def _format_timestamp(ts: str) -> str:
    """Format timestamp for display."""
    if not ts:
        return ""
    # Truncate to readable format
    if "T" in ts:
        parts = ts.replace("Z", "").split("T")
        if len(parts) == 2:
            date_part = parts[0][5:]  # Remove year prefix
            time_part = parts[1][:12]  # HH:MM:SS.mmm
            return f"{date_part} {time_part}"
    return ts[:19] if len(ts) > 19 else ts


def _truncate_message(msg: str, max_len: int = 120) -> str:
    """Truncate message for table display."""
    if not msg:
        return ""
    # Remove newlines for table
    msg = msg.replace("\n", " ").replace("\r", "")
    if len(msg) > max_len:
        return msg[: max_len - 3] + "..."
    return msg


def _print_logs_table(entries: list[LogEntry], show_revision: bool = False) -> None:
    """Print logs in a formatted table."""
    if not entries:
        console.print("[yellow]No logs found matching the criteria.[/yellow]")
        return

    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Time", style="dim", width=18)
    table.add_column("Sev", width=7)
    if show_revision:
        table.add_column("Revision", width=25)
    table.add_column("Message", overflow="fold")

    for entry in entries:
        severity_text = Text(entry.severity[:7], style=_severity_style(entry.severity))
        time_str = _format_timestamp(entry.timestamp)
        msg = _truncate_message(entry.message)

        if show_revision:
            rev = entry.revision or ""
            if len(rev) > 25:
                rev = "..." + rev[-22:]
            table.add_row(time_str, severity_text, rev, msg)
        else:
            table.add_row(time_str, severity_text, msg)

    console.print(table)


def _print_logs_raw(entries: list[LogEntry]) -> None:
    """Print logs in raw format (one per line)."""
    for entry in entries:
        severity_style = _severity_style(entry.severity)
        console.print(
            f"[dim]{_format_timestamp(entry.timestamp)}[/dim] "
            f"[{severity_style}]{entry.severity}[/{severity_style}] "
            f"{entry.message}"
        )


def _print_result_summary(result: LogResult, env: str) -> None:
    """Print summary of the log query."""
    env_name = normalize_env(env)
    console.print(f"\n[dim]Environment: {env_name} | Found: {result.count} entries[/dim]")
    if result.query_used:
        console.print(
            f"[dim]Filter: {result.query_used[:100]}...[/dim]"
            if len(result.query_used) > 100
            else f"[dim]Filter: {result.query_used}[/dim]"
        )


def cmd_errors(args: argparse.Namespace) -> int:
    """Get error logs - the primary use case."""
    since = None
    if args.since:
        try:
            since = parse_duration(args.since)
        except ValueError as e:
            console.print(f"[red]Error: {e}[/red]")
            return 1

    console.print(
        f"[cyan]Fetching errors from {args.service} ({normalize_env(args.env)})...[/cyan]"
    )

    result = fetch_logs(
        env=args.env,
        service=args.service,
        severity="ERROR",
        since=since,
        limit=args.limit,
        since_deployment=not args.since,  # Use deployment time if no --since specified
    )

    if not result.success:
        console.print(f"[red]Error: {result.error}[/red]")
        return 1

    if args.format == "raw":
        _print_logs_raw(result.entries)
    else:
        _print_logs_table(result.entries, show_revision=args.revision)

    _print_result_summary(result, args.env)
    return 0


def cmd_recent(args: argparse.Namespace) -> int:
    """Get recent logs (all severities)."""
    since = timedelta(hours=1)  # Default to 1 hour
    if args.since:
        try:
            since = parse_duration(args.since)
        except ValueError as e:
            console.print(f"[red]Error: {e}[/red]")
            return 1

    console.print(
        f"[cyan]Fetching recent logs from {args.service} ({normalize_env(args.env)})...[/cyan]"
    )

    result = fetch_logs(
        env=args.env,
        service=args.service,
        severity=args.severity,
        since=since,
        limit=args.limit,
    )

    if not result.success:
        console.print(f"[red]Error: {result.error}[/red]")
        return 1

    if args.format == "raw":
        _print_logs_raw(result.entries)
    else:
        _print_logs_table(result.entries, show_revision=args.revision)

    _print_result_summary(result, args.env)
    return 0


def cmd_http(args: argparse.Namespace) -> int:
    """Get HTTP request logs."""
    since = timedelta(hours=1)
    if args.since:
        try:
            since = parse_duration(args.since)
        except ValueError as e:
            console.print(f"[red]Error: {e}[/red]")
            return 1

    console.print(
        f"[cyan]Fetching HTTP logs from {args.service} ({normalize_env(args.env)})...[/cyan]"
    )

    result = fetch_logs(
        env=args.env,
        service=args.service,
        since=since,
        limit=args.limit,
        http_only=True,
    )

    if not result.success:
        console.print(f"[red]Error: {result.error}[/red]")
        return 1

    if args.format == "raw":
        _print_logs_raw(result.entries)
    else:
        _print_logs_table(result.entries)

    _print_result_summary(result, args.env)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Search logs for a pattern."""
    since = timedelta(hours=1)
    if args.since:
        try:
            since = parse_duration(args.since)
        except ValueError as e:
            console.print(f"[red]Error: {e}[/red]")
            return 1

    console.print(
        f"[cyan]Searching for '{args.pattern}' in {args.service} ({normalize_env(args.env)})...[/cyan]"
    )

    result = fetch_logs(
        env=args.env,
        service=args.service,
        since=since,
        limit=args.limit,
        pattern=args.pattern,
        severity=args.severity,
    )

    if not result.success:
        console.print(f"[red]Error: {result.error}[/red]")
        return 1

    if args.format == "raw":
        _print_logs_raw(result.entries)
    else:
        _print_logs_table(result.entries)

    _print_result_summary(result, args.env)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Get service status."""
    console.print(f"[cyan]Getting status for {args.service} ({normalize_env(args.env)})...[/cyan]")

    status = get_service_status(args.env, args.service)

    if not status.get("success"):
        console.print(f"[red]Error: {status.get('error')}[/red]")
        return 1

    console.print(f"\n[bold]Service:[/bold] {status['service']}")
    console.print(f"[bold]URL:[/bold] {status['url']}")
    console.print(f"[bold]Latest Revision:[/bold] {status['latest_revision']}")

    if status.get("conditions"):
        console.print("\n[bold]Conditions:[/bold]")
        for cond in status["conditions"]:
            status_style = "green" if cond["status"] == "True" else "red"
            console.print(f"  {cond['type']}: [{status_style}]{cond['status']}[/{status_style}]")

    return 0


def cmd_services(args: argparse.Namespace) -> int:
    """List available services."""
    console.print("[bold]Available services:[/bold]\n")
    for svc, desc in SERVICES.items():
        default = " (default)" if svc == DEFAULT_SERVICE else ""
        console.print(f"  [cyan]{svc}[/cyan]{default}")
        console.print(f"    {desc}")
    return 0


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add common arguments to a parser."""
    parser.add_argument(
        "--env",
        "-e",
        choices=list(ENVIRONMENTS.keys()) + list(ENV_ALIASES.keys()),
        default="development",
        help="Target environment (default: dev)",
    )
    parser.add_argument(
        "--service",
        "-s",
        choices=list(SERVICES.keys()),
        default=DEFAULT_SERVICE,
        help=f"Cloud Run service (default: {DEFAULT_SERVICE})",
    )
    parser.add_argument(
        "--format",
        "-f",
        choices=["table", "raw"],
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--limit",
        "-n",
        type=int,
        default=50,
        help="Max entries to return (default: 50)",
    )
    parser.add_argument(
        "--revision",
        "-r",
        action="store_true",
        help="Show revision column in output",
    )


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="cloud-logs",
        description="Google Cloud Run Log Viewer - Simple log access with smart defaults",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Quick Examples:
  cloud-logs errors                    Get errors since last deployment (dev)
  cloud-logs errors --env prod         Get errors from production
  cloud-logs recent --since 30m        Recent logs from last 30 minutes
  cloud-logs search "error"            Search for pattern
  cloud-logs status                    Check service health

Environments: dev (default), prod
Services: pax-chat (default), pax-widget-proxy, pax-google-chat-proxy
Duration: 30m (minutes), 2h (hours), 1d (days)
        """,
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # errors command
    errors_parser = subparsers.add_parser(
        "errors",
        help="Get error logs (default: since last deployment)",
    )
    _add_common_args(errors_parser)
    errors_parser.add_argument(
        "--since",
        help="Time to look back (e.g., 1h, 30m, 2d). Default: since last deployment",
    )
    errors_parser.set_defaults(func=cmd_errors)

    # recent command
    recent_parser = subparsers.add_parser(
        "recent",
        help="Get recent logs",
    )
    _add_common_args(recent_parser)
    recent_parser.add_argument(
        "--since",
        default="1h",
        help="Time to look back (default: 1h)",
    )
    recent_parser.add_argument(
        "--severity",
        choices=["ERROR", "WARNING", "INFO", "DEBUG"],
        help="Minimum severity level",
    )
    recent_parser.set_defaults(func=cmd_recent)

    # http command
    http_parser = subparsers.add_parser(
        "http",
        help="Get HTTP request logs",
    )
    _add_common_args(http_parser)
    http_parser.add_argument(
        "--since",
        default="1h",
        help="Time to look back (default: 1h)",
    )
    http_parser.set_defaults(func=cmd_http)

    # search command
    search_parser = subparsers.add_parser(
        "search",
        help="Search logs for a pattern",
    )
    _add_common_args(search_parser)
    search_parser.add_argument(
        "pattern",
        help="Regex pattern to search for",
    )
    search_parser.add_argument(
        "--since",
        default="1h",
        help="Time to look back (default: 1h)",
    )
    search_parser.add_argument(
        "--severity",
        choices=["ERROR", "WARNING", "INFO", "DEBUG"],
        help="Minimum severity level",
    )
    search_parser.set_defaults(func=cmd_search)

    # status command
    status_parser = subparsers.add_parser(
        "status",
        help="Get service status",
    )
    _add_common_args(status_parser)
    status_parser.set_defaults(func=cmd_status)

    # services command
    services_parser = subparsers.add_parser(
        "services",
        help="List available services",
    )
    services_parser.set_defaults(func=cmd_services)

    return parser


def main(argv: list[str] | None = None) -> None:
    """Main entry point."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        # Default to errors command
        args.command = "errors"
        args.since = None
        args.func = cmd_errors

    # Run the subcommand
    exit_code = args.func(args)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
