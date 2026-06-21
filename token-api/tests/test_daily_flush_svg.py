"""End-of-day flush → timer-graph SVG embed (P3 2026-06-21).

`_sync_generate_daily_analytics` is the once-a-day flush. P3 gives the daily note
its single durable timer footprint: it renders the day's break-balance graph as a
server-side SVG, writes it beside the JSON analytics, and embeds it in place of
the (runtime-dead) NOW callout. These tests drive the flush against a temp DB +
temp Daily dir — never the live vault/DB/tmux.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

_DATE = "2026-06-20"

_TARGET_SHIFTS = [
    # (timestamp, old_mode, new_mode, trigger, source, break_balance_ms, active_instances)
    (f"{_DATE}T06:00:00-07:00", None, "working", "manual", "user", 0, 1),
    (f"{_DATE}T09:00:00-07:00", "working", "working", "tick", "engine", 45 * 60 * 1000, 2),
    (f"{_DATE}T12:00:00-07:00", "working", "break", "manual", "user", 90 * 60 * 1000, 1),
    (f"{_DATE}T15:00:00-07:00", "break", "working", "enforcement", "satellite", 10 * 60 * 1000, 3),
    (f"{_DATE}T18:00:00-07:00", "working", "break", "tick", "engine", -30 * 60 * 1000, 0),
]

_NON_TARGET_SHIFTS = [
    ("2026-06-20T05:59:59-07:00", "break", "working", "previous_day", "engine", 999, 9),
    ("2026-06-21T06:00:00-07:00", "working", "break", "next_day_boundary", "engine", 888, 8),
    ("2026-06-22T09:00:00-07:00", "working", "break", "future_day", "engine", 777, 7),
]


def _seed_timer_shifts(db_path: Path, shifts: list[tuple] | None = None) -> list[int]:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS timer_shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            old_mode TEXT,
            new_mode TEXT NOT NULL,
            trigger TEXT,
            source TEXT,
            break_balance_ms INTEGER,
            break_backlog_ms INTEGER,
            work_time_ms INTEGER,
            active_instances INTEGER,
            phone_app TEXT,
            details TEXT
        )
    """)
    ids: list[int] = []
    for shift in shifts or _TARGET_SHIFTS:
        cur = conn.execute(
            "INSERT INTO timer_shifts "
            "(timestamp, old_mode, new_mode, trigger, source, break_balance_ms, active_instances) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            shift,
        )
        ids.append(int(cur.lastrowid))
    conn.commit()
    conn.close()
    return ids


def _daily_dir(main: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    daily = tmp_path / "Daily"
    (daily).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main, "OBSIDIAN_DAILY_PATH", daily)
    return daily


_NOTE_WITH_CALLOUT = (
    "---\n"
    "date: 2026-06-20\n"
    "type: daily-note\n"
    "agents:\n"
    "- custodes-abc\n"
    "---\n"
    "\n"
    "# 2026-06-20\n"
    "\n"
    "<!-- callout:now BEGIN -->\n"
    "> [!info]+ NOW\n"
    "> working\n"
    "<!-- callout:now END -->\n"
    "\n"
    "## Log\n"
    "Body that must survive byte-for-byte.\n"
)


