from __future__ import annotations

import json
import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "skills-sync"


def _write_skill(
    root: pathlib.Path, folder: str, name: str | None = None, filename: str = "SKILL.md"
):
    d = root / folder
    d.mkdir(parents=True)
    (d / filename).write_text(
        f"---\nname: {name or folder}\ndescription: Test skill {folder}\n---\n\n# {folder}\n",
        encoding="utf-8",
    )
    return d


def _write_openai_yaml(skill: pathlib.Path, allow_implicit: bool):
    agents = skill / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "openai.yaml").write_text(
        "interface:\n"
        f'  display_name: "{skill.name}"\n'
        f'  short_description: "Test skill {skill.name}"\n'
        f'  default_prompt: "${skill.name} test"\n'
        "\n"
        "policy:\n"
        f"  allow_implicit_invocation: {str(allow_implicit).lower()}\n",
        encoding="utf-8",
    )


def _run(tmp_path: pathlib.Path, canonical: pathlib.Path, *args: str):
    env = os.environ.copy()
    env["SKILLS_SYNC_HOME"] = str(tmp_path / "home")
    env["SKILLS_SYNC_CANONICAL"] = str(canonical)
    return subprocess.run([str(SCRIPT), *args], text=True, capture_output=True, env=env)


def test_skills_sync_install_preserves_system_and_links_shared_skills(tmp_path):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    skill = _write_skill(canonical, "preplan")
    _write_openai_yaml(skill, allow_implicit=True)
    aux = _write_skill(canonical, "aux")
    commands = tmp_path / "home" / ".claude" / "commands"
    commands.mkdir(parents=True)
    system = tmp_path / "home" / ".codex" / "skills" / ".system"
    system.mkdir(parents=True)
    (system / "sentinel").write_text("keep", encoding="utf-8")

    install = _run(tmp_path, canonical, "--install", "--json")
    assert install.returncode == 0, install.stdout + install.stderr
    data = json.loads(install.stdout)
    assert data["success"] is True
    assert (system / "sentinel").read_text(encoding="utf-8") == "keep"
    assert (tmp_path / "home" / ".claude" / "skills").resolve() == canonical.resolve()
    assert (tmp_path / "home" / ".codex" / "skills" / "preplan").resolve() == skill.resolve()
    assert (tmp_path / "home" / ".agents" / "skills" / "preplan").resolve() == skill.resolve()
    assert (commands / "preplan.md").resolve() == (skill / "SKILL.md").resolve()
    assert (canonical.parent / "commands" / "preplan.md").resolve() == (
        skill / "SKILL.md"
    ).resolve()
    assert (tmp_path / "home" / ".codex" / "skills" / "aux").resolve() == aux.resolve()

    check = _run(tmp_path, canonical, "--check", "--json")
    assert check.returncode == 0, check.stdout + check.stderr
    checked = json.loads(check.stdout)
    assert checked["skill_count"] == 2


def test_skills_sync_check_reports_duplicate_names(tmp_path):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    _write_skill(canonical, "one", name="dupe")
    _write_skill(canonical, "two", name="dupe")

    result = _run(tmp_path, canonical, "--check", "--json")
    assert result.returncode == 1
    data = json.loads(result.stdout)
    assert any(f["code"] == "duplicate_skill_name" for f in data["findings"])


def test_skills_sync_check_rejects_preplan_hidden_from_codex_literal_invocation(tmp_path):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    skill = _write_skill(canonical, "preplan")
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    text = text.replace(
        "description: Test skill preplan\n",
        "description: Test skill preplan\ndisable-model-invocation: true\n",
    )
    (skill / "SKILL.md").write_text(text, encoding="utf-8")
    _write_openai_yaml(skill, allow_implicit=False)

    result = _run(tmp_path, canonical, "--check", "--json")
    assert result.returncode == 1
    data = json.loads(result.stdout)
    codes = {f["code"] for f in data["findings"]}
    assert "codex_skill_model_invocation_disabled" in codes
    assert "preplan_not_model_visible" in codes


def test_skills_sync_check_requires_preplan_openai_visibility_policy(tmp_path):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    _write_skill(canonical, "preplan")

    result = _run(tmp_path, canonical, "--check", "--json")
    assert result.returncode == 1
    data = json.loads(result.stdout)
    assert any(f["code"] == "preplan_not_model_visible" for f in data["findings"])


def test_skills_sync_skip_commands_repairs_all_skill_roots_without_command_shims(tmp_path):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    skill = _write_skill(canonical, "sample")
    home = tmp_path / "home"
    protected_commands = home / ".claude" / "commands"
    protected_commands.mkdir(parents=True)
    (protected_commands / "preplan.md").write_text("do not touch", encoding="utf-8")

    install = _run(tmp_path, canonical, "--install", "--skip-commands", "--json")
    assert install.returncode == 0, install.stdout + install.stderr
    assert (home / ".codex" / "skills" / "sample").resolve() == skill.resolve()
    assert (home / ".agents" / "skills" / "sample").resolve() == skill.resolve()
    assert (protected_commands / "preplan.md").read_text(encoding="utf-8") == "do not touch"
    assert (home / ".claude" / "skills").resolve() == canonical.resolve()
