from pathlib import Path

import yaml


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    fm_text = text.split("---\n", 2)[1]
    return yaml.safe_load(fm_text) or {}


def test_fresh_session_doc_omits_pool_field(app_env, tmp_path):
    doc_path = tmp_path / "session.md"
    app_env.main.create_session_doc_file(doc_path, "No Pool", 123, project="tests")

    fm = _frontmatter(doc_path)
    assert "pool" not in fm
