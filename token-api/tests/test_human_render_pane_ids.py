from __future__ import annotations

import pytest

import human_render
import talk


def test_report_render_translates_raw_pane_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_translate(text: str, *, unresolved: str = "unresolved") -> str:
        return text.replace("%51", "mechanicus:1").replace("%52", "council:custodes")

    monkeypatch.setattr(human_render, "_translate_with_tmuxctl", fake_translate)

    rendered = human_render.sanitize_human_render_text_sync(
        "Worker %51 reported to %52; no raw ids should leak."
    )

    assert rendered == "Worker mechanicus:1 reported to council:custodes; no raw ids should leak."
    assert "%" not in rendered


def test_unresolvable_pane_id_fails_safe_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_translate(text: str, *, unresolved: str = "unresolved") -> str:
        raise RuntimeError("tmux unavailable")

    monkeypatch.setattr(human_render, "_translate_with_tmuxctl", fail_translate)

    rendered = human_render.sanitize_human_render_text_sync("Report mentioned %404.")

    assert rendered == "Report mentioned unresolved."
    assert "%404" not in rendered


@pytest.mark.asyncio
async def test_programmatic_raw_pane_resolution_path_is_unchanged() -> None:
    # Regression guard for #394: render-layer sanitization must not alter raw pane
    # ids that are being used as programmatic tmux targets.
    assert await talk.resolve_pane("%51") == "%51"


def test_tts_public_sanitizer_uses_render_translation(monkeypatch: pytest.MonkeyPatch) -> None:
    from routes import tts

    monkeypatch.setattr(
        tts,
        "sanitize_human_render_text_sync",
        lambda text: str(text).replace("%51", "mechanicus:1"),
    )

    rendered = tts._sanitize_public_text("TTS should say mechanicus, not %51")

    assert rendered == "TTS should say mechanicus, not mechanicus:1"
    assert "%" not in rendered
