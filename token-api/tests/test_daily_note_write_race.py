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

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import dailynote_callout as dc
import session_doc_helpers as sdh
import vault_lock

# Repo layout: this file is token-api/tests/…; the repo root is two parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_OBSIDIAN_CLI = _REPO_ROOT / "cli-tools" / "bin" / "obsidian"
_VAULT_LOCK_PY = Path(__file__).resolve().parents[1] / "vault_lock.py"


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

    assert all(not t.is_alive() for t in threads), "worker thread did not finish (deadlock?)"
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


def test_transform_runs_inside_locked_retry_loop(tmp_path, monkeypatch):
    """A transform that increments a counter must re-run each attempt against the
    freshly-read frontmatter, so a concurrent change can't be clobbered by a
    stale whole-dict overwrite."""
    note = tmp_path / "n.md"
    note.write_text("---\ncounter: 0\n---\nbody\n", encoding="utf-8")

    real_atomic = dc._atomic_write
    state = {"calls": 0}

    def conflict_once(path, content, expected_mtime_ns):
        state["calls"] += 1
        if state["calls"] == 1:
            # Simulate another writer bumping the counter to 5 mid-flight.
            other = path.read_text(encoding="utf-8").replace("counter: 0", "counter: 5")
            path.write_text(other, encoding="utf-8")
            raise dc.CalloutConflictError("changed")
        return real_atomic(path, content, expected_mtime_ns)

    monkeypatch.setattr(dc, "_atomic_write", conflict_once)

    def bump(fm):
        fm["counter"] = int(fm.get("counter", 0)) + 1

    sdh.update_frontmatter(note, transform=bump)

    fm, _ = sdh.read_frontmatter(note)
    # Re-read on retry sees 5, +1 = 6 — the concurrent write to 5 was NOT lost.
    assert fm["counter"] == 6


def test_concurrent_rubric_subkey_updates_no_lost_update(tmp_path):
    """update_rubric_field on two different subkeys of the same rubric, run
    concurrently, must both land (the CodeRabbit-flagged whole-dict-overwrite
    lost-update case)."""
    note = tmp_path / "doc.md"
    note.write_text(
        "---\nrubric_key: victory\nvictory:\n  committed: false\n  pushed: false\n---\nbody\n",
        encoding="utf-8",
    )
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def flip(subkey):
        try:
            barrier.wait(timeout=5)
            for _ in range(20):
                sdh.update_rubric_field(note, subkey, True)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=flip, args=("committed",))
    t2 = threading.Thread(target=flip, args=("pushed",))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not t1.is_alive() and not t2.is_alive(), "worker thread did not finish"
    assert not errors, errors
    fm, _ = sdh.read_frontmatter(note)
    assert fm["victory"]["committed"] is True
    assert fm["victory"]["pushed"] is True


# ── Write-skip guard (churn kill) ─────────────────────────────────────────────


def test_apply_callout_skips_write_when_unchanged(tmp_path):
    """A second identical callout write must not move mtime (byte-identical → no-op)."""
    note = _daily_note(tmp_path)
    # Seed a distinct callout body so the first write actually changes the file.
    first = dc.apply_callout(note, "now", "fresh-tick", title="NOW")
    assert first.action in ("replaced", "appended")
    mtime_after_first = note.stat().st_mtime_ns

    time.sleep(0.01)  # ensure the clock could advance if a write happened
    second = dc.apply_callout(note, "now", "fresh-tick", title="NOW")

    assert second.action == "unchanged"
    assert second.bytes_written == 0
    assert note.stat().st_mtime_ns == mtime_after_first, "unchanged callout still rewrote the note"


def test_update_frontmatter_skips_write_when_unchanged(tmp_path):
    """A second identical frontmatter update must not move mtime."""
    note = _daily_note(tmp_path)
    sdh.update_frontmatter(note, {"timer_status": "working"})
    mtime_after_first = note.stat().st_mtime_ns

    time.sleep(0.01)
    out = sdh.update_frontmatter(note, {"timer_status": "working"})

    assert out["timer_status"] == "working"
    assert note.stat().st_mtime_ns == mtime_after_first, (
        "unchanged frontmatter still rewrote the note"
    )


# ── Cross-process lock derivation (bash ↔ python agreement) ───────────────────


def test_lock_path_for_is_stable_and_resolved(tmp_path):
    note = tmp_path / "n.md"
    note.write_text("x", encoding="utf-8")
    a = vault_lock.lock_path_for(note)
    b = vault_lock.lock_path_for(str(note))
    assert a == b
    # Lives under the local tempdir, never beside the note.
    assert "imperium-vault-locks" in a
    assert not a.startswith(str(tmp_path))


