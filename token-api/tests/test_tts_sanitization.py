"""TTS speech sanitization unit tests.

These tests stay at the sanitizer/chokepoint layer: no live TTS engine, no tmux.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

TOKEN_API_DIR = Path(__file__).resolve().parents[1]


def _load_tts():
    if str(TOKEN_API_DIR) not in sys.path:
        sys.path.insert(0, str(TOKEN_API_DIR))
    return importlib.import_module("routes.tts")


def test_sanitizer_path_uses_final_segment_drops_extension_and_collapses_separators() -> None:
    tts = _load_tts()

    assert (
        tts.sanitize_tts_for_speech("Review /Volumes/Imperium/Mars/Bugs/tmux-foo_bar.md now")
        == "Review tmux foo bar now"
    )


def test_sanitizer_collapses_kebab_and_snake_tokens() -> None:
    tts = _load_tts()

    assert (
        tts.sanitize_tts_for_speech("kebab-case-it snake_case_it") == "kebab case it snake case it"
    )


def test_sanitizer_elides_commit_sha_tokens() -> None:
    tts = _load_tts()

    assert (
        tts.sanitize_tts_for_speech("merged c68a314ef4394bb9dc73dae2e2b5d50857a7b625")
        == "merged commit"
    )
    assert tts.sanitize_tts_for_speech("base 566b697") == "base commit"


def test_sanitizer_sha_false_positive_guards() -> None:
    tts = _load_tts()

    text = "defaced facade well-known abcdef"
    sanitized = tts.sanitize_tts_for_speech(text)
    assert sanitized == "defaced facade well known abcdef"
    assert "commit" not in sanitized


def test_sanitizer_leaves_ordinary_prose_unchanged() -> None:
    tts = _load_tts()

    prose = "The deployment is ready for review after tests pass."
    assert tts.sanitize_tts_for_speech(prose) == prose


def test_speak_tts_sanitizes_once_before_backend_fanout(monkeypatch) -> None:
    tts = _load_tts()
    spoken: list[str] = []

    monkeypatch.setattr(
        tts,
        "resolve_tts_device",
        lambda *a, **k: {"device": "wsl", "reason": "unit test", "discord_bot": None},
    )

    def fake_dispatch(backend: str, chunks: list[dict], **kwargs) -> dict:
        spoken.extend(chunk["text"] for chunk in chunks)
        return {"success": True, "method": backend, "chunks": len(chunks)}

    monkeypatch.setattr(tts, "dispatch_tts_chunks_to_backend", fake_dispatch)

    result = tts.speak_tts(
        "Ship /Volumes/Imperium/Mars/Bugs/tmux-foo-bar.md after c68a314ef4394bb9dc73dae2e2b5d50857a7b625"
    )

    assert result["success"] is True
    assert spoken == ["Ship tmux foo bar after commit"]
