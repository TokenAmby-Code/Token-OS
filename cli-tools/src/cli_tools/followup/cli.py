#!/usr/bin/env python3
"""Follow-up creation CLI.

Create one-shot and recurring follow-up jobs via Token-API cron.

Usage:
    followup create "check migration status" --at +4h
    followup create "fix PATH issue" --at +2h --route cc
    followup create "monitor errors" --recurring --every 2h --expires 7d
    followup list
    followup cancel followup-check-migration-0214-1800
"""

from __future__ import annotations

import argparse
import sys

from .dispatcher import cancel_followup, create_oneshot, create_recurring, list_followups


def cmd_create(args: argparse.Namespace) -> int:
    """Create a follow-up job."""
    if args.recurring:
        if not args.every and not args.cron:
            print("Error: recurring jobs require --every or --cron", file=sys.stderr)
            return 1
        return create_recurring(
            prompt=args.prompt,
            every=args.every,
            cron=args.cron,
            name=args.name,
            route=args.route,
            expires=args.expires,
            announce=args.announce,
            channel=args.channel,
        )
    else:
        if not args.at:
            print("Error: one-shot jobs require --at (e.g. --at +4h or --at '2026-02-17 09:00')", file=sys.stderr)
            return 1
        return create_oneshot(
            prompt=args.prompt,
            at=args.at,
            name=args.name,
            route=args.route,
            announce=args.announce,
            channel=args.channel,
        )


def cmd_list(args: argparse.Namespace) -> int:
    """List active follow-ups."""
    return list_followups()


def cmd_cancel(args: argparse.Namespace) -> int:
    """Cancel a follow-up job."""
    return cancel_followup(args.name)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="followup",
        description="Create and manage follow-up jobs via Token-API cron",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  followup create "review checkin system performance" --at "2026-02-17 09:00"
  followup create "check if migration completed" --at +4h
  followup create "fix PATH issue" --at +2h --route cc
  followup create "monitor error rates" --recurring --every 2h --expires 7d
  followup create "check health" --recurring --cron "0 9 * * 1-5" --expires 14d
  followup list
  followup cancel followup-check-health-0214-0900
""",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # create subcommand
    create_parser = subparsers.add_parser("create", help="Create a follow-up job")
    create_parser.add_argument("prompt", help="Task description for the agent")
    create_parser.add_argument(
        "--at",
        help="When to run (ISO time or +duration like +4h, +30m)",
    )
    create_parser.add_argument(
        "--recurring",
        action="store_true",
        help="Create a recurring job instead of one-shot",
    )
    create_parser.add_argument(
        "--every",
        help="Interval for recurring jobs (e.g. 2h, 30m, 1d)",
    )
    create_parser.add_argument(
        "--cron",
        help="Cron expression for recurring jobs (5-field)",
    )
    create_parser.add_argument(
        "--expires",
        help="Auto-expire after duration (e.g. 7d, 24h) — recurring only",
    )
    create_parser.add_argument(
        "--route",
        choices=["minimax", "cc"],
        default="minimax",
        help="Routing: minimax (default) for research/checks, cc for Claude Code implementation",
    )
    create_parser.add_argument(
        "--name",
        help="Custom job name (auto-generated if omitted)",
    )
    create_parser.add_argument(
        "--announce",
        action="store_true",
        help="Post results to Discord",
    )
    create_parser.add_argument(
        "--channel",
        help="Discord channel for announcements",
    )
    create_parser.set_defaults(func=cmd_create)

    # list subcommand
    list_parser = subparsers.add_parser("list", help="List active follow-ups")
    list_parser.set_defaults(func=cmd_list)

    # cancel subcommand
    cancel_parser = subparsers.add_parser("cancel", help="Cancel a follow-up job")
    cancel_parser.add_argument("name", help="Job name to cancel")
    cancel_parser.set_defaults(func=cmd_cancel)

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
