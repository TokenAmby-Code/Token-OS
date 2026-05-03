"""Tests for the AskUserQuestion three-touch ladder.

Touch 1 → Touch 2 (enforcement cascade) → Touch 3 (TTS re-read) → Bust (autonomous prompt).
PostToolUse(AskUserQuestion) cancels the ladder.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest


def _set_short_durations(shared, t1=0.05, t2=0.05, t3=0.05):
    """Shorten timer constants so the full ladder fires quickly under test."""
    shared.ASKQ_T1_SECONDS = t1
    shared.ASKQ_T2_SECONDS = t2
    shared.ASKQ_T3_SECONDS = t3


@pytest.fixture
def ladder_env(app_env, monkeypatch):
    """app_env + ladder durations slammed down to a few ms for fast tests."""
    shared = app_env.shared
    _set_short_durations(shared)
    yield app_env


def test_should_engage_ladder_voice_chat(ladder_env):
    hooks = sys.modules["routes.hooks"]
    shared = ladder_env.shared

    sid = "vc-instance"
    shared.VOICE_CHAT_SESSIONS[sid] = {"active": True}
    try:
        assert hooks._askq_should_engage_ladder(None, sid) is True
    finally:
        shared.VOICE_CHAT_SESSIONS.pop(sid, None)


def test_should_engage_ladder_golden_throne(ladder_env):
    hooks = sys.modules["routes.hooks"]
    assert hooks._askq_should_engage_ladder({"instance_type": "golden_throne"}, "gt-1") is True


def test_should_not_engage_ladder_plain_session(ladder_env):
    hooks = sys.modules["routes.hooks"]
    assert hooks._askq_should_engage_ladder({"instance_type": "one_off"}, "plain") is False
    assert hooks._askq_should_engage_ladder(None, "no-row") is False


def test_ladder_cancelled_on_post_tool_use(ladder_env):
    """Answer arrives before T1 elapses → ladder cancels, no Touch 2 fires."""
    hooks = sys.modules["routes.hooks"]
    shared = ladder_env.shared

    touch2_calls: list[tuple[str, str]] = []

    async def fake_touch2(instance_id, question_text):
        touch2_calls.append((instance_id, question_text))

    hooks._askq_touch2_callback = fake_touch2
    shared.ASKQ_T1_SECONDS = 0.5

    async def run():
        sid = "answered-fast"
        await hooks._askq_ladder_start(sid, "Question?", ["yes", "no"], {"tmux_pane": "%0"})
        assert sid in shared.ASKQ_LADDER
        # Answer arrives quickly via PostToolUse(AskUserQuestion).
        await asyncio.sleep(0.05)
        await hooks._askq_ladder_cancel(sid, reason="answered")
        # Wait past where Touch 2 would have fired.
        await asyncio.sleep(0.6)
        assert sid not in shared.ASKQ_LADDER
        assert touch2_calls == []

    asyncio.run(run())


def test_ladder_full_walkthrough_fires_touch2_touch3_bust(ladder_env, monkeypatch):
    """No answer → Touch 2 callback fires, Touch 3 re-reads TTS, Bust attempts claude-cmd."""
    hooks = sys.modules["routes.hooks"]
    shared = ladder_env.shared

    touch2_calls: list[tuple[str, str]] = []
    tts_calls: list[tuple[str, str]] = []
    bust_calls: list[dict] = []

    async def fake_touch2(instance_id, question_text):
        touch2_calls.append((instance_id, question_text))

    async def fake_queue_tts(instance_id, text, queue_target="hot"):
        tts_calls.append((instance_id, text))

    async def fake_send_bust(instance_id, state):
        bust_calls.append({"instance_id": instance_id, "state": dict(state)})

    hooks._askq_touch2_callback = fake_touch2
    monkeypatch.setattr(hooks, "queue_tts", fake_queue_tts)
    monkeypatch.setattr(hooks, "_askq_send_bust_prompt", fake_send_bust)

    async def run():
        sid = "no-answer"
        await hooks._askq_ladder_start(
            sid, "Pick a path?", ["A", "B"], {"tmux_pane": "%9", "device_id": "Mac-Mini"}
        )
        # Wait long enough for T1 + T2 + T3 to all elapse (each is 0.05s).
        await asyncio.sleep(0.4)

        # All three callback paths exercised, ladder cleaned up.
        assert touch2_calls == [(sid, "Pick a path?")]
        assert tts_calls == [(sid, "Pick a path?")]
        assert len(bust_calls) == 1
        assert bust_calls[0]["instance_id"] == sid
        assert sid not in shared.ASKQ_LADDER

    asyncio.run(run())


def test_new_question_supersedes_prior_ladder(ladder_env, monkeypatch):
    """A second AskUserQuestion before the first answers cancels the prior ladder."""
    hooks = sys.modules["routes.hooks"]
    shared = ladder_env.shared
    shared.ASKQ_T1_SECONDS = 0.5

    touch2_calls: list[str] = []

    async def fake_touch2(instance_id, question_text):
        touch2_calls.append(question_text)

    hooks._askq_touch2_callback = fake_touch2

    async def run():
        sid = "twice-asked"
        await hooks._askq_ladder_start(sid, "first?", [], {"tmux_pane": "%0"})
        first_task = shared.ASKQ_LADDER[sid]["task"]

        await asyncio.sleep(0.02)
        await hooks._askq_ladder_start(sid, "second?", [], {"tmux_pane": "%0"})

        # First task must have been cancelled in favor of the second.
        await asyncio.sleep(0.05)
        assert first_task.cancelled() or first_task.done()
        assert shared.ASKQ_LADDER[sid]["question_text"] == "second?"

        # Clean up to avoid trailing touch2 firing during teardown.
        await hooks._askq_ladder_cancel(sid, reason="test_cleanup")

    asyncio.run(run())


def test_question_persistence_records_answer(ladder_env):
    hooks = sys.modules["routes.hooks"]
    shared = ladder_env.shared
    shared.ASKQ_T1_SECONDS = 0.5

    async def run():
        sid = "persist-answer"
        await hooks._askq_ladder_start(
            sid,
            "Pick a path?",
            ["Alpha", "Beta"],
            {"tab_name": "palace:NE", "legion": "custodes", "tmux_pane": "%0"},
            questions=[
                {
                    "header": "Choice",
                    "question": "Pick a path?",
                    "options": [
                        {"label": "Alpha", "description": "First route"},
                        {"label": "Beta", "description": "Second route"},
                    ],
                }
            ],
        )
        await hooks._askq_ladder_cancel(sid, reason="answered", answer="Alpha")

    asyncio.run(run())

    questions_path = Path(hooks._imperium_env_root()) / "Terra" / "Inbox" / "Questions.md"
    text = questions_path.read_text(encoding="utf-8")
    assert 'title: "AskUserQuestion Log"' in text
    assert "## " in text
    assert "palace:NE / custodes" in text
    assert "- Status: answered" in text
    assert "- Answer: Alpha" in text
    assert "**Choice**" in text
    assert "- **Alpha** — First route" in text


def test_question_persistence_records_bust_queue(ladder_env, monkeypatch):
    hooks = sys.modules["routes.hooks"]

    async def fake_touch2(instance_id, question_text):
        return None

    async def fake_queue_tts(instance_id, text, queue_target="hot"):
        return None

    async def fake_send_bust(instance_id, state):
        return None

    hooks._askq_touch2_callback = fake_touch2
    monkeypatch.setattr(hooks, "queue_tts", fake_queue_tts)
    monkeypatch.setattr(hooks, "_askq_send_bust_prompt", fake_send_bust)

    async def run():
        await hooks._askq_ladder_start(
            "persist-bust",
            "Are you there?",
            ["Yes"],
            {"tab_name": "watch", "legion": "custodes", "tmux_pane": "%1"},
        )
        await asyncio.sleep(0.4)

    asyncio.run(run())

    root = Path(hooks._imperium_env_root())
    questions = (root / "Terra" / "Inbox" / "Questions.md").read_text(encoding="utf-8")
    unanswered = (root / "Terra" / "Inbox" / "Unanswered.md").read_text(encoding="utf-8")
    assert "- Status: bust" in questions
    assert "- Answer: <bust>" in questions
    assert 'title: "Unanswered Questions"' in unanswered
    assert "- [ ] Answer asynchronously" in unanswered
    assert "Are you there?" in unanswered


def test_hook_handlers_persist_question_and_answer(ladder_env, monkeypatch):
    hooks = sys.modules["routes.hooks"]
    shared = ladder_env.shared
    sid = "hook-persist"

    async def fake_queue_tts(instance_id, text, queue_target="hot"):
        return None

    monkeypatch.setattr(hooks, "queue_tts", fake_queue_tts)
    shared.VOICE_CHAT_SESSIONS[sid] = {"active": True, "tmux_pane": "%2"}

    conn = sqlite3.connect(ladder_env.db_path)
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id, status,
            instance_type, legion, tmux_pane)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            sid,
            sid,
            "palace:SE",
            "/tmp",
            "local",
            "Mac-Mini",
            "idle",
            "one_off",
            "custodes",
            "%2",
        ),
    )
    conn.commit()
    conn.close()

    async def run():
        pre = await hooks.handle_pre_tool_use(
            {
                "session_id": sid,
                "tool_name": "AskUserQuestion",
                "tool_input": {
                    "questions": [
                        {
                            "header": "Decision",
                            "question": "Proceed?",
                            "options": ["Yes", "No"],
                        }
                    ]
                },
            }
        )
        assert pre["success"] is True

        post = await hooks.handle_post_tool_use(
            {
                "session_id": sid,
                "tool_name": "AskUserQuestion",
                "tool_response": {"answer": "Yes"},
            }
        )
        assert post["success"] is True

    asyncio.run(run())

    questions_path = Path(hooks._imperium_env_root()) / "Terra" / "Inbox" / "Questions.md"
    text = questions_path.read_text(encoding="utf-8")
    assert "palace:SE / custodes" in text
    assert "- Status: answered" in text
    assert "- Answer: Yes" in text
    shared.VOICE_CHAT_SESSIONS.pop(sid, None)
