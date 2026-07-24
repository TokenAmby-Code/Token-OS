"""Behavioral pins for the Imperium daily-note automation service control."""

from __future__ import annotations

import asyncio
import importlib
import plistlib
import sys
from datetime import datetime as RealDateTime
from pathlib import Path
from types import SimpleNamespace

import pytest

TOKEN_API_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = TOKEN_API_DIR.parent
if str(TOKEN_API_DIR) not in sys.path:
    sys.path.insert(0, str(TOKEN_API_DIR))

ENV_NAME = "TOKEN_API_DISABLE_AUTOMATIC_IMPERIUM_DAILY_NOTE_WRITES"


@pytest.fixture
def disabled(monkeypatch):
    monkeypatch.setenv(ENV_NAME, "1")


def test_launchagent_template_disables_automatic_imperium_daily_note_writes():
    plist_path = REPO_ROOT / "cli-tools" / "launchd" / "ai.openclaw.tokenapi.plist"
    with plist_path.open("rb") as handle:
        plist = plistlib.load(handle)

    assert plist["EnvironmentVariables"][ENV_NAME] == "1"


def test_service_control_is_fail_closed_for_unknown_enabled_values(monkeypatch):
    shared = importlib.import_module("shared")

    monkeypatch.delenv(ENV_NAME, raising=False)
    assert shared.automatic_imperium_daily_note_writes_disabled() is False
    for value in ("0", "false", "no", "off"):
        monkeypatch.setenv(ENV_NAME, value)
        assert shared.automatic_imperium_daily_note_writes_disabled() is False
    for value in ("", "1", "true", "enabled", "unexpected"):
        monkeypatch.setenv(ENV_NAME, value)
        assert shared.automatic_imperium_daily_note_writes_disabled() is True


def test_morning_session_writer_chokepoints_skip_without_side_effects(disabled, monkeypatch):
    morning = importlib.import_module("morning_session")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled morning path attempted a writer or pane side effect")

    monkeypatch.setattr(morning.subprocess, "run", forbidden)
    monkeypatch.setattr(morning.shared, "_tmuxctld_post_json", forbidden)
    monkeypatch.setattr(morning, "_discord_create_thread", forbidden)
    monkeypatch.setattr(morning, "resolve_custodes_pane", forbidden)

    assert morning.ensure_daily_notes() == {
        "status": "skipped",
        "reason": "automatic_imperium_daily_note_writes_disabled",
    }
    assert morning.create_daily_thread("2026-07-24") is None
    assert morning.inject_morning_lifecycle_prompt() == {
        "injected": False,
        "reason": "automatic_imperium_daily_note_writes_disabled",
    }
    assert morning.run_morning_session() == {
        "status": "skipped",
        "reason": "automatic_imperium_daily_note_writes_disabled",
        "date": RealDateTime.now().strftime("%Y-%m-%d"),
    }


def test_day_start_fanout_preserves_unrelated_consumers_and_skips_all_writers(
    disabled, monkeypatch
):
    day_start = importlib.import_module("routes.day_start")
    events: list[tuple[str, dict]] = []

    async def fake_log_event(event_type, **kwargs):
        events.append((event_type, kwargs))

    async def fake_phone(_state):
        return {"status": "ok", "reachable": True}

    monkeypatch.setattr(day_start, "log_event", fake_log_event)
    monkeypatch.setattr(day_start, "_consumer_phone_reachability", fake_phone)

    fanout = asyncio.run(_run_day_start_fanout(day_start))
    by_name = {item["consumer"]: item for item in fanout}

    for name in ("custodes_doc_rebind", "custodes_morning_session", "daily_note_creation"):
        assert by_name[name]["success"] is True
        assert by_name[name]["result"] == {
            "status": "skipped",
            "reason": "automatic_imperium_daily_note_writes_disabled",
        }
    assert by_name["quiet_hours"]["result"]["status"] == "ok"
    assert by_name["tts_suppression"]["result"]["status"] == "ok"
    assert by_name["phone_reachability_check"]["result"]["reachable"] is True
    assert {
        details["details"]["consumer"] for event, details in events if event == "day_start_consumer"
    } >= {
        "custodes_doc_rebind",
        "custodes_morning_session",
        "daily_note_creation",
    }


async def _run_day_start_fanout(day_start):
    return await day_start._day_start_fanout({"day_started_at": "2026-07-24T06:00:00"})


