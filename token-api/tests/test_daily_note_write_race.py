"""Regression tests for the daily-note timer write-race (P0 2026-06-17).

The timer frontmatter writer (`update_frontmatter`) used to do a non-atomic
`read → fm.update → write_text(serialize(fm, stale_body))`, rewriting the WHOLE
file from a body it read before the write. Any append/Edit that landed between
its read and write — including the atomic callout writer and external
`obsidian append` — was silently lost, and frontmatter lists like
`agents`/`instance_ids` were reset.

These tests pin the fix: `update_frontmatter` is now surgical (frontmatter-only
splice), atomic (temp file + os.replace), mtime-guarded with retry, and
serialized against the callout writer via a shared per-file lock.

They use only temp notes + threads — they never touch the live tmux session,
the live DB, or the live vault.
"""

from __future__ import annotations

import threading
import time

import pytest

import dailynote_callout as dc
import session_doc_helpers as sdh


def _daily_note(tmp_path):
    """A realistic daily note: frontmatter lists + a NOW callout + body sections."""
    note = tmp_path / "2026-06-17.md"
    note.write_text(
        "---\n"
        "session_doc_id: 42\n"
        "date: 2026-06-17\n"
        "type: daily-note\n"
        "agents:\n"
        "- custodes-abc\n"
        "- guilliman-def\n"
        "instance_ids:\n"
        "- 101\n"
        "- 202\n"
        "timer_status: idle\n"
        "---\n"
        "\n"
        "# 2026-06-17\n"
        "\n"
        "<!-- callout:now BEGIN -->\n"
        "> [!info]+ NOW\n"
        "> working\n"
        "<!-- callout:now END -->\n"
        "\n"
        "## Regressions\n"
        "### Fix-locus one\n"
        "First digest body — must survive.\n",
        encoding="utf-8",
    )
    return note


def test_frontmatter_only_write_preserves_body_bytes(tmp_path):
    """Setting timer_* keys must leave the body region byte-for-byte unchanged."""
    note = _daily_note(tmp_path)
    before = note.read_text(encoding="utf-8")
    body_marker = before.index("# 2026-06-17")
    body_before = before[body_marker:]

    sdh.update_frontmatter(
        note,
        {"timer_status": "working", "last_timer_update": "12:34:56"},
    )

    after = note.read_text(encoding="utf-8")
    fm, _ = sdh.read_frontmatter(note)
    # Only the timer keys changed.
    assert fm["timer_status"] == "working"
    assert fm["last_timer_update"] == "12:34:56"
    # Body is untouched, byte-for-byte.
    assert after[after.index("# 2026-06-17") :] == body_before
    assert "First digest body — must survive." in after
    assert "<!-- callout:now BEGIN -->" in after


def test_agents_and_instance_ids_preserved(tmp_path):
    """A timer frontmatter update must never reset agents / instance_ids."""
    note = _daily_note(tmp_path)

    sdh.update_frontmatter(note, {"timer_status": "working"})

    fm, _ = sdh.read_frontmatter(note)
    assert fm["agents"] == ["custodes-abc", "guilliman-def"]
    assert fm["instance_ids"] == [101, 202]


def test_external_body_append_during_update_survives(tmp_path, monkeypatch):
    """The lost-update case: a body append that lands AFTER the writer reads but
    BEFORE it writes must survive (mtime guard forces a re-read + retry)."""
    note = _daily_note(tmp_path)
    sentinel = "SENTINEL-EXTERNAL-APPEND-must-survive"

    real_parse = sdh.parse_frontmatter
    state = {"injected": False}

    def parse_then_append(content):
        # On the first read, simulate an external `obsidian append` (O_APPEND >>)
        # landing right after we read but before we write. The mtime changes, so
        # the atomic write must conflict, retry, and re-read the appended body.
        result = real_parse(content)
        if not state["injected"]:
            state["injected"] = True
            with open(note, "a", encoding="utf-8") as f:
                f.write(f"\n{sentinel}\n")
        return result

    monkeypatch.setattr(sdh, "parse_frontmatter", parse_then_append)

    sdh.update_frontmatter(note, {"timer_status": "working"})

    final = note.read_text(encoding="utf-8")
    assert sentinel in final, "external append was clobbered by the frontmatter writer"
    fm, _ = sdh.read_frontmatter(note)
    assert fm["timer_status"] == "working"
    # Pre-existing body and lists also survived.
    assert "First digest body — must survive." in final
    assert fm["agents"] == ["custodes-abc", "guilliman-def"]


