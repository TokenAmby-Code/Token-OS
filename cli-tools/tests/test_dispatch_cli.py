import json
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
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "d")
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
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "d")
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
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "d")
    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--id",
            "missing-session",
            "--engine",
            "claude",
            "--dir",
            str(ROOT),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0
    assert "dispatch dry-run" in result.stdout
    assert "dispatch aspirant dry-run" not in result.stdout


def test_dispatch_resume_aliases_are_open(monkeypatch):
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "d")
    for flag in ("--resume", "-r"):
        result = subprocess.run(
            [
                str(DISPATCH),
                "--dry-run",
                flag,
                "missing-session",
                "--engine",
                "claude",
                "--dir",
                str(ROOT),
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(ROOT),
        )
        assert result.returncode == 0, result.stderr
        assert "resume_id:       missing-session" in result.stdout
        assert "dispatch aspirant dry-run" not in result.stdout


def test_dispatch_interactive_session_doc_resume_option(tmp_path, monkeypatch):
    db = tmp_path / "agents.db"
    import sqlite3

    escaped_root = str(ROOT).replace("'", "''")
    conn = sqlite3.connect(db)
    conn.executescript(
        f"""
        CREATE TABLE session_documents (id INTEGER, file_path TEXT);
        CREATE TABLE claude_instances (
          session_id TEXT, engine TEXT, launcher TEXT, target_working_dir TEXT,
          working_dir TEXT, dispatch_session_doc_path TEXT, session_doc_id INTEGER,
          instance_type TEXT, zealotry TEXT, dispatch_target TEXT, dispatch_window TEXT,
          dispatch_mode TEXT, dispatch_slot TEXT, launch_mode TEXT, tmux_pane TEXT,
          primarch TEXT, parent_instance_id TEXT, discord_hosted TEXT,
          discord_channel TEXT, discord_bot TEXT, tab_name TEXT, pane_label TEXT,
          last_activity TEXT
        );
        INSERT INTO claude_instances (
          session_id, engine, working_dir, instance_type, zealotry, tab_name, last_activity
        ) VALUES ('resume-session-id', 'claude', '{escaped_root}', 'golden_throne', '5', 'Readable Name', '2026-05-15');
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("TOKEN_API_DB", str(db))
    monkeypatch.setenv("DISPATCH_INTERACTIVE_SESSION_DOC", "__resume__")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_RESUME", "resume-session-id")
    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--interactive",
            "--direct",
            "--engine",
            "claude",
            "--dir",
            str(ROOT),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0, result.stderr
    assert "resume_id:       resume-session-id" in result.stdout
    assert "resume_db:       true" in result.stdout


def test_dispatch_auto_policy_ignores_internal_dispatch(monkeypatch):
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "d")
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
        'printf \'%s|%s\\n\' "$TOKEN_API_DISPATCH_ORIGIN" "$*" >> "$DISPATCH_LOG"\n',
        encoding="utf-8",
    )
    fake_dispatch.chmod(0o755)

    script = f"""
      source {ROOT / "cli-tools" / "lib" / "shell-aliases.sh"}
      c
      d "do more"
      cdc {ROOT} "do cdc"
      d --direct "direct work"
      cdc {ROOT} --direct "direct cdc"
      d --resume resume-session-id
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
    assert lines[0] == "d|--aspirant --aspirant-kind dispatch --interactive do more"
    assert lines[1] == f"cdc|--aspirant --aspirant-kind dispatch --interactive --dir {ROOT} do cdc"
    assert lines[2] == "d|--interactive --direct direct work"
    assert lines[3] == f"cdc|--interactive --dir {ROOT} --direct direct cdc"
    assert lines[4] == "d|--interactive --resume resume-session-id"


def test_dispatch_human_origin_forces_interactive_even_with_direct(monkeypatch):
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "d")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_DIR", str(ROOT))
    monkeypatch.setenv("DISPATCH_INTERACTIVE_SESSION_DOC", "__none__")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_PERSONA", "__sisters_of_battle__")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_MODE", "one_off")
    result = subprocess.run(
        [str(DISPATCH), "--dry-run", "--direct", "launch directly"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0, result.stderr
    assert "dispatch dry-run" in result.stdout
    assert "dispatch aspirant dry-run" not in result.stdout
    assert "engine:          codex" in result.stdout
    assert "instance_type:   one_off" in result.stdout


def test_dispatch_menu_consumed_prevents_second_interactive_menu(monkeypatch):
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "d")
    monkeypatch.setenv("TOKEN_API_DISPATCH_MENU_CONSUMED", "1")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_DIR", str(ROOT))
    monkeypatch.setenv("DISPATCH_INTERACTIVE_SESSION_DOC", "__none__")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_PERSONA", "__sisters_of_battle__")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_MODE", "one_off")
    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--direct",
            "--engine",
            "claude",
            "--dir",
            str(ROOT),
            "launch directly",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0, result.stderr
    assert "dispatch dry-run" in result.stdout
    assert "engine:          claude" in result.stdout
    assert "instance_type:   golden_throne" in result.stdout


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
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmuxctl_log = tmp_path / "tmuxctl.log"
    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" > "$TMUXCTL_LOG"\nprintf "%%aspirant-pane\\n"\n',
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)
    env = os.environ.copy()
    env["IMPERIUM"] = str(tmp_path)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TMUXCTL_LOG"] = str(tmuxctl_log)
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
    data = json.loads(result.stdout.splitlines()[0])
    assert data["status"] == "aspirant_trials"
    assert data["dispatch_schema_complete"] is True
    assert data["dispatch_ready"] is False
    assert data["operator_approved_dispatch"] is False
    assert "dispatched claude to legion:new" in result.stdout
    assert "%aspirant-pane" not in result.stdout

    note = next((vault / "Aspirants").glob("implement-safely*.md"))
    note_text = note.read_text(encoding="utf-8")
    assert "status: aspirant_trials" in note_text
    assert "dispatch_schema_complete: true" in note_text
    assert "dispatch_ready: false" in note_text
    assert "trials_verdict: pending" in note_text
    assert "operator_approved_dispatch: false" in note_text
    assert "questions:" in note_text
    assert "which other questions are needed for this aspirant?" in note_text
    assert "importance: 10" in note_text
    assert "launch_action: dispatch --direct --engine claude" in result.stdout
    assert "--target legion:new" in result.stdout
    assert "--session-doc" in result.stdout
    assert "--system-prompt-file" in result.stdout
    assert "--prompt-file" in result.stdout
    launched = tmuxctl_log.read_text(encoding="utf-8", errors="replace")
    assert "stack dispatch legion --session main" in launched
    assert "--command bash " in launched
    assert "%aspirant-pane" not in launched
    staged_path = Path(launched.rsplit("--command bash ", 1)[1].strip())
    staged = staged_path.read_text(encoding="utf-8", errors="replace")
    assert "--append-system-prompt" in staged
    assert "Aspirant Session Startup" in staged
    assert "## Implantation" in staged
    assert "## Trials" in staged


