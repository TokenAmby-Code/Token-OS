import importlib.util
import urllib.error
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TALK = REPO_ROOT / "cli-tools" / "bin" / "talk"


def _load_talk_module():
    spec = importlib.util.spec_from_loader("talk_cli", SourceFileLoader("talk_cli", str(TALK)))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _send_record():
    return {
        "status": "open",
        "talk_id": "talk-pr649",
        "caller_pane": "custodes:test",
        "target_pane": "mechanicus:test",
        "delivery": {"status": "sent", "delivered": True, "operation_id": "op-pr649"},
    }


def test_talk_await_http_error_emits_structured_failure_preserving_delivery(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    talk = _load_talk_module()
    send = _send_record()
    monkeypatch.setattr(talk, "_resolve_caller", lambda explicit: explicit or "custodes:test")
    monkeypatch.setattr(talk, "_post", lambda *_a, **_k: send)

    def fake_get(*_a, **_k):
        raise urllib.error.HTTPError(
            "http://token/api/talk/await/talk-pr649", 500, "Internal Server Error", {}, None
        )

    monkeypatch.setattr(talk, "_get", fake_get)

    rc = talk.main(
        [
            "--caller",
            "custodes:test",
            "--pane",
            "mechanicus:test",
            "--timeout",
            "1",
            "--json",
            "probe",
        ]
    )

    assert rc == 1
    out = capsys.readouterr().out
    assert '"status": "await_failed"' in out
    assert '"send_status": "open"' in out
    assert '"delivery"' in out
    assert '"delivered": true' in out
    assert "HTTP Error 500: Internal Server Error" in out


def test_talk_await_url_error_emits_structured_failure_preserving_delivery(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    talk = _load_talk_module()
    send = _send_record()
    monkeypatch.setattr(talk, "_resolve_caller", lambda explicit: explicit or "custodes:test")
    monkeypatch.setattr(talk, "_post", lambda *_a, **_k: send)
    monkeypatch.setattr(
        talk, "_get", lambda *_a, **_k: (_ for _ in ()).throw(urllib.error.URLError("refused"))
    )

    rc = talk.main(
        [
            "--caller",
            "custodes:test",
            "--pane",
            "mechanicus:test",
            "--timeout",
            "1",
            "--json",
            "probe",
        ]
    )

    assert rc == 1
    out = capsys.readouterr().out
    assert '"status": "await_failed"' in out
    assert '"send_status": "open"' in out
    assert '"delivery"' in out
    assert '"delivered": true' in out
    assert "connection error: refused" in out
