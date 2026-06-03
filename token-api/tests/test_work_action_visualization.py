"""Work-action visualization: durable daily-note store + cockpit read model.

These tests pin the visibility/durability layer added on top of the shipped
ack→action redesign (#58). The contract:

  - An explicit /api/work-action press appends one {at, source} object to today's
    daily-note `work_actions` frontmatter array (count = len). A second press →
    len 2. This is the canonical, durable store.
  - Ambient work signals (prompt_submit / dictation) route through
    observe_work_signal but MUST NOT write the daily note — only the explicit
    endpoint persists.
  - The cockpit reads work-actions back from the `events` table: /api/ui/ops/state
    reports {count, ticks(with source), last_at, score}, and
    /api/ui/ops/timer/history carries window-scoped `work_action_ticks` for the
    timeline. The optional `score` aggregates `work_signal` events separately and
    is never joined to the timer graph.
"""

import os
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(app_env, monkeypatch):
    async def _no_pane_rows():
        return []

    monkeypatch.setattr(app_env.main, "_tmux_pane_rows", _no_pane_rows)
    return TestClient(app_env.main.app)


def _daily_note_path() -> Path:
    return (
        Path(os.environ["IMPERIUM_ENV"])
        / "Terra"
        / "Journal"
        / "Daily"
        / f"{datetime.now().strftime('%Y-%m-%d')}.md"
    )


def _read_work_actions(note_path: Path) -> list:
    from session_doc_helpers import read_frontmatter

    fm, _body = read_frontmatter(note_path)
    return fm.get("work_actions") or []


# (1) Each explicit press appends {at, source}; a second press → len 2.
def test_work_action_appends_to_daily_note_frontmatter(client) -> None:
    note_path = _daily_note_path()

    resp = client.post("/api/work-action", json={"source": "stream-deck", "note": "paperwork"})
    assert resp.status_code == 200, resp.text

    actions = _read_work_actions(note_path)
    assert len(actions) == 1
    assert actions[0]["source"] == "stream-deck"
    assert actions[0]["at"]  # ISO8601 timestamp present

    resp2 = client.post("/api/work-action", json={"source": "work-action-bind"})
    assert resp2.status_code == 200, resp2.text

    actions = _read_work_actions(note_path)
    assert len(actions) == 2
    assert [a["source"] for a in actions] == ["stream-deck", "work-action-bind"]


# (2) prompt_submit / dictation route through observe_work_signal but never
#     touch the daily note — only the explicit endpoint persists.
def test_ambient_work_signals_do_not_write_daily_note(client) -> None:
    note_path = _daily_note_path()

    resp = client.post("/api/dictation", params={"active": True})
    assert resp.status_code == 200, resp.text

    # No daily note created, or if some other path created it, no work_actions.
    if note_path.exists():
        assert _read_work_actions(note_path) == []


# (3) The cockpit state read model reports today's count, ticks (with source),
#     and last_at from the events table.
def test_ops_state_reports_work_actions(client, app_env) -> None:
    client.post("/api/work-action", json={"source": "stream-deck", "note": "x"})

    resp = client.get("/api/ui/ops/state")
    assert resp.status_code == 200, resp.text
    wa = resp.json()["work_actions"]

    assert wa["count"] == 1
    assert wa["last_at"]
    assert wa["stale_fade_minutes"] == app_env.main.WORK_ACTION_STALE_FADE_MINUTES
    assert len(wa["ticks"]) == 1
    assert wa["ticks"][0]["source"] == "stream-deck"


# (4) The timer-history read model carries window-scoped work-action ticks for
#     the timeline graph.
def test_ops_timer_history_includes_work_action_ticks(client) -> None:
    client.post("/api/work-action", json={"source": "stream-deck", "note": "x"})

    resp = client.get("/api/ui/ops/timer/history?window=6h&bucket=60s")
    assert resp.status_code == 200, resp.text
    ticks = resp.json()["work_action_ticks"]

    assert len(ticks) == 1
    assert ticks[0]["source"] == "stream-deck"
    assert ticks[0]["at"]


# (5) The optional aggregate `score` counts ALL work signals separately from the
#     load-bearing work-action count. One work-action press emits both a
#     work_action event and a work_signal event; a dictation emits only a
#     work_signal. So after 1 press + 1 dictation: count == 1, score == 2.
def test_score_aggregates_work_signals_separately(client) -> None:
    client.post("/api/work-action", json={"source": "stream-deck"})
    client.post("/api/dictation", params={"active": True})

    resp = client.get("/api/ui/ops/state")
    assert resp.status_code == 200, resp.text
    wa = resp.json()["work_actions"]

    assert wa["count"] == 1  # load-bearing: explicit work-actions only
    assert wa["score"] == 2  # non-load-bearing: all work_signal events


# (6) Regression: prompt_submit / ask_user_question / typing-guard reach the same
#     work_action() logic via hook_work_action_callback. They satisfy enforcement
#     but are NOT explicit presses — they must not write the daily note or count
#     toward the work-action dial/ticks. (Bug from PR #64: the count/persist keyed
#     off event_type='work_action', which the hook path also produces.)
def test_hook_driven_work_action_is_not_explicit(client, app_env) -> None:
    import asyncio

    main = app_env.main
    note_path = _daily_note_path()

    # Hook-driven satisfy signal (e.g. a prompt submit) — explicit=False.
    asyncio.run(main.hook_work_action_callback("prompt_submit", "session_id=abc"))

    # The hook path neither writes the daily note...
    if note_path.exists():
        assert _read_work_actions(note_path) == []
    # ...nor counts as an explicit work-action.
    resp = client.get("/api/ui/ops/state")
    assert resp.json()["work_actions"]["count"] == 0

    # An explicit HTTP press IS counted and persisted, even with the hook event
    # already in the same events table.
    client.post("/api/work-action", json={"source": "streamdeck"})
    assert [a["source"] for a in _read_work_actions(note_path)] == ["streamdeck"]
    wa = client.get("/api/ui/ops/state").json()["work_actions"]
    assert wa["count"] == 1
    assert wa["ticks"][0]["source"] == "streamdeck"
