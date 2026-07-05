from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import datetime


def test_talk_resolves_instance_id_via_live_oracle(app_env, monkeypatch) -> None:
    talk = sys.modules["talk"]
    talk.DB_PATH = str(app_env.db_path)
    talk.shared = app_env.shared
    now = datetime.now().isoformat()
    with sqlite3.connect(app_env.db_path) as conn:
        conn.execute(
            """INSERT INTO instances (id, name, device_id, status, rank, last_activity)
               VALUES (?, ?, 'Mac-Mini', 'working', 'astartes', ?)""",
            ("inst-live", "needs-name", now),
        )
        conn.commit()

    async def fake_resolve(instance_id):
        assert instance_id == "inst-live"
        return "%44", "mechanicus:4"

    async def no_panes():
        return []

    monkeypatch.setattr(app_env.shared, "resolve_instance_pane", fake_resolve)
    monkeypatch.setattr(talk, "_tmux_list_panes", no_panes)

    assert asyncio.run(talk.resolve_pane("inst-live")) == "%44"


def test_talk_resolves_persona_display_name_via_live_oracle(app_env, monkeypatch) -> None:
    talk = sys.modules["talk"]
    talk.DB_PATH = str(app_env.db_path)
    talk.shared = app_env.shared
    now = datetime.now().isoformat()
    with sqlite3.connect(app_env.db_path) as conn:
        conn.execute(
            """INSERT INTO personas (id, slug, display_name, default_rank)
               VALUES ('persona-resolver', 'resolver-persona', 'Resolver Persona', 'overseer')"""
        )
        conn.execute(
            """INSERT INTO instances (id, name, device_id, status, rank, persona_id, last_activity)
               VALUES ('fg-inst', 'needs-name', 'Mac-Mini', 'working', 'overseer', 'persona-resolver', ?)""",
            (now,),
        )
        conn.commit()

    async def fake_resolve(instance_id):
        assert instance_id == "fg-inst"
        return "%55", "mechanicus:resolver-persona"

    async def no_panes():
        return []

    monkeypatch.setattr(app_env.shared, "resolve_instance_pane", fake_resolve)
    monkeypatch.setattr(talk, "_tmux_list_panes", no_panes)

    assert asyncio.run(talk.resolve_pane("resolver persona")) == "%55"
