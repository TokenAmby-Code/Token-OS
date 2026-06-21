"""Tests for the pure-Python timer-graph SVG renderer (P3 2026-06-21).

`render_timer_svg` turns the flush `summary` dict into a self-contained
`<svg>…</svg>` string. It must never raise on degenerate input and must emit a
well-formed, embeddable element for a real timeline.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from timer_svg import render_timer_svg


def _seeded_summary():
    # A day that earns, peaks, then dips into backlog.
    timeline = [
        {"time": "2026-06-20T06:00:00-07:00", "balance_ms": 0},
        {"time": "2026-06-20T09:00:00-07:00", "balance_ms": 45 * 60 * 1000},
        {"time": "2026-06-20T12:00:00-07:00", "balance_ms": 90 * 60 * 1000},
        {"time": "2026-06-20T15:00:00-07:00", "balance_ms": 10 * 60 * 1000},
        {"time": "2026-06-20T18:00:00-07:00", "balance_ms": -30 * 60 * 1000},
    ]
    return {
        "date": "2026-06-20",
        "balance_timeline": timeline,
        "peak_break_balance_ms": 90 * 60 * 1000,
        "min_break_balance_ms": -30 * 60 * 1000,
    }


def test_renders_well_formed_svg():
    svg = render_timer_svg(_seeded_summary())
    assert svg.startswith("<svg")
    assert svg.rstrip().endswith("</svg>")
    # Parseable XML (self-contained, no external refs needed).
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert root.get("viewBox") == "0 0 800 400"


def test_contains_balance_polyline_and_axis_labels():
    summary = _seeded_summary()
    svg = render_timer_svg(summary)
    # The core series is a polyline.
    assert "<polyline" in svg
    # Filled area split into earned/backlog regions.
    assert "<polygon" in svg
    assert svg.count("clip-path") == 2
    # Peak / min labels via format_timer_time ("1h 30m" peak, "-0h 30m" trough).
    assert "1h 30m" in svg
    assert "-0h 30m" in svg
    # Title carries the date.
    assert "2026-06-20" in svg
    # X-axis end labels from the timestamps (local HH:MM).
    assert "06:00" in svg
    assert "18:00" in svg


def test_empty_timeline_renders_placeholder_not_crash():
    svg = render_timer_svg({"date": "2026-06-20", "balance_timeline": []})
    root = ET.fromstring(svg)  # still valid XML
    assert root.tag.endswith("svg")
    assert "No timer data" in svg
    assert "<polyline" not in svg


def test_missing_timeline_key_renders_placeholder():
    svg = render_timer_svg({"date": "2026-06-20"})
    assert "No timer data" in svg
    ET.fromstring(svg)


def test_single_point_timeline_is_placeholder():
    summary = {
        "date": "2026-06-20",
        "balance_timeline": [{"time": "2026-06-20T06:00:00-07:00", "balance_ms": 0}],
    }
    svg = render_timer_svg(summary)
    assert "No timer data" in svg
    ET.fromstring(svg)


def test_unparseable_timestamps_fall_back_to_index_spacing():
    summary = {
        "date": "2026-06-20",
        "balance_timeline": [
            {"time": "not-a-time", "balance_ms": 0},
            {"time": "also-bad", "balance_ms": 60 * 60 * 1000},
            {"time": "nope", "balance_ms": 30 * 60 * 1000},
        ],
        "peak_break_balance_ms": 60 * 60 * 1000,
        "min_break_balance_ms": 0,
    }
    # Must not raise even though no timestamp parses.
    svg = render_timer_svg(summary)
    assert "<polyline" in svg
    ET.fromstring(svg)
    # No X-axis HH:MM labels when timestamps don't parse.
    assert "<text" in svg


def test_all_zero_balance_does_not_divide_by_zero():
    summary = {
        "date": "2026-06-20",
        "balance_timeline": [
            {"time": "2026-06-20T06:00:00-07:00", "balance_ms": 0},
            {"time": "2026-06-20T12:00:00-07:00", "balance_ms": 0},
            {"time": "2026-06-20T18:00:00-07:00", "balance_ms": 0},
        ],
        "peak_break_balance_ms": 0,
        "min_break_balance_ms": 0,
    }
    svg = render_timer_svg(summary)
    assert "<polyline" in svg
    ET.fromstring(svg)
