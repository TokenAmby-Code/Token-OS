from fastapi.testclient import TestClient


def test_legacy_discord_trials_scanner_removed_for_new_lifecycle(app_env, tmp_path, monkeypatch):
    aspirants = tmp_path / "Aspirants"
    aspirants.mkdir()
    note = aspirants / "new-lifecycle.md"
    note.write_text(
        "---\n"
        "status: aspirant_trials\n"
        "aspirant: true\n"
        "questions:\n"
        "  - question: blocker?\n"
        "    state: open\n"
        "---\n"
        "# New lifecycle\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(app_env.main, "OBSIDIAN_INBOX_PATH", aspirants)

    client = TestClient(app_env.main.app)
    response = client.post("/api/inbox/trials-check")
    assert response.status_code == 404
    assert "status: aspirant_trials" in note.read_text(encoding="utf-8")
