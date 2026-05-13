import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "cli-tools" / "bin" / "dispatch"


def test_dispatch_claude_system_prompt_file_dry_run(tmp_path):
    prompt_file = tmp_path / "prompt.txt"
    system_file = tmp_path / "system.txt"
    prompt_file.write_text("initial prompt", encoding="utf-8")
    system_file.write_text("aspirant system", encoding="utf-8")

    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--target",
            "legion:new",
            "--dir",
            str(ROOT),
            "--system-prompt-file",
            str(system_file),
            "--prompt-file",
            str(prompt_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "system_prompt:   provided" in result.stdout
    assert "--append-system-prompt" in result.stdout
    assert "aspirant\\ system" in result.stdout
    assert "initial\\ prompt" in result.stdout


def test_dispatch_codex_rejects_system_prompt(tmp_path):
    system_file = tmp_path / "system.txt"
    system_file.write_text("unsupported", encoding="utf-8")

    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--engine",
            "codex",
            "--dir",
            str(ROOT),
            "--system-prompt-file",
            str(system_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 64
    assert "not supported for --engine codex" in result.stderr


def test_dispatch_aspirant_delegates_with_prompt_fallback_objective():
    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--aspirant",
            "--aspirant-kind",
            "deploy_p",
            "long goal / bug report text",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0
    assert "dispatch aspirant dry-run" in result.stdout
    assert "aspirant_kind:  deploy_p" in result.stdout
    assert "objective:      long goal / bug report text" in result.stdout
    assert "aspirant-create --kind deploy_p" in result.stdout
    assert "--dir" not in result.stdout


def test_dispatch_kind_alias_requires_aspirant():
    result = subprocess.run(
        [str(DISPATCH), "--dry-run", "--kind", "deploy_d", "note"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 64
    assert "--kind is only valid" in result.stderr


def test_dispatch_auto_policy_uses_origin_env(monkeypatch):
    monkeypatch.delenv("TOKEN_API_INTERNAL_DISPATCH", raising=False)
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "c")
    result = subprocess.run(
        [str(DISPATCH), "--dry-run", "make this a tracked worker goal"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0
    assert "dispatch aspirant dry-run" in result.stdout
    assert "aspirant_kind:  dispatch" in result.stdout


def test_dispatch_direct_bypasses_auto_policy(monkeypatch):
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "c")
    result = subprocess.run(
        [str(DISPATCH), "--dry-run", "--direct", "launch directly"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0
    assert "dispatch dry-run" in result.stdout
    assert "dispatch aspirant dry-run" not in result.stdout


def test_dispatch_missing_aspirant_backend_fails_clearly():
    env = os.environ.copy()
    env["PATH"] = "/usr/bin:/bin"
    result = subprocess.run(
        [str(DISPATCH), "--aspirant", "needs backend"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
    )

    assert result.returncode == 69
    assert "aspirant backend not installed; explicit --direct to bypass" in result.stderr


def test_dispatch_auto_policy_ignores_resume(monkeypatch):
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "c")
    result = subprocess.run(
        [str(DISPATCH), "--dry-run", "--id", "missing-session", "--engine", "claude", "--dir", str(ROOT)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0
    assert "dispatch dry-run" in result.stdout
    assert "dispatch aspirant dry-run" not in result.stdout


def test_dispatch_auto_policy_ignores_internal_dispatch(monkeypatch):
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "c")
    monkeypatch.setenv("TOKEN_API_INTERNAL_DISPATCH", "1")
    result = subprocess.run(
        [str(DISPATCH), "--dry-run", "internal launch"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0
    assert "dispatch dry-run" in result.stdout
    assert "dispatch aspirant dry-run" not in result.stdout


def test_dispatch_aspirant_rejects_inline_system_prompt():
    result = subprocess.run(
        [str(DISPATCH), "--dry-run", "--aspirant", "--system-prompt", "inline", "objective"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 64
    assert "--system-prompt text is not supported with --aspirant" in result.stderr
