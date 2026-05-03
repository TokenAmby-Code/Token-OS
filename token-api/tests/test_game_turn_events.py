import json
import sqlite3


def test_game_turn_endpoint_records_observational_event(app_env):
    from fastapi.testclient import TestClient

    app_env.main.DESKTOP_STATE["work_mode"] = "clocked_out"

    client = TestClient(app_env.main.app)
    resp = client.post(
        "/games/turn",
        json={
            "game": "mewgenics",
            "steam_app_id": "686060",
            "steam_app_name": "Mewgenics",
            "steam_exe": "Mewgenics.exe",
            "source": "ahk",
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "recorded": True,
        "block": False,
        "reason": "observational_only",
        "ack_id": None,
    }

    conn = sqlite3.connect(app_env.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT event_type, device_id, details FROM events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    assert row["event_type"] == "game_turn_end"
    assert row["device_id"] == "desktop"
    details = json.loads(row["details"])
    assert details["game"] == "mewgenics"
    assert details["steam_app_id"] == "686060"
    assert details["steam_app_name"] == "Mewgenics"
    assert details["steam_exe"] == "Mewgenics.exe"
    assert details["source"] == "ahk"


def test_desktop_gaming_detection_persists_steam_metadata(app_env):
    from fastapi.testclient import TestClient

    app_env.main.DESKTOP_STATE["current_mode"] = "silence"
    app_env.main.DESKTOP_STATE["work_mode"] = "clocked_out"

    client = TestClient(app_env.main.app)
    resp = client.post(
        "/desktop",
        json={
            "detected_mode": "gaming",
            "window_title": "Mewgenics",
            "steam_app_id": "686060",
            "steam_app_name": "Mewgenics",
            "steam_exe": "mewgenics.exe",
            "source": "ahk",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["action"] == "mode_changed"
    assert app_env.main.DESKTOP_STATE["steam_app_id"] == "686060"
    assert app_env.main.DESKTOP_STATE["steam_app_name"] == "Mewgenics"
    assert app_env.main.DESKTOP_STATE["steam_exe"] == "mewgenics.exe"

    conn = sqlite3.connect(app_env.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT details FROM events WHERE event_type = 'desktop_mode_change' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    details = json.loads(row["details"])
    assert details["new_mode"] == "gaming"
    assert details["steam_app_id"] == "686060"
    assert details["steam_app_name"] == "Mewgenics"
    assert details["steam_exe"] == "mewgenics.exe"
