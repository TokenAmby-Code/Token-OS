"""Regressions for Token-API's tmuxctld-backed send path."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


async def test_send_prompt_defaults_loopback_with_runtime_agents_db(
    app_env: Any, monkeypatch: Any
) -> None:
    """The canonical runtimes DB layout must still default sends to loopback."""
    main = app_env.main
    calls: list[dict[str, Any]] = []

    def _post(path: str, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append({"path": path, "body": body, "kwargs": kwargs})
        return {
            "ok": True,
            "result": {
                "dispatch_id": "dispatch-runtime-db",
                "payload_hash": "hash-runtime-db",
                "verification_status": "unverified",
                "verified_by": None,
                "pane": body.get("pane"),
            },
        }

    monkeypatch.delenv("TMUXCTLD_URL", raising=False)
    monkeypatch.setattr(main, "DB_PATH", Path.home() / "runtimes" / "database" / "agents.db")
    monkeypatch.setattr(main.shared, "_tmuxctld_post_json", _post)

    result = await main.send_prompt_to_pane("mechanicus:fabricator-general", "send-path red")

    assert result["returncode"] == 0
    assert calls[0]["path"] == "/send-text"
    assert calls[0]["kwargs"]["default_loopback"] is True


async def test_send_prompt_url_none_falls_back_to_tmuxctl_and_surfaces_real_stderr(
    app_env: Any, monkeypatch: Any
) -> None:
    """A missing daemon URL must not collapse to the bare unavailable placeholder."""
    main = app_env.main
    fallback_calls: list[tuple[str, ...]] = []

    async def _fake_subprocess(args: tuple[str, ...] | list[str], **kwargs: Any):
        fallback_calls.append(tuple(args))
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=42,
            stdout="",
            stderr="tmuxctl real send failure: no such pane",
        )

    monkeypatch.setenv("TMUXCTLD_URL", "disabled")
    monkeypatch.setattr(main, "_tmuxctld_default_loopback", lambda: False)
    monkeypatch.setattr(main.shared, "_run_subprocess_offloop", _fake_subprocess)

    result = await main.send_prompt_to_pane("%dead", "payload")

    assert result["returncode"] == 42
    assert result["operation"] == "tmuxctl.send_text_fallback"
    assert "tmuxctl real send failure" in result["stderr"]
    assert result["stderr"] != "tmuxctld unavailable"
    assert fallback_calls
    assert "send-text" in fallback_calls[0]