def test_lock_path_for_matches_cli_derivation(tmp_path):
    """The lockfile the bash CLI grabs (via vault_lock.py) must equal the one
    token-api derives in-process for the same path — else they don't serialize."""
    note = tmp_path / "Terra" / "Journal" / "Daily" / "2026-06-17.md"
    note.parent.mkdir(parents=True)
    note.write_text("x", encoding="utf-8")

    py_lock = vault_lock.lock_path_for(note)
    # Run the SAME code the bash CLI shells into: vault_lock.py acquires the lock
    # then runs a child that prints the lockfile it would derive for the path.
    printer = "import sys, vault_lock; print(vault_lock.lock_path_for(sys.argv[1]))"
    env = {**os.environ, "PYTHONPATH": str(_VAULT_LOCK_PY.parent)}
    out = subprocess.check_output(
        [
            sys.executable,
            str(_VAULT_LOCK_PY),
            str(note),
            "--",
            sys.executable,
            "-c",
            printer,
            str(note),
        ],
        text=True,
        env=env,
    ).strip()
    assert out == py_lock


# ── Concurrent-writer stress: in-proc vs auto-flocked obsidian CLI ────────────


@pytest.mark.skipif(
    shutil.which("bash") is None or not _OBSIDIAN_CLI.exists(),
    reason="needs bash + the obsidian CLI",
)
def test_concurrent_inproc_and_cli_writers_no_lost_update(tmp_path):
    """Race in-proc update_frontmatter against subprocess `obsidian append` AND
    `property:set`, all auto-flocked on the same daily note. Every appended line
    must survive and the frontmatter must parse with the expected keys."""
    home = tmp_path / "home"
    vault_root = home / "TestVault"
    daily_dir = vault_root / "Terra" / "Journal" / "Daily"
    daily_dir.mkdir(parents=True)
    rel_path = "Terra/Journal/Daily/2026-06-17.md"
    note = daily_dir / "2026-06-17.md"

    # Env for the CLI: resolve the vault under HOME, and point LOCK_HELPER at our
    # real vault_lock.py via TOKEN_OS so needs_lock auto-flocks the daily note.
    cli_env = {
        **os.environ,
        "HOME": str(home),
        "TOKEN_OS": str(_REPO_ROOT),
    }

    def cli(*args):
        subprocess.run(
            [str(_OBSIDIAN_CLI), "vault=TestVault", *args],
            check=True,
            env=cli_env,
            capture_output=True,
            text=True,
        )

    def run_one_round(round_idx):
        """One race round, factored out so the worker closures bind function-local
        names (not loop variables — avoids the B023 late-binding trap)."""
        note.write_text(
            "---\ndate: 2026-06-17\ntype: daily-note\ntimer_status: idle\n---\n\n# 2026-06-17\n",
            encoding="utf-8",
        )
        appended = [f"APPEND-{round_idx}-{i}-must-survive" for i in range(8)]
        errors: list[BaseException] = []

        def run_frontmatter():
            try:
                for i in range(30):
                    sdh.update_frontmatter(note, {"timer_status": f"s{i}"})
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        def run_appends():
            try:
                for line in appended:
                    cli("append", f"path={rel_path}", f"content={line}")
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        def run_property():
            try:
                for i in range(8):
                    cli("property:set", f"path={rel_path}", "property=lap", f"value={i}")
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [
            threading.Thread(target=run_frontmatter),
            threading.Thread(target=run_appends),
            threading.Thread(target=run_property),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert all(not t.is_alive() for t in threads), "writer thread hung (deadlock?)"
        assert not errors, f"writer raised under contention: {errors}"

        final = note.read_text(encoding="utf-8")
        for line in appended:
            assert line in final, f"lost append: {line!r}"
        fm, _ = sdh.read_frontmatter(note)
        assert "timer_status" in fm and str(fm["timer_status"]).startswith("s")
        assert "lap" in fm  # property:set landed and survived the frontmatter rewrites

    for round_idx in range(3):
        run_one_round(round_idx)


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

    assert not t1.is_alive() and not t2.is_alive(), "worker thread did not finish (deadlock?)"
    assert not errors, errors
    fm, _ = sdh.read_frontmatter(note)
    assert fm["timer_status"] == "working"
    assert fm["timer_work_time"] == "01:00:00"
