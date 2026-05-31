"""Timer graph telemetry validation helpers.

The timer balance can only move at -1:1, 0:1, or +1:1 while normal samples are
flowing. Large deltas across sparse samples are physically possible, but the UI
must not render them as a continuous line: the graph truth is "telemetry gap",
not "instant correction".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

GAP_THRESHOLD_SECONDS = 90
# Graph anomaly detection is an alerting/display guard, not accounting. Keep
# enough slack for scheduler jitter, delayed writes, and integer rounding so
# normal penalized break burn doesn't fragment the graph into point soup.
RATE_TOLERANCE_MS = 30_000
MATERIAL_DELTA_MS = 5 * 60_000

RESET_DISCONTINUITY_TRIGGERS = {
    "daily_reset",
    "manual_reset",
    "timer_reset",
    "reset",
    "manual_set",
    "set_break",
    "gym_bounty",
}


def parse_timer_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def timer_event_is_reset_discontinuity(event: dict[str, Any]) -> bool:
    trigger = str(event.get("trigger") or event.get("event_type") or "").lower()
    return trigger in RESET_DISCONTINUITY_TRIGGERS


def _events_between(
    events: list[dict[str, Any]], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for event in events:
        ts = parse_timer_timestamp(event.get("timestamp") or event.get("created_at"))
        if ts is None:
            continue
        if start < ts <= end:
            found.append(event)
    return found


def annotate_timer_telemetry(
    points: list[dict[str, Any]],
    *,
    window_start: datetime,
    window_end: datetime,
    reset_events: list[dict[str, Any]] | None = None,
    gap_threshold_seconds: int = GAP_THRESHOLD_SECONDS,
    rate_tolerance_ms: int = RATE_TOLERANCE_MS,
    material_delta_ms: int = MATERIAL_DELTA_MS,
    no_sample_reason: str = "no_timer_samples",
    break_penalty_multiplier: float = 1.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Mark point gaps/anomalies and return ``(gaps, anomalies)``.

    Mutates the point dictionaries so callers can preserve their response shape
    while adding ``gap_before``, ``gap_reason``, ``anomaly`` and
    ``anomaly_reason`` metadata.
    """

    reset_events = reset_events or []
    parsed: list[tuple[datetime, dict[str, Any]]] = []
    for point in points:
        ts = parse_timer_timestamp(point.get("t"))
        if ts is not None:
            parsed.append((ts, point))

    parsed.sort(key=lambda item: item[0])
    gaps: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    penalty_rate = max(1.0, float(break_penalty_multiplier or 1.0))

    def expected_abs_rate(
        previous_point: dict[str, Any],
        current_point: dict[str, Any],
        interval_events: list[dict[str, Any]],
    ) -> float:
        """Return the maximum plausible absolute ms/ms balance rate.

        Manual/user break burns at 1.0x, but idle-timeout / undeclared break
        burns at the configured penalty multiplier. Samples do not currently
        persist the manual-break trigger, so any interval touching BREAK gets
        the penalty ceiling. This prevents legitimate 1.5x burn from becoming
        "impossible_rate" spam.
        """

        # Match any break-flavored mode: legacy "break" plus the split
        # "declared_break" / "idle_break" — all contain the substring "break".
        # An interval touching any of them gets the penalty ceiling so a
        # legitimate 1.5x idle-break burn is not flagged as impossible_rate.
        modes = {
            str(previous_point.get("mode") or "").lower(),
            str(current_point.get("mode") or "").lower(),
        }
        if any("break" in m for m in modes):
            return penalty_rate
        for event in interval_events:
            if "break" in str(event.get("new_mode") or "").lower():
                return penalty_rate
        return 1.0

    def add_gap(
        start: datetime,
        end: datetime,
        reason: str,
        point: dict[str, Any] | None = None,
        *,
        anomaly_reason: str | None = None,
        reset_event: dict[str, Any] | None = None,
    ) -> None:
        gap = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "reason": reason,
        }
        if anomaly_reason:
            gap["anomaly_reason"] = anomaly_reason
        if reset_event is not None:
            gap["trigger"] = reset_event.get("trigger") or reset_event.get("event_type")
        gaps.append(gap)
        if point is not None:
            point["gap_before"] = True
            point["gap_reason"] = reason
            if anomaly_reason:
                point["anomaly"] = True
                point["anomaly_reason"] = anomaly_reason
                anomalies.append(
                    {
                        "t": point.get("t"),
                        "reason": anomaly_reason,
                        "gap_reason": reason,
                        "elapsed_ms": int((end - start).total_seconds() * 1000),
                        "delta_balance_ms": int(point.get("delta_balance_ms") or 0),
                    }
                )

    if not parsed:
        add_gap(window_start, window_end, no_sample_reason, None)
        return gaps, anomalies

    first_time, first_point = parsed[0]
    if (first_time - window_start).total_seconds() > gap_threshold_seconds:
        add_gap(window_start, first_time, no_sample_reason, first_point)

    previous_time, previous_point = parsed[0]
    for current_time, current_point in parsed[1:]:
        elapsed_ms = int((current_time - previous_time).total_seconds() * 1000)
        previous_balance = int(previous_point.get("break_balance_ms") or 0)
        current_balance = int(current_point.get("break_balance_ms") or 0)
        delta_ms = current_balance - previous_balance
        current_point["delta_balance_ms"] = delta_ms

        interval_events = _events_between(reset_events, previous_time, current_time)
        resets = [event for event in interval_events if timer_event_is_reset_discontinuity(event)]
        allowed_rate = expected_abs_rate(previous_point, current_point, interval_events)
        allowed_delta_ms = int(elapsed_ms * allowed_rate)
        # Small samples can be noisy; long samples should tolerate a small
        # percent error as well as the fixed jitter budget.
        tolerance_ms = max(rate_tolerance_ms, int(elapsed_ms * 0.20))
        if resets:
            add_gap(
                previous_time,
                current_time,
                "reset_discontinuity",
                current_point,
                reset_event=resets[-1],
            )
        elif elapsed_ms > 0 and abs(delta_ms) > allowed_delta_ms + tolerance_ms:
            current_point["expected_abs_rate"] = allowed_rate
            add_gap(
                previous_time,
                current_time,
                "anomaly",
                current_point,
                anomaly_reason="impossible_rate",
            )
        elif elapsed_ms > gap_threshold_seconds * 1000 and abs(delta_ms) >= material_delta_ms:
            add_gap(
                previous_time,
                current_time,
                "sample_gap",
                current_point,
                anomaly_reason="sparse_large_delta",
            )
        elif elapsed_ms > gap_threshold_seconds * 1000:
            add_gap(previous_time, current_time, "sample_gap", current_point)

        previous_time, previous_point = current_time, current_point

    last_time, _last_point = parsed[-1]
    if (window_end - last_time).total_seconds() > gap_threshold_seconds:
        add_gap(last_time, window_end, "sample_gap", None)

    return gaps, anomalies
