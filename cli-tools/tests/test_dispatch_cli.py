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
    assert "internal_action: create aspirant note/session" in result.stdout
    assert "aspirant-create" not in result.stdout
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


def test_no_public_aspirant_create_command_remains():
    assert not (ROOT / "cli-tools" / "bin" / "aspirant-create").exists()


def test_dispatch_aspirant_uses_internal_backend_without_public_command(tmp_path):
    vault = tmp_path / "Imperium-ENV"
    vault.mkdir()
    env = os.environ.copy()
    env["IMPERIUM"] = str(tmp_path)
    env["PATH"] = "/usr/bin:/bin"
    result = subprocess.run(
        [str(DISPATCH), "--aspirant", "--aspirant-kind", "deploy_p", "needs backend"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "created: True" in result.stdout
    assert "aspirant backend not installed" not in result.stderr


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


def test_human_shell_surfaces_call_dispatch_interactive_aspirants(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "dispatch.log"
    fake_dispatch = fake_bin / "dispatch"
    fake_dispatch.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s|%s\\n' \"$TOKEN_API_DISPATCH_ORIGIN\" \"$*\" >> \"$DISPATCH_LOG\"\n",
        encoding="utf-8",
    )
    fake_dispatch.chmod(0o755)

    script = f"""
      source {ROOT / "cli-tools" / "lib" / "shell-aliases.sh"}
      c
      cc "do more"
      cdc {ROOT} "do cdc"
    """
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            "DISPATCH_LOG": str(log),
            "CLI_TOOLS": str(ROOT / "cli-tools"),
            "TOKEN_OS": str(ROOT),
            "IMPERIUM": str(ROOT.parent),
            "TERM": "xterm",
        }
    )
    result = subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
    )

    assert result.returncode == 0, result.stderr
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "c|--interactive --aspirant --aspirant-kind dispatch"
    assert lines[1] == "cc|--aspirant --aspirant-kind dispatch do more"
    assert lines[2] == "cdc|--aspirant --aspirant-kind dispatch do cdc"


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


def test_dispatch_interactive_aspirant_collects_objective_from_override(monkeypatch):
    monkeypatch.setenv("DISPATCH_INTERACTIVE_DIR", str(ROOT))
    monkeypatch.setenv("DISPATCH_INTERACTIVE_SESSION_DOC", "__none__")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_PERSONA", "vulkan")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_MODE", "one_off")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_OBJECTIVE", "interactive work intake")
    result = subprocess.run(
        [str(DISPATCH), "--dry-run", "--interactive", "--aspirant"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0, result.stderr
    assert "dispatch aspirant dry-run" in result.stdout
    assert "objective:      interactive work intake" in result.stdout
    assert "persona:        vulkan" in result.stdout
    assert "zealotry:       3" in result.stdout


def test_dispatch_aspirant_dispatch_complete_metadata_enters_trials(tmp_path):
    vault = tmp_path / "Imperium-ENV"
    vault.mkdir()
    env = os.environ.copy()
    env["IMPERIUM"] = str(tmp_path)
    result = subprocess.run(
        [
            str(DISPATCH),
            "--aspirant",
            "--aspirant-kind",
            "dispatch",
            "--engine",
            "claude",
            "--persona",
            "vulkan",
            "--dir",
            str(ROOT),
            "--target",
            "legion:new",
            "--victory-condition",
            "Tests pass",
            "Implement safely",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "status: aspirant_trials" in result.stdout
    assert "dispatch_schema_complete: True" in result.stdout
    assert "dispatch_ready: False" in result.stdout
    assert "operator_approved_dispatch: False" in result.stdout

    note = next((vault / "Aspirants").glob("implement-safely*.md"))
    note_text = note.read_text(encoding="utf-8")
    assert "status: aspirant_trials" in note_text
    assert "dispatch_schema_complete: true" in note_text
    assert "dispatch_ready: false" in note_text
    assert "trials_verdict: pending" in note_text
    assert "operator_approved_dispatch: false" in note_text
    assert "open_questions: {}" in note_text