def test_flush_writes_svg_and_embeds_in_now_callout(
    app_env: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = app_env.main
    daily = _daily_dir(main, tmp_path, monkeypatch)
    target_ids = _seed_timer_shifts(
        app_env.db_path, _NON_TARGET_SHIFTS[:1] + _TARGET_SHIFTS + _NON_TARGET_SHIFTS[1:]
    )
    expected_deleted_ids = target_ids[1 : 1 + len(_TARGET_SHIFTS)]

    note = daily / f"{_DATE}.md"
    note.write_text(_NOTE_WITH_CALLOUT, encoding="utf-8")

    out = main._sync_generate_daily_analytics(_DATE)

    # JSON analytics written.
    assert out is not None and out.endswith(f"timer-{_DATE}.json")

    # SVG written beside the JSON, well-formed and self-contained.
    svg_path = daily / "analytics" / f"timer-{_DATE}.svg"
    assert svg_path.exists()
    svg = svg_path.read_text(encoding="utf-8")
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert "<polyline" in svg  # real timeline, not the placeholder
    # No temp file left behind.
    assert not (daily / "analytics" / f".timer-{_DATE}.svg.tmp").exists()

    # The NOW callout now carries the embed (not the old "working" text).
    final = note.read_text(encoding="utf-8")
    assert f"![[timer-{_DATE}.svg]]" in final
    assert "> working" not in final
    assert final.count("<!-- callout:now BEGIN -->") == 1
    assert "Timer · 2026-06-20" in final

    # Body preserved byte-for-byte (everything after the callout END region).
    assert "## Log\nBody that must survive byte-for-byte.\n" in final
    assert "custodes-abc" in final  # frontmatter list untouched

    # Analytics frontmatter set.
    import session_doc_helpers as sdh

    fm, _ = sdh.read_frontmatter(note)
    assert fm["timer_total_shifts"] == len(_TARGET_SHIFTS)
    assert fm["timer_enforcements"] == 1
    assert fm["timer_peak_break"] == "1h 30m"
    assert fm["timer_min_break"] == "-0h 30m"
    assert fm["timer_max_instances"] == 3

    # Only flushed target-window rows were deleted; previous/current/future rows remain.
    summary = json.loads(Path(out).read_text(encoding="utf-8"))
    assert summary["total_shifts"] == len(_TARGET_SHIFTS)
    assert {p["time"] for p in summary["balance_timeline"]} == {row[0] for row in _TARGET_SHIFTS}

    conn = sqlite3.connect(app_env.db_path)
    remaining_rows = conn.execute(
        "SELECT id, timestamp, trigger FROM timer_shifts ORDER BY id"
    ).fetchall()
    conn.close()
    assert {row[0] for row in remaining_rows}.isdisjoint(expected_deleted_ids)
    assert [row[2] for row in remaining_rows] == [
        "previous_day",
        "next_day_boundary",
        "future_day",
    ]


def test_flush_appends_embed_when_note_has_no_now_callout(
    app_env: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prior-day notes from create_daily_note_file have no NOW callout — the embed
    must be appended, not lost."""
    main = app_env.main
    daily = _daily_dir(main, tmp_path, monkeypatch)
    _seed_timer_shifts(app_env.db_path)

    note = daily / f"{_DATE}.md"
    note.write_text(
        "---\ndate: 2026-06-20\ntype: daily-note\n---\n\n# 2026-06-20\n\n## Log\nbody.\n",
        encoding="utf-8",
    )

    main._sync_generate_daily_analytics(_DATE)

    final = note.read_text(encoding="utf-8")
    assert f"![[timer-{_DATE}.svg]]" in final
    assert final.count("<!-- callout:now BEGIN -->") == 1
    assert "body." in final  # original body preserved


def test_flush_with_no_target_shifts_writes_nothing_and_preserves_rows(
    app_env: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No target-day shift data → no files, no note touch, no deletes."""
    main = app_env.main
    daily = _daily_dir(main, tmp_path, monkeypatch)
    inserted_ids = _seed_timer_shifts(app_env.db_path, _NON_TARGET_SHIFTS)

    out = main._sync_generate_daily_analytics(_DATE)
    assert out is None
    assert not (daily / "analytics" / f"timer-{_DATE}.json").exists()
    assert not (daily / "analytics" / f"timer-{_DATE}.svg").exists()

    conn = sqlite3.connect(app_env.db_path)
    remaining_ids = [
        row[0] for row in conn.execute("SELECT id FROM timer_shifts ORDER BY id").fetchall()
    ]
    conn.close()
    assert remaining_ids == inserted_ids


def test_flush_without_note_still_writes_svg(
    app_env: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the daily note doesn't exist, the SVG + JSON are still written (the note
    embed is simply skipped) — no crash."""
    main = app_env.main
    daily = _daily_dir(main, tmp_path, monkeypatch)
    _seed_timer_shifts(app_env.db_path)

    out = main._sync_generate_daily_analytics(_DATE)
    assert out is not None
    assert (daily / "analytics" / f"timer-{_DATE}.svg").exists()
    assert not (daily / f"{_DATE}.md").exists()
