from __future__ import annotations

import pytest

import dailynote_callout as dc


@pytest.mark.parametrize("callout_id", ["now", "now_1", "phase-1", "a0"])
def test_callout_id_regex_accepts_valid_ids(callout_id):
    assert dc.CALLOUT_ID_RE.fullmatch(callout_id)


@pytest.mark.parametrize("callout_id", ["NOW", "now!", "with space", "", "../now"])
def test_callout_id_regex_rejects_invalid_ids(callout_id):
    assert dc.CALLOUT_ID_RE.fullmatch(callout_id) is None


def test_append_when_markers_missing(tmp_path):
    note = tmp_path / "2026-05-09.md"
    note.write_text("# Day\n\nEmperor text stays.\n", encoding="utf-8")

    result = dc.apply_callout(note, "now", "**Block:** test", title="NOW")

    text = note.read_text(encoding="utf-8")
    assert result.action == "appended"
    assert "Emperor text stays." in text
    assert "<!-- callout:now BEGIN -->" in text
    assert "> [!info]+ NOW" in text
    assert "> **Block:** test" in text
    assert text.endswith("<!-- callout:now END -->\n")


def test_replace_existing_marker_region_only(tmp_path):
    note = tmp_path / "2026-05-09.md"
    note.write_text(
        "before\n"
        "<!-- callout:now BEGIN -->\n"
        "> [!info]+ NOW\n"
        "> old\n"
        "<!-- callout:now END -->\n"
        "after\n",
        encoding="utf-8",
    )

    result = dc.apply_callout(note, "now", "new", title="NOW", callout_type="success")

    text = note.read_text(encoding="utf-8")
    assert result.action == "replaced"
    assert text.startswith("before\n")
    assert text.endswith("\nafter\n")
    assert "> old" not in text
    assert "> [!success]+ NOW" in text
    assert text.count("<!-- callout:now BEGIN -->") == 1


def test_idempotent_same_input_identical_bytes(tmp_path):
    note = tmp_path / "2026-05-09.md"
    note.write_text("# Day\n", encoding="utf-8")

    dc.apply_callout(note, "now", "same", title="NOW")
    first = note.read_bytes()
    dc.apply_callout(note, "now", "same", title="NOW")
    second = note.read_bytes()

    assert second == first


def test_blank_lines_are_callout_quoted(tmp_path):
    note = tmp_path / "2026-05-09.md"
    note.write_text("", encoding="utf-8")

    dc.apply_callout(note, "now", "line 1\n\nline 3")

    assert "> line 1\n>\n> line 3" in note.read_text(encoding="utf-8")


@pytest.mark.parametrize("bad_id", ["NOW", "now!", "with space", ""])
def test_invalid_callout_id(tmp_path, bad_id):
    note = tmp_path / "2026-05-09.md"
    note.write_text("", encoding="utf-8")

    with pytest.raises(dc.CalloutError):
        dc.apply_callout(note, bad_id, "content")


def test_invalid_callout_type(tmp_path):
    note = tmp_path / "2026-05-09.md"
    note.write_text("", encoding="utf-8")

    with pytest.raises(dc.CalloutError):
        dc.apply_callout(note, "now", "content", callout_type="danger")


def test_content_size_cap(tmp_path):
    note = tmp_path / "2026-05-09.md"
    note.write_text("", encoding="utf-8")

    with pytest.raises(dc.CalloutError):
        dc.apply_callout(note, "now", "x" * (dc.MAX_CONTENT_BYTES + 1))


def test_malformed_marker_pair_rejected(tmp_path):
    note = tmp_path / "2026-05-09.md"
    note.write_text("<!-- callout:now BEGIN -->\nold\n", encoding="utf-8")

    with pytest.raises(dc.CalloutError):
        dc.apply_callout(note, "now", "new")


def test_atomic_write_failure_leaves_original_and_cleans_temp(tmp_path, monkeypatch):
    note = tmp_path / "2026-05-09.md"
    original = "# Day\n"
    note.write_text(original, encoding="utf-8")

    def boom(src, dst):
        raise OSError("simulated replace crash")

    monkeypatch.setattr(dc.os, "replace", boom)

    with pytest.raises(OSError):
        dc.apply_callout(note, "now", "content")

    assert note.read_text(encoding="utf-8") == original
    assert [p.name for p in tmp_path.iterdir()] == [note.name]


def test_conflict_retries_once(tmp_path, monkeypatch):
    note = tmp_path / "2026-05-09.md"
    note.write_text("# Day\n", encoding="utf-8")
    real_atomic = dc._atomic_write
    calls = {"n": 0}

    def conflict_then_write(path, content, expected_mtime_ns):
        calls["n"] += 1
        if calls["n"] == 1:
            raise dc.CalloutConflictError("changed")
        return real_atomic(path, content, expected_mtime_ns)

    monkeypatch.setattr(dc, "_atomic_write", conflict_then_write)

    result = dc.apply_callout(note, "now", "content")

    assert result.action == "appended"
    assert calls["n"] == 2


def test_conflict_after_retry_raises_409_error_type(tmp_path, monkeypatch):
    note = tmp_path / "2026-05-09.md"
    note.write_text("# Day\n", encoding="utf-8")

    def always_conflict(path, content, expected_mtime_ns):
        raise dc.CalloutConflictError("changed")

    monkeypatch.setattr(dc, "_atomic_write", always_conflict)

    with pytest.raises(dc.CalloutConflictError):
        dc.apply_callout(note, "now", "content")


def test_daily_note_callout_bad_id_returns_400_not_500(app_env, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    note_dir = tmp_path / "Daily"
    note_dir.mkdir()
    (note_dir / "2026-05-09.md").write_text("# 2026-05-09\n", encoding="utf-8")
    monkeypatch.setattr(app_env.main, "DAILY_NOTE_DIR", note_dir)

    client = TestClient(app_env.main.app)
    response = client.put(
        "/api/daily-note/callout",
        json={
            "date": "2026-05-09",
            "callout_id": "bad id!",
            "content": "content",
            "callout_type": "info",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "callout_id must match [a-z0-9_-]+"