def test_callout_writer_and_frontmatter_writer_serialize(tmp_path):
    """Hammer both writers concurrently; neither loses the other's update and the
    body sections are never collapsed."""
    note = _daily_note(tmp_path)
    errors: list[BaseException] = []

    def run_callout():
        try:
            for i in range(40):
                dc.apply_callout(note, "now", f"tick {i}", title="NOW")
        except BaseException as e:  # noqa: BLE001 - surface in assertion
            errors.append(e)

    def run_frontmatter():
        try:
            for i in range(40):
                sdh.update_frontmatter(note, {"timer_status": f"s{i}"})
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=run_callout), threading.Thread(target=run_frontmatter)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"writer raised under contention: {errors}"
    final = note.read_text(encoding="utf-8")
    # Body section never collapsed away.
    assert "## Regressions" in final
    assert "First digest body — must survive." in final
    # Exactly one callout region, well-formed.
    assert final.count("<!-- callout:now BEGIN -->") == 1
    assert final.count("<!-- callout:now END -->") == 1
    # Frontmatter lists intact.
    fm, _ = sdh.read_frontmatter(note)
    assert fm["agents"] == ["custodes-abc", "guilliman-def"]
    assert fm["instance_ids"] == [101, 202]


def test_shared_lock_is_same_object_across_callers(tmp_path):
    note = _daily_note(tmp_path)
    a = dc.file_write_lock(note)
    b = dc.file_write_lock(str(note))
    assert a is b


def test_atomic_write_failure_leaves_original_and_cleans_temp(tmp_path, monkeypatch):
    note = _daily_note(tmp_path)
    original = note.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("simulated replace crash")

    monkeypatch.setattr(dc.os, "replace", boom)

    with pytest.raises(OSError):
        sdh.update_frontmatter(note, {"timer_status": "working"})

    assert note.read_text(encoding="utf-8") == original
    # No leftover temp files in the dir.
    assert sorted(p.name for p in tmp_path.iterdir()) == [note.name]


def test_conflict_retries_then_raises(tmp_path, monkeypatch):
    note = _daily_note(tmp_path)

    def always_conflict(path, content, expected_mtime_ns):
        raise dc.CalloutConflictError("changed")

    monkeypatch.setattr(dc, "_atomic_write", always_conflict)

    with pytest.raises(dc.CalloutConflictError):
        sdh.update_frontmatter(note, {"timer_status": "working"}, max_attempts=2)


def test_splice_frontmatter_no_frontmatter_prepends(tmp_path):
    content = "# Just a body\n\nno frontmatter here.\n"
    out = sdh.splice_frontmatter(content, {"timer_status": "idle"})
    fm, body = sdh.parse_frontmatter(out)
    assert fm == {"timer_status": "idle"}
    assert "# Just a body" in body


@pytest.mark.parametrize(
    "content",
    [
        "# no fm\nbody\n",  # no leading fence
        "---\n---\nbody\n",  # empty (non-dict) frontmatter block
        "---\nnot a dict just a string\n---\nx\n",  # scalar yaml, not a dict
    ],
)
def test_splice_no_valid_frontmatter_matches_legacy_serialize(content):
    """For inputs with no parseable frontmatter dict, the surgical splice must
    fall back to exactly the pre-fix serialize_frontmatter behavior (no new
    divergence in degenerate cases)."""
    fm, body = sdh.parse_frontmatter(content)
    fm = {**fm, "added": True}
    assert sdh.splice_frontmatter(content, fm) == sdh.serialize_frontmatter(fm, body)


def test_update_frontmatter_returns_merged_dict(tmp_path):
    note = _daily_note(tmp_path)
    out = sdh.update_frontmatter(note, {"timer_status": "working"})
    assert out["timer_status"] == "working"
    assert out["agents"] == ["custodes-abc", "guilliman-def"]


def test_delete_keys_still_works(tmp_path):
    note = _daily_note(tmp_path)
    sdh.update_frontmatter(note, {"temp_key": "x"})
    sdh.update_frontmatter(note, {}, delete_keys=["temp_key"])
    fm, _ = sdh.read_frontmatter(note)
    assert "temp_key" not in fm
    # Unrelated keys preserved.
    assert fm["agents"] == ["custodes-abc", "guilliman-def"]


def test_concurrent_frontmatter_updates_no_lost_update(tmp_path):
    """Two threads each set a distinct key; both must land (no last-writer-wins
    over the whole frontmatter)."""
    note = _daily_note(tmp_path)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def setter(key, val):
        try:
            barrier.wait(timeout=5)
            time.sleep(0.001)
            sdh.update_frontmatter(note, {key: val})
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=setter, args=("timer_status", "working"))
    t2 = threading.Thread(target=setter, args=("timer_work_time", "01:00:00"))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, errors
    fm, _ = sdh.read_frontmatter(note)
    assert fm["timer_status"] == "working"
    assert fm["timer_work_time"] == "01:00:00"