def test_schedule_fallback_reaches_guarded_fanout_without_creating_or_launching(
    disabled, monkeypatch
):
    day_start = importlib.import_module("routes.day_start")

    async def no_day_state(_date):
        return None

    async def start_day(*, source, details=None, force=False):
        return {
            "date": "2026-07-24",
            "day_started_at": "2026-07-24T08:30:00",
            "source": source,
            "already_started": False,
        }

    monkeypatch.setattr(day_start, "get_day_state", no_day_state)
    monkeypatch.setattr(day_start, "set_day_started_at", start_day)
    monkeypatch.setattr(day_start, "log_event", lambda *_args, **_kwargs: asyncio.sleep(0))
    monkeypatch.setattr(
        day_start,
        "_consumer_phone_reachability",
        lambda _state: asyncio.sleep(0, result={"status": "ok", "reachable": True}),
    )

    result = asyncio.run(day_start.fire_day_start_schedule_fallback())
    by_name = {item["consumer"]: item["result"] for item in result["fanout"]}
    assert by_name["custodes_doc_rebind"]["status"] == "skipped"
    assert by_name["custodes_morning_session"]["status"] == "skipped"
    assert by_name["daily_note_creation"]["status"] == "skipped"


def test_alarm_silenced_fanout_cannot_create_rebind_or_launch(disabled, monkeypatch):
    main = importlib.import_module("main")
    calls: list[str] = []

    async def fake_fire_day_start_internal(*, source):
        calls.append(source)
        return {
            "already_started": False,
            "day_state": {"source": source},
            "fanout": [
                {
                    "consumer": name,
                    "success": True,
                    "result": {
                        "status": "skipped",
                        "reason": "automatic_imperium_daily_note_writes_disabled",
                    },
                }
                for name in (
                    "custodes_doc_rebind",
                    "custodes_morning_session",
                    "daily_note_creation",
                )
            ],
        }

    monkeypatch.setattr(main, "fire_day_start_internal", fake_fire_day_start_internal)
    monkeypatch.setattr(main, "log_event", lambda *_args, **_kwargs: asyncio.sleep(0))

    result = asyncio.run(main.alarm_silenced(delay_minutes=0))
    assert calls == ["alarm_silenced"]
    assert result["already_started"] is False


def test_direct_morning_endpoints_skip_before_db_or_spawn(disabled, monkeypatch):
    main = importlib.import_module("main")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled endpoint attempted DB access or a spawn")

    async def fake_log_event(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main, "connect_agents_db", forbidden)
    monkeypatch.setattr(main.asyncio, "create_task", forbidden)
    monkeypatch.setattr(main, "log_event", fake_log_event)

    start = asyncio.run(main.start_morning_session())
    brief = asyncio.run(main.custodes_morning_brief(None))

    for result in (start, brief):
        assert result["status"] == "skipped"
        assert result["reason"] == "automatic_imperium_daily_note_writes_disabled"


def test_internal_prompt_injection_skips_before_thread_dispatch(disabled, monkeypatch):
    main = importlib.import_module("main")

    async def fake_log_event(*_args, **_kwargs):
        return None

    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled lifecycle prompt reached a worker thread")

    monkeypatch.setattr(main, "log_event", fake_log_event)
    monkeypatch.setattr(main.asyncio, "to_thread", forbidden)

    assert asyncio.run(main._inject_custodes_morning_prompt("test")) == {
        "injected": False,
        "reason": "automatic_imperium_daily_note_writes_disabled",
    }


def test_analytics_chokepoint_cannot_touch_sqlite_or_vault(disabled, monkeypatch):
    main = importlib.import_module("main")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled analytics path opened its DB or vault")

    async def fake_log_event(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main.sqlite3, "connect", forbidden)
    monkeypatch.setattr(main.asyncio, "to_thread", forbidden)
    monkeypatch.setattr(main, "log_event", fake_log_event)
    assert main._sync_generate_daily_analytics("2026-07-23") is None
    assert asyncio.run(main.generate_daily_timer_analytics("2026-07-23")) == {
        "status": "skipped",
        "reason": "automatic_imperium_daily_note_writes_disabled",
        "date": "2026-07-23",
    }