def test_dispatch_aspirant_dispatch_intake_only_preserves_note_only_behavior(tmp_path):
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
            "--intake-only",
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
    assert "dispatched claude" not in result.stdout


def test_tmux_prefix_space_launcher_routes_to_d_without_popup_newline():
    conf = (ROOT / "cli-tools" / "tmux" / "tmux-base.conf").read_text(encoding="utf-8")
    assert "bind Space command-prompt" in conf
    assert "display-popup" not in conf.split("bind Space", 1)[1].split("\n", 1)[0]
    assert "tmux-legion-prompt --prompt" in conf

    launcher = (ROOT / "cli-tools" / "bin" / "tmux-legion-prompt").read_text(encoding="utf-8")
    assert "TOKEN_API_DISPATCH_ORIGIN=d" in launcher
    assert "${SCRIPT_DIR}/dispatch" in launcher
    assert 'LAUNCH_CMD="cd ~ && d ' not in launcher
    assert "c --prompt-file" not in launcher


def test_fzf_launcher_expands_and_cli_prompt_retracts_idempotently():
    dispatch = DISPATCH.read_text(encoding="utf-8")
    assert 'tmux-grid-expand --pane "$pane" --expand' in dispatch
    assert 'tmux-grid-expand --pane "$DISPATCH_AUTO_EXPANDED_PANE" --retract' in dispatch
    assert '[[ -n "$PROMPT" ]] || return 0' in dispatch

    expand = (ROOT / "cli-tools" / "bin" / "tmux-grid-expand").read_text(encoding="utf-8")
    assert "--expand" in expand
    assert 'if [[ "$EXPAND" == true ]]' in expand
    assert 'if [[ "$WINDOW_ZOOMED" != "1" ]]' in expand
