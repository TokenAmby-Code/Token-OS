import json
import subprocess
import sys

import pytest

from questions_gate import trials_clear, trials_report


def _note(tmp_path, frontmatter: str, name: str = "session.md") -> str:
    p = tmp_path / name
    p.write_text(frontmatter, encoding="utf-8")
    return str(p)


def test_empty_questions_clear(tmp_path):
    path = _note(tmp_path, "---\nquestions: []\n---\n\nbody\n")
    assert trials_clear(path) == (True, [])


def test_missing_questions_clear(tmp_path):
    path = _note(tmp_path, "---\ntitle: Test\n---\n\nbody\n")
    assert trials_clear(path) == (True, [])


def test_all_closed_clear(tmp_path):
    path = _note(
        tmp_path,
        """---
questions:
  - question: done
    answer: yes
    state: closed
    importance: 3
---
body
""",
    )
    clear, blockers = trials_clear(path)
    assert clear is True
    assert blockers == []


def test_mixed_states_not_clear(tmp_path):
    path = _note(
        tmp_path,
        """---
questions:
  - question: done
    answer: yes
    state: closed
    importance: 3
  - question: blocked
    answer: null
    state: open
    importance: 8
---
body
""",
    )
    clear, blockers = trials_clear(path)
    assert clear is False
    assert [b["question"] for b in blockers] == ["blocked"]


def test_malformed_frontmatter_value_error(tmp_path):
    path = _note(tmp_path, "---\nquestions: [\n---\nbody\n")
    with pytest.raises(ValueError):
        trials_clear(path)


def test_cli_exit_codes(tmp_path):
    clear_path = _note(tmp_path, "---\nquestions: []\n---\n", "clear.md")
    blocked_path = _note(
        tmp_path,
        """---
questions:
  - question: blocked
    answer: null
    state: unanswered
    importance: 5
---
""",
        "blocked.md",
    )
    assert subprocess.run([sys.executable, "-m", "questions_gate", clear_path]).returncode == 0
    blocked = subprocess.run(
        [sys.executable, "-m", "questions_gate", blocked_path], capture_output=True, text=True
    )
    assert blocked.returncode == 1
    assert "trials_clear: false" in blocked.stdout


def test_cli_json_output_shape(tmp_path):
    path = _note(tmp_path, "---\nquestions: []\n---\n")
    result = subprocess.run(
        [sys.executable, "-m", "questions_gate", path, "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    assert data == {"clear": True, "total": 0, "closed": 0, "blockers": [], "path": path}


def test_blockers_sorted_by_importance_desc_then_original_index(tmp_path):
    path = _note(
        tmp_path,
        """---
questions:
  - question: later same priority
    answer: null
    state: open
    importance: 7
  - question: highest
    answer: null
    state: unanswered
    importance: 10
  - question: earlier same priority
    answer: null
    state: refining
    importance: 7
---
""",
    )
    report = trials_report(path)
    assert [b["question"] for b in report["blockers"]] == [
        "highest",
        "later same priority",
        "earlier same priority",
    ]
