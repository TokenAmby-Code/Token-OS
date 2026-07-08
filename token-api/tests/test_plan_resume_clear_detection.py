from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "routes" / "hooks.py"


def _load_hooks():
    spec = importlib.util.spec_from_file_location("hooks_for_plan_resume_detection", HOOKS)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_resume_plan_mode_exit_requests_clear_from_recent_transcript(tmp_path: Path):
    hooks = _load_hooks()
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text(
        "\n".join(
            [
                '{"type":"assistant","message":{"content":"older"}}',
                '{"type":"system","content":"SessionStart:resume"}',
                '{"type":"event_msg","payload":{"type":"plan_mode_exit"}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert (
        asyncio.run(
            hooks._session_start_needs_plan_resume_clear(
                {
                    "source": "resume",
                    "transcript_path": str(transcript),
                }
            )
        )
        is True
    )


def test_prior_clear_suppresses_plan_resume_clear_loop(tmp_path: Path):
    hooks = _load_hooks()
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text(
        '{"type":"event_msg","payload":{"type":"plan_mode_exit"}}\n'
        '{"type":"system","content":"SessionStart:clear"}\n',
        encoding="utf-8",
    )
    assert (
        asyncio.run(
            hooks._session_start_needs_plan_resume_clear(
                {
                    "source": "resume",
                    "transcript_path": str(transcript),
                }
            )
        )
        is False
    )


def test_non_resume_does_not_request_plan_resume_clear(tmp_path: Path):
    hooks = _load_hooks()
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text(
        '{"type":"event_msg","payload":{"type":"plan_mode_exit"}}\n', encoding="utf-8"
    )
    assert (
        asyncio.run(
            hooks._session_start_needs_plan_resume_clear(
                {
                    "source": "clear",
                    "transcript_path": str(transcript),
                }
            )
        )
        is False
    )
