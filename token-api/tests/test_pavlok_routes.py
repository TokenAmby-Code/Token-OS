"""Routes that expose Pavlok enforcement over HTTP.

Regression net for the gap where `97bed98` dropped the inline
`/api/pavlok/{status,toggle,zap}` endpoints from main.py, so the `pavlok`
CLI (cli-tools/bin/pavlok) and custodes_watchtower got HTTP 404 and 65
"zaps" landed nowhere. The stimulus function (`send_pavlok_stimulus`)
survived; only the HTTP surface was missing.
"""

import pytest


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    return TestClient(main.app)


main = None


@pytest.fixture(autouse=True)
def _bind_main(app_env):
    global main
    main = app_env.main
    yield


def test_status_returns_200_not_404(client):
    """GET /api/pavlok/status must exist (was 404 after the route drop)."""
    resp = client.get("/api/pavlok/status")
    assert resp.status_code == 200
    body = resp.json()
    # Keys the `pavlok` CLI show_status() reads.
    for key in ("enabled", "token_set", "default_zap_value", "last_stimulus_at"):
        assert key in body, f"status missing {key!r}: {body}"


def test_zap_invokes_send_pavlok_stimulus(client, monkeypatch):
    """POST /api/pavlok/zap must drive send_pavlok_stimulus with the CLI contract."""
    calls = []

    def fake_send(stimulus_type="zap", value=None, reason="manual"):
        calls.append({"stimulus_type": stimulus_type, "value": value, "reason": reason})
        return {"success": True, "type": stimulus_type, "value": value}

    monkeypatch.setattr(main, "send_pavlok_stimulus", fake_send)

    resp = client.post("/api/pavlok/zap", params={"type": "zap", "value": 75, "reason": "break"})
    assert resp.status_code == 200
    assert len(calls) == 1, "send_pavlok_stimulus was not invoked"
    assert calls[0]["stimulus_type"] == "zap"
    assert calls[0]["value"] == 75
    assert calls[0]["reason"] == "break"
    assert resp.json().get("success") is True


def test_zap_passes_beep_type(client, monkeypatch):
    """The CLI sends beep/vibe through the same /zap route via ?type=."""
    calls = []
    monkeypatch.setattr(
        main,
        "send_pavlok_stimulus",
        lambda stimulus_type="zap", value=None, reason="manual": (
            calls.append(stimulus_type) or {"success": True}
        ),
    )
    resp = client.post("/api/pavlok/zap", params={"type": "beep"})
    assert resp.status_code == 200
    assert calls == ["beep"]


def test_toggle_persists_enable_state(client):
    """POST /api/pavlok/toggle with explicit ?enabled flips PAVLOK_CONFIG state."""
    from shared import PAVLOK_CONFIG

    resp = client.post("/api/pavlok/toggle", params={"enabled": "false"})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert PAVLOK_CONFIG["enabled"] is False

    resp = client.post("/api/pavlok/toggle", params={"enabled": "true"})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    assert PAVLOK_CONFIG["enabled"] is True


def test_toggle_no_arg_inverts_state(client):
    """No-arg toggle inverts the current enable state (CLI `pavlok toggle`)."""
    from shared import PAVLOK_CONFIG

    PAVLOK_CONFIG["enabled"] = True
    resp = client.post("/api/pavlok/toggle")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert PAVLOK_CONFIG["enabled"] is False
