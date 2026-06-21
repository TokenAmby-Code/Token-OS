"""End-of-day flush → timer-graph SVG embed (P3 2026-06-21).

`_sync_generate_daily_analytics` is the once-a-day flush. P3 gives the daily note
its single durable timer footprint: it renders the day's break-balance graph as a
server-side SVG, writes it beside the JSON analytics, and embeds it in place of
the (runtime-dead) NOW callout. These tests drive the flush against a temp DB +
temp Daily dir — never the live vault/DB/tmux.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

_DATE = "2026-06-20"

_SHIFTS = [
    # (timestamp, old_mode, new_mode, trigger, source, break_balance_ms, active_instances)
    (f"{_DATE}T06:00:00-07:00", None, "working", "manual", "user", 0, 1),
    (f"{_DATE}T09:00:00-07:00", "working", "working", "tick", "engine", 45 * 60 * 1000, 2),
    (f"{_DATE}T12:00:00-07:00", "working", "break", "manual", "user", 90 * 60 * 1000, 1),
    (f"{_DATE}T15:00:00-07:00", "break", "working", "enforcement", "satellite", 10 * 60 * 1000, 3),
    (f"{_DATE}T18:00:00-07:00", "working", "break", "tick", "engine", -30 * 60 * 1000, 0),
]


def _seed_timer_shifts(db_path: Path) -> None:
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
    conn.executemany(
        "INSERT INTO timer_shifts "
        "(timestamp, old_mode, new_mode, trigger, source, break_balance_ms, active_instances) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        _SHIFTS,
    )
    conn.commit()
    conn.close()


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
    _seed_timer_shifts(app_env.db_path)

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
    assert fm["timer_total_shifts"] == len(_SHIFTS)
    assert fm["timer_enforcements"] == 1
    assert fm["timer_peak_break"] == "1h 30m"
    assert fm["timer_min_break"] == "-0h 30m"
    assert fm["timer_max_instances"] == 3

    # timer_shifts wiped.
    conn = sqlite3.connect(app_env.db_path)
    remaining = conn.execute("SELECT COUNT(*) FROM timer_shifts").fetchone()[0]
    conn.close()
    assert remaining == 0


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


def test_flush_with_no_shifts_writes_nothing(
    app_env: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No shift data → the flush returns None and writes no SVG (no note touch)."""
    main = app_env.main
    daily = _daily_dir(main, tmp_path, monkeypatch)
    # No seed.

    out = main._sync_generate_daily_analytics(_DATE)
    assert out is None
    assert not (daily / "analytics" / f"timer-{_DATE}.svg").exists()


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