def test_0600_entry_preserves_timer_and_db_lifecycle_but_skips_writers(disabled, monkeypatch):
    main = importlib.import_module("main")
    calls: list[str] = []

    class FakeResult:
        events = {main.TimerEvent.DAILY_RESET}
        reset_date = "2026-07-23"
        productivity_score = 77

    class FakeTimerEngine:
        current_mode = SimpleNamespace(value="quiet")
        break_balance_ms = 0
        total_work_time_ms = 0
        total_break_time_ms = 0

        def enter_morning_session(self, _now_ms, _today):
            self.current_mode = SimpleNamespace(value="morning_session")
            calls.append("timer_reset")
            return True, FakeResult()

    async def record(name, result=None):
        calls.append(name)
        return result

    monkeypatch.setattr(main, "timer_engine", FakeTimerEngine())
    monkeypatch.setattr(main, "_current_session_id", 0)
    monkeypatch.setattr(
        main.shared,
        "set_day_started_at",
        lambda **_kwargs: record("day_started"),
    )
    monkeypatch.setattr(
        main,
        "_write_morning_audit_state",
        lambda *_args: record("audit_state"),
    )
    monkeypatch.setattr(main, "timer_save_to_db", lambda: record("timer_db_saved"))
    monkeypatch.setattr(
        main,
        "_wipe_prior_day_timer_events",
        lambda _today: record("prior_events_wiped"),
    )
    monkeypatch.setattr(
        main,
        "timer_start_session",
        lambda *_args: record("timer_session_started", 9),
    )
    monkeypatch.setattr(
        main,
        "timer_log_mode_change",
        lambda *_args, **_kwargs: record("mode_change_logged"),
    )
    monkeypatch.setattr(
        main,
        "timer_log_shift",
        lambda *_args, **_kwargs: record("shift_logged"),
    )
    monkeypatch.setattr(
        main,
        "log_event",
        lambda *_args, **_kwargs: record("event_logged"),
    )

    result = asyncio.run(
        main.enter_morning_session_internal(source="scheduler_0600", inject_prompt=True)
    )

    assert result["status"] == "morning_session"
    assert result["injection"] == {
        "injected": False,
        "reason": "automatic_imperium_daily_note_writes_disabled",
    }
    assert {
        "timer_reset",
        "day_started",
        "audit_state",
        "timer_db_saved",
        "prior_events_wiped",
        "timer_session_started",
        "mode_change_logged",
        "shift_logged",
    } <= set(calls)
    assert "timer_daily_analytics_skipped" not in calls


def test_startup_recovery_never_requests_prompt_injection(disabled, monkeypatch):
    main = importlib.import_module("main")
    captured: dict[str, object] = {}

    class FixedDateTime(RealDateTime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 24, 6, 30, tzinfo=tz)

    async def fake_enter(*, source, inject_prompt):
        captured.update(source=source, inject_prompt=inject_prompt)
        return {"status": "morning_session"}

    monkeypatch.setattr(main, "datetime", FixedDateTime)
    monkeypatch.setattr(main, "timer_engine", SimpleNamespace(daily_start_date="2026-07-23"))
    monkeypatch.setattr(main, "enter_morning_session_internal", fake_enter)

    result = asyncio.run(main.recover_missed_morning_session_startup())
    assert result["recovered"] is True
    assert captured == {"source": "startup_recovery", "inject_prompt": False}


def test_session_start_cannot_create_or_bind_imperium_daily_note(disabled, monkeypatch):
    helpers = importlib.import_module("session_doc_helpers")

    async def daily_persona(_db, _candidate):
        return {"default_session_doc": "daily_note"}

    monkeypatch.setattr(helpers, "resolve_persona", daily_persona)
    doc_id, policy = asyncio.run(
        helpers.resolve_session_doc_for_start(
            object(),
            dispatch_session_doc_path=None,
            primarch_name="custodes",
            origin_type="interactive",
            cron_job_id=None,
            cron_job_name=None,
            working_dir="/Users/tokenclaw/Documents/Imperium-ENV",
            is_subagent=False,
            legion="custodes",
        )
    )
    assert (doc_id, policy) == (None, "automatic_daily_note_writes_disabled")

    with pytest.raises(helpers.AutomaticImperiumDailyNoteWritesDisabled):
        asyncio.run(
            helpers.resolve_or_create_today_daily_note_session_doc(
                object(),
                working_dir="/Users/tokenclaw/Documents/Imperium-ENV",
                legion="custodes",
            )
        )


def test_morning_keepalive_liveness_is_disabled_even_while_timer_mode_remains(
    disabled, monkeypatch
):
    morning = importlib.import_module("morning_session")
    monkeypatch.setattr(
        morning.shared,
        "timer_engine",
        SimpleNamespace(current_mode=SimpleNamespace(value="morning_session")),
        raising=False,
    )

    assert morning.morning_session_active() == (
        False,
        "automatic_imperium_daily_note_writes_disabled",
    )


def test_health_exposes_service_control(disabled, monkeypatch):
    main = importlib.import_module("main")
    monkeypatch.setattr(
        main,
        "probe_sqlite_write_readiness",
        lambda _path: {"live": True, "ready": True, "reason": None},
    )

    health = asyncio.run(main.health_check())
    assert health["automatic_imperium_daily_note_writes"] == {
        "disabled": True,
        "control": ENV_NAME,
    }
