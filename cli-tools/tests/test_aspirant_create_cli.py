import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASPIRANT_CREATE = ROOT / "cli-tools" / "bin" / "aspirant-create"
DISPATCH = ROOT / "cli-tools" / "bin" / "dispatch"


def env_for(tmp_path):
    vault = tmp_path / "Imperium-ENV"
    vault.mkdir()
    env = os.environ.copy()
    env["IMPERIUM"] = str(tmp_path)
    env["PATH"] = f"{ROOT / 'cli-tools' / 'bin'}:{env.get('PATH', '')}"
    return env, vault


def test_aspirant_create_deploy_p_creates_prescriptive_note(tmp_path):
    env, vault = env_for(tmp_path)
    result = subprocess.run(
        [
            str(ASPIRANT_CREATE),
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
            str(ASPIRANT_CREATE),
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


def test_aspirant_create_dispatch_creates_mars_session_doc_when_ready(tmp_path):
    env, vault = env_for(tmp_path)
    result = subprocess.run(
        [
            str(ASPIRANT_CREATE),
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
    assert data["status"] == "dispatch_ready"
    assert data["dispatch_ready"] is True
    assert data["session_doc"].startswith("Mars/Sessions/")
    session_doc = vault / data["session_doc"]
    session_text = session_doc.read_text(encoding="utf-8")
    assert "status: dispatch_ready" in session_text
    assert "persona: \"vulkan\"" in session_text
    assert "dispatch_target: \"legion:new\"" in session_text


def test_aspirant_create_dispatch_incomplete_stays_intake(tmp_path):
    env, vault = env_for(tmp_path)
    result = subprocess.run(
        [
            str(ASPIRANT_CREATE),
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
    assert "dispatch_ready: false" in text


def test_dispatch_delegation_dry_run_points_at_aspirant_create(tmp_path):
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
    assert "aspirant-create --kind deploy_d" in result.stdout
