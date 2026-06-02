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


def test_wall_of_anomalies_collapses_to_suspect_detection():
    # A backfill / clock-skew flood: every other sample swings the balance by a
    # physically impossible amount. The detector would otherwise flag a wall of
    # impossible_rate anomalies; that wall is itself a reverse signal.
    now = datetime.now()
    points = []
    for i in range(40):
        points.append(
            {
                "t": (now - timedelta(seconds=(40 - i) * 30)).isoformat(),
                "mode": "working",
                # Alternate between 0 and a 10-minute swing every 30s sample.
                "break_balance_ms": 0 if i % 2 == 0 else -600_000,
            }
        )

    gaps, anomalies = annotate_timer_telemetry(
        points,
        window_start=now - timedelta(minutes=30),
        window_end=now,
    )

    assert len(anomalies) == 1
    record = anomalies[0]
    assert record["reason"] == "bulk_anomaly_suspected"
    assert record["suppressed_count"] >= 12
    assert record["dominant_reason"] == "impossible_rate"
    # The per-point anomaly marks and the anomaly-derived gaps are reverted so
    # the graph line reconnects instead of shattering into point soup.
    assert not any(point.get("anomaly") for point in points)
    assert not any(gap.get("anomaly_reason") for gap in gaps)


def test_a_handful_of_anomalies_is_not_bulk():
    # Below the min-count floor, genuine anomalies survive as themselves even
    # if they are a high fraction of a tiny window.
    now = datetime.now()
    points = [
        {"t": (now - timedelta(seconds=90)).isoformat(), "mode": "working", "break_balance_ms": 0},
        {"t": (now - timedelta(seconds=60)).isoformat(), "mode": "working", "break_balance_ms": -600_000},
        {"t": (now - timedelta(seconds=30)).isoformat(), "mode": "working", "break_balance_ms": 0},
    ]

    _gaps, anomalies = annotate_timer_telemetry(
        points,
        window_start=now - timedelta(minutes=15),
        window_end=now,
    )

    assert all(a["reason"] != "bulk_anomaly_suspected" for a in anomalies)
    assert anomalies
