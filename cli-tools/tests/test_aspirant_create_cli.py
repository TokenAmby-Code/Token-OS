import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "cli-tools" / "bin" / "dispatch"
ASPIRANT_CREATE_MODULE = ["python3", "-m", "aspirant_create"]


def env_for(tmp_path):
    vault = tmp_path / "Imperium-ENV"
    vault.mkdir()
    env = os.environ.copy()
    env["IMPERIUM"] = str(tmp_path)
    env["PATH"] = f"{ROOT / 'cli-tools' / 'bin'}:{env.get('PATH', '')}"
    env["PYTHONPATH"] = f"{ROOT / 'cli-tools' / 'lib'}:{env.get('PYTHONPATH', '')}"
    return env, vault


def test_aspirant_create_deploy_p_creates_prescriptive_note(tmp_path):
    env, vault = env_for(tmp_path)
    result = subprocess.run(
        [
            *ASPIRANT_CREATE_MODULE,
            "--json",
            "--kind",
            "deploy_p",
            "--title",
            "Bug followup",
            "--objective",
            "File this bug for later",
            "--source",
            "test",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["kind"] == "deploy_p"
    assert data["session_doc"] is None
    note = vault / data["note_path"]
    text = note.read_text(encoding="utf-8")
    assert "type: prescriptive" in text
    assert "aspirant_kind: deploy_p" in text
    assert "deployment_target: Mars/Tasks" in text


def test_aspirant_create_deploy_d_creates_descriptive_note(tmp_path):
    env, vault = env_for(tmp_path)
    result = subprocess.run(
        [
            *ASPIRANT_CREATE_MODULE,
            "--json",
            "--kind",
            "deploy_d",
            "--title",
            "Reference note",
            "--objective",
            "Remember this concept",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    note = vault / data["note_path"]
    text = note.read_text(encoding="utf-8")
    assert "type: descriptive" in text
    assert "aspirant_kind: deploy_d" in text
    assert "deployment_target: Terra/Ultramar" in text


def test_internal_aspirant_create_dispatch_creates_mars_session_doc_for_trials(tmp_path):
    env, vault = env_for(tmp_path)
    result = subprocess.run(
        [
            *ASPIRANT_CREATE_MODULE,
            "--json",
            "--kind",
            "dispatch",
            "--title",
            "Worker plan",
            "--objective",
            "Implement the worker plan",
            "--engine",
            "claude",
            "--persona",
            "vulkan",
            "--dir",
            str(ROOT),
            "--target",
            "legion:new",
            "--zealotry",
            "4",
            "--victory-condition",
            "Tests pass",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["status"] == "aspirant_trials"
    assert data["dispatch_schema_complete"] is True
    assert data["dispatch_ready"] is False
    assert data["trials_verdict"] == "pending"
    assert data["operator_approved_dispatch"] is False
    assert data["session_doc"].startswith("Mars/Sessions/")
    assert data["session_doc"].endswith("aspirant-worker-plan.md")
    assert not Path(data["session_doc"]).name.startswith("20")
    session_doc = vault / data["session_doc"]
    session_text = session_doc.read_text(encoding="utf-8")
    note_text = (vault / data["note_path"]).read_text(encoding="utf-8")
    assert "status: aspirant_trials" in session_text
    assert "type: session" in session_text
    assert "aspirant: true" in session_text
    assert "aspirant_kind: dispatch" in session_text
    assert "aspirant_persona: aspirant" in session_text
    assert "aspirant_note: " in session_text
    assert "dispatch_schema_complete: true" in session_text
    assert "dispatch_ready: false" in session_text
    assert "trials_verdict: pending" in session_text
    assert "operator_approved_dispatch: false" in session_text
    assert "questions:" in session_text
    assert '  - question: "which other questions are needed for this aspirant?"' in session_text
    assert '    state: "unanswered"' in session_text
    assert "    importance: 10" in session_text
    assert "    importance: 8" in session_text
    assert 'engine: "claude"' in session_text
    assert 'persona: "vulkan"' in session_text
    assert f"target_working_dir: {json.dumps(str(ROOT))}" in session_text
    assert 'dispatch_target: "legion:new"' in session_text
    assert "zealotry: 4" in session_text
    assert "victory_conditions:" in session_text
    assert '  - "Tests pass"' in session_text
    assert "## Dispatch Boundary" in session_text
    assert "no downstream agent has been launched" in session_text
    assert "aspirant_persona: aspirant" in note_text
    assert "dispatch_boundary: true" in note_text
    assert "dispatch_schema_complete: true" in note_text
    assert "dispatch_ready: false" in note_text
    assert 'dispatch_blocked_reason: "pending_aspirant_trials"' in note_text
    assert "trials_verdict: pending" in note_text
    assert "operator_approved_dispatch: false" in note_text
    assert "questions:" in note_text
    assert '  - question: "which other questions are needed for this aspirant?"' in note_text
    assert "    importance: 10" in note_text
    prompt_line = next(
        line for line in session_text.splitlines() if line.startswith("aspirant_persona_prompt: ")
    )
    prompt_path = Path(json.loads(prompt_line.split(": ", 1)[1]))
    assert prompt_path.is_absolute()
    assert prompt_path.exists()


def test_aspirant_create_dispatch_incomplete_stays_intake(tmp_path):
    env, vault = env_for(tmp_path)
    result = subprocess.run(
        [
            *ASPIRANT_CREATE_MODULE,
            "--json",
            "--kind",
            "dispatch",
            "--title",
            "Vague work",
            "--objective",
            "Do something later",
            "--engine",
            "claude",
            "--dir",
            str(ROOT),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["status"] == "aspirant_intake"
    assert data["dispatch_ready"] is False
    assert "persona" in data["dispatch_blocked_reason"]
    text = (vault / data["note_path"]).read_text(encoding="utf-8")
    assert "status: aspirant_intake" in text
    assert "dispatch_schema_complete: false" in text
    assert "dispatch_ready: false" in text
    assert 'dispatch_blocked_reason: "missing persona, dispatch_target, victory_conditions"' in text
    assert "trials_verdict: pending" in text
    assert "operator_approved_dispatch: false" in text
    assert "questions:" in text
    assert '  - question: "which other questions are needed for this aspirant?"' in text
    assert "    importance: 10" in text
    assert "aspirant_persona: aspirant" in text
    session_text = (vault / data["session_doc"]).read_text(encoding="utf-8")
    assert "status: aspirant_intake" in session_text
    assert "dispatch_schema_complete: false" in session_text
    assert "dispatch_ready: false" in session_text
    assert "operator_approved_dispatch: false" in session_text
    assert "questions:" in session_text
    assert '  - question: "which other questions are needed for this aspirant?"' in session_text
    assert "    importance: 10" in session_text
    assert "aspirant_persona_prompt: " in session_text


def test_aspirant_create_dispatch_defaults_dir_to_imperium_env_vault(tmp_path):
    env, vault = env_for(tmp_path)
    result = subprocess.run(
        [
            *ASPIRANT_CREATE_MODULE,
            "--json",
            "--kind",
            "dispatch",
            "--title",
            "Default dir",
            "--objective",
            "Use vault root by default",
            "--engine",
            "claude",
            "--persona",
            "vulkan",
            "--target",
            "legion:new",
            "--victory-condition",
            "Tests pass",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    session_text = (vault / data["session_doc"]).read_text(encoding="utf-8")
    note_text = (vault / data["note_path"]).read_text(encoding="utf-8")
    expected = f"target_working_dir: {json.dumps(str(vault))}"
    assert expected in session_text
    assert expected in note_text


def test_aspirant_create_rejects_empty_objective_without_staging(tmp_path):
    env, vault = env_for(tmp_path)
    result = subprocess.run(
        [
            *ASPIRANT_CREATE_MODULE,
            "--json",
            "--kind",
            "dispatch",
            "--title",
            "Empty objective",
            "--objective",
            "",
            "--engine",
            "claude",
            "--persona",
            "vulkan",
            "--target",
            "legion:new",
            "--victory-condition",
            "Tests pass",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode != 0
    assert "must not be empty" in result.stderr
    assert not list((vault / "Aspirants").glob("*.md"))


def test_aspirant_create_allows_trivial_non_empty_objective(tmp_path):
    env, vault = env_for(tmp_path)
    result = subprocess.run(
        [
            *ASPIRANT_CREATE_MODULE,
            "--json",
            "--kind",
            "dispatch",
            "--title",
            "Trivial objective",
            "--objective",
            "test",
            "--engine",
            "claude",
            "--persona",
            "vulkan",
            "--target",
            "legion:new",
            "--victory-condition",
            "Tests pass",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert (vault / data["note_path"]).exists()
    assert data["status"] == "aspirant_trials"


def test_dispatch_delegation_dry_run_uses_internal_creation_surface(tmp_path):
    env, _vault = env_for(tmp_path)
    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--aspirant",
            "--aspirant-kind",
            "deploy_d",
            "Remember this without launching",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "dispatch aspirant dry-run" in result.stdout
    assert "internal_action: create aspirant note/session" in result.stdout
    assert "aspirant-create" not in result.stdout


def test_dispatch_aspirant_dispatch_dry_run_delegates_without_launch(tmp_path):
    env, _vault = env_for(tmp_path)
    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--aspirant",
            "--aspirant-kind",
            "dispatch",
            "--engine",
            "codex",
            "--persona",
            "aspirant",
            "--dir",
            str(ROOT),
            "--target",
            "legion:new",
            "--victory-condition",
            "Boundary verified",
            "Prepare dispatch but do not launch",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "dispatch aspirant dry-run" in result.stdout
    assert "aspirant_kind:  dispatch" in result.stdout
    assert "internal_action: create aspirant note/session" in result.stdout
    assert "dispatch_ready:  false" in result.stdout
    assert "trials_verdict:  pending" in result.stdout
    assert "aspirant-create" not in result.stdout
