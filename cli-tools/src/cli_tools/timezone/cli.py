"""CLI tool for converting source timezone times to the local system timezone."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import re

import click
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TIME_COLON_PATTERN = re.compile(r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})$")
TIME_DIGITS_PATTERN = re.compile(r"^(?P<digits>\d{1,4})$")

TZ_SHORTHANDS = {
    "UTC": "UTC",
    "GMT": "Etc/GMT",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "CET": "Europe/Paris",
    "CEST": "Europe/Berlin",
}


@dataclass(frozen=True)
class ParsedTime:
    hour: int
    minute: int


def parse_time(value: str) -> ParsedTime:
    """Parse supported time formats like 8, 830, 15:30, or 1530."""

    value = value.strip()
    colon_match = TIME_COLON_PATTERN.match(value)
    if colon_match:
        hour = int(colon_match.group("hour"))
        minute = int(colon_match.group("minute"))
        return _validate_time(hour, minute)

    digits_match = TIME_DIGITS_PATTERN.match(value)
    if digits_match:
        digits = digits_match.group("digits")
        if len(digits) <= 2:
            return _validate_time(int(digits), 0)
        if len(digits) == 3:
            return _validate_time(int(digits[0]), int(digits[1:]))
        return _validate_time(int(digits[:2]), int(digits[2:]))

    raise click.BadParameter(
        "Unsupported time format. Use HH:MM, H:MM, HMM, or HHMM (24h).",
        param_hint="time",
    )


def _validate_time(hour: int, minute: int) -> ParsedTime:
    if not 0 <= hour <= 23:
        raise click.BadParameter("Hour must be between 0 and 23.", param_hint="time")
    if not 0 <= minute <= 59:
        raise click.BadParameter("Minute must be between 0 and 59.", param_hint="time")
    return ParsedTime(hour=hour, minute=minute)


def resolve_timezone(tz_name: str) -> ZoneInfo:
    """Return a ZoneInfo, allowing common shorthands like UTC/PST."""

    normalized = tz_name.strip()
    try:
        return ZoneInfo(normalized)
    except ZoneInfoNotFoundError:
        pass

    alias = TZ_SHORTHANDS.get(normalized.upper())
    if alias:
        return ZoneInfo(alias)

    raise ZoneInfoNotFoundError(normalized)


def build_source_datetime(parsed: ParsedTime, tz_name: str, anchor: date) -> datetime:
    try:
        source_tz = resolve_timezone(tz_name)
    except ZoneInfoNotFoundError as exc:  # pragma: no cover - depends on system data
        raise click.BadParameter(f"Unknown timezone: {tz_name}", param_hint="timezone") from exc

    return datetime(
        anchor.year, anchor.month, anchor.day, parsed.hour, parsed.minute, tzinfo=source_tz
    )


def format_timezone(tzinfo) -> str:
    if tzinfo is None:
        return "Unknown"
    key = getattr(tzinfo, "key", None)
    if key:
        return key
    name = tzinfo.tzname(None)
    return name if name else str(tzinfo)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("time", metavar="TIME")
@click.argument("timezone", metavar="SOURCE_TZ")
@click.option(
    "--date",
    "anchor_date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    help="Anchor date in YYYY-MM-DD (defaults to today).",
)
@click.option(
    "--output-format",
    default="%Y-%m-%d %I:%M %p %Z",
    show_default=True,
    help="strftime pattern for the local time output.",
)
@click.option(
    "--quiet/--verbose",
    default=True,
    show_default=True,
    help="Quiet prints just the converted time; verbose shows extra context.",
)
def main(
    time: str, timezone: str, anchor_date: datetime | None, output_format: str, quiet: bool
) -> None:
    """Convert TIME in SOURCE_TZ to the system's local timezone."""

    parsed = parse_time(time)
    anchor = anchor_date.date() if anchor_date else date.today()
    source_dt = build_source_datetime(parsed, timezone, anchor)
    local_dt = source_dt.astimezone()

    if quiet:
        click.echo(local_dt.strftime(output_format))
        return

    local_label = format_timezone(local_dt.tzinfo)
    source_label = format_timezone(source_dt.tzinfo)

    click.echo(f"Source ({source_label}): {source_dt.strftime('%Y-%m-%d %I:%M %p %Z (%z)')}")
    click.echo(f" Local ({local_label}): {local_dt.strftime('%Y-%m-%d %I:%M %p %Z (%z)')}")


if __name__ == "__main__":  # pragma: no cover
    main()
