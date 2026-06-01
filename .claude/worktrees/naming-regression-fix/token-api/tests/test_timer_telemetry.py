from datetime import datetime, timedelta

from timer_telemetry import annotate_timer_telemetry


def test_penalized_break_burn_is_not_impossible_rate():
    now = datetime.now()
    points = [
        {
            "t": (now - timedelta(seconds=60)).isoformat(),
            "mode": "break",
            "break_balance_ms": 0,
        },
        {
            "t": (now - timedelta(seconds=30)).isoformat(),
            "mode": "break",
            "break_balance_ms": -45_000,
        },
    ]

    _gaps, anomalies = annotate_timer_telemetry(
        points,
        window_start=now - timedelta(minutes=15),
        window_end=now,
        break_penalty_multiplier=1.5,
    )

    assert not [a for a in anomalies if a["reason"] == "impossible_rate"]


def test_true_large_jump_is_still_impossible_rate():
    now = datetime.now()
    points = [
        {
            "t": (now - timedelta(seconds=60)).isoformat(),
            "mode": "break",
            "break_balance_ms": 0,
        },
        {
            "t": (now - timedelta(seconds=30)).isoformat(),
            "mode": "break",
            "break_balance_ms": -120_000,
        },
    ]

    _gaps, anomalies = annotate_timer_telemetry(
        points,
        window_start=now - timedelta(minutes=15),
        window_end=now,
        break_penalty_multiplier=1.5,
    )

    assert any(a["reason"] == "impossible_rate" for a in anomalies)
