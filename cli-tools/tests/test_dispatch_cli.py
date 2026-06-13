import json
import os
import subprocess
from pathlib import Path

import pytest

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


def test_dispatch_unicode_prompt_no_illegal_byte_sequence_under_c_locale(tmp_path):
    """Regression: a unicode prompt must dry-run cleanly under a C/POSIX locale.

    dispatch pipes prompt/title text through sed/tr/cut (title + slugify) and the
    %pane redactor runs over bash `printf %q` output. Under LC_ALL=C — the default
    in cron/launchd and many shells — BSD tooling used to abort with
    `sed: RE error: illegal byte sequence`, forcing the user to export
    LC_ALL=en_US.UTF-8 by hand. The script now hardens its own locale; no env var
    should ever be required.
    """
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("# Sand the flow → mirror coat\n\nMake it ✓ now\n", encoding="utf-8")

    env = os.environ.copy()
    # Simulate the hostile default the user hits; the script must self-correct.
    env["LC_ALL"] = "C"
    env.pop("LC_CTYPE", None)
    env.pop("LANG", None)

    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--direct",
            "--dir",
            str(ROOT),
            "--prompt-file",
            str(prompt_file),
        ],
        capture_output=True,
        text=True,
        # Prompt bytes may surface in the printed command; never let decoding the
        # harness output mask the real assertion below.
        errors="replace",
        check=False,
        cwd=str(ROOT),
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "illegal byte sequence" not in result.stderr
    assert "illegal byte sequence" not in result.stdout
    assert "RE error" not in result.stderr


def test_dispatch_claude_model_dry_run_forwards_to_wrapper():
    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--direct",
            "--dir",
            str(ROOT),
            "--model",
            "sonnet",
            "stand by",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0, result.stderr
    assert "claude_model:    sonnet" in result.stdout
    assert "--model sonnet" in result.stdout


def test_dispatch_legion_shorthand_maps_to_target(tmp_path):
    for keyword in ("legion", "mechanicus", "civic"):
        result = subprocess.run(
            [str(DISPATCH), keyword, "--dry-run", "--direct", "--dir", str(ROOT), "work"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(ROOT),
        )
        assert result.returncode == 0, result.stderr
        assert f"target:          {keyword}:new" in result.stdout


def test_dispatch_legion_shorthand_only_consumes_leading_token():
    # A prompt that merely mentions "legion" must not be hijacked as a target.
    result = subprocess.run(
        [str(DISPATCH), "--dry-run", "--direct", "--dir", str(ROOT), "deploy the legion now"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "target:          current" in result.stdout
    assert "legion:new" not in result.stdout


def test_dispatch_explicit_target_overrides_shorthand():
    result = subprocess.run(
        [
            str(DISPATCH),
            "legion",
            "--dry-run",
            "--direct",
            "--dir",
            str(ROOT),
            "--target",
            "mechanicus:new",
            "work",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "target:          mechanicus:new" in result.stdout


def test_dispatch_worktree_derives_title_and_short_objective(tmp_path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(
        "# Sand the dispatch flow → mirror coat\n\nbody line ✓\n", encoding="utf-8"
    )
    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--aspirant",
            "--worktree",
            "test-mirror-coat",
            "--prompt-file",
            str(prompt_file),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    # Title from the branch name, objective from the first line (heading stripped).
    assert "title:          test mirror coat" in result.stdout
    assert "objective:      Sand the dispatch flow → mirror coat" in result.stdout


def test_dispatch_worktree_metadata_does_not_override_explicit(tmp_path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("# heading\n\nbody\n", encoding="utf-8")
    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--aspirant",
            "--worktree",
            "test-mirror-coat",
            "--title",
            "My Title",
            "--objective",
            "My Objective",
            "--prompt-file",
            str(prompt_file),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "title:          My Title" in result.stdout
    assert "objective:      My Objective" in result.stdout


def test_dispatch_default_one_off_but_sync_keeps_gt_zealotry() -> None:
    default = subprocess.run(
        [str(DISPATCH), "--dry-run", "--direct", "--dir", str(ROOT), "default work"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )
    assert default.returncode == 0, default.stderr
    assert "instance_type:   one_off" in default.stdout
    assert "zealotry:        3" in default.stdout

    sync = subprocess.run(
        [str(DISPATCH), "--dry-run", "--direct", "--sync", "--dir", str(ROOT), "sync work"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )
    assert sync.returncode == 0, sync.stderr
    assert "instance_type:   sync" in sync.stdout
    assert "zealotry:        5" in sync.stdout


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


def test_dispatch_human_origin_no_longer_auto_aspirant(monkeypatch) -> None:
    monkeypatch.delenv("TOKEN_API_INTERNAL_DISPATCH", raising=False)
    monkeypatch.delenv("TOKEN_API_DISPATCH_AUTO_ASPIRANT", raising=False)
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "d")
    result = subprocess.run(
        [str(DISPATCH), "--dry-run", "make this a tracked worker goal"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0
    assert "dispatch dry-run" in result.stdout
    assert "dispatch aspirant dry-run" not in result.stdout
    assert "instance_type:   one_off" in result.stdout


def test_dispatch_auto_aspirant_can_be_enabled_by_env(monkeypatch) -> None:
    monkeypatch.delenv("TOKEN_API_INTERNAL_DISPATCH", raising=False)
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "d")
    monkeypatch.setenv("TOKEN_API_DISPATCH_AUTO_ASPIRANT", "1")
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
        CREATE TABLE personas (id TEXT PRIMARY KEY, slug TEXT);
        CREATE TABLE instances (
          id TEXT PRIMARY KEY, name TEXT, engine TEXT, launcher TEXT, target_working_dir TEXT,
          working_dir TEXT, dispatch_session_doc_path TEXT, session_doc_id INTEGER,
          golden_throne TEXT, zealotry TEXT, dispatch_target TEXT, dispatch_window TEXT,
          dispatch_mode TEXT, dispatch_slot TEXT, launch_mode TEXT, tmux_pane TEXT,
          persona_id TEXT, commander_type TEXT, commander_id TEXT, discord_hosted TEXT,
          discord_channel TEXT, discord_bot TEXT, pane_label TEXT,
          last_activity TEXT
        );
        INSERT INTO instances (
          id, name, engine, working_dir, golden_throne, zealotry, last_activity, commander_type
        ) VALUES ('resume-session-id', 'Readable Name', 'claude', '{escaped_root}', '1', '5', '2026-05-15', 'emperor');
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


def test_human_shell_surfaces_call_dispatch_interactive_direct_by_default(tmp_path) -> None:
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
      d --target legion:new "new pane"
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
            "TMUX_PANE": "%42",
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
    assert lines[0] == "d|--interactive --pane self do more"
    assert lines[1] == f"cdc|--interactive --pane self --dir {ROOT} do cdc"
    assert lines[2] == "d|--interactive --pane self --direct direct work"
    assert lines[3] == f"cdc|--interactive --pane self --dir {ROOT} --direct direct cdc"
    assert lines[4] == "d|--interactive --pane self --resume resume-session-id"
    assert lines[5] == "d|--interactive --target legion:new new pane"


def test_dispatch_human_origin_defaults_to_self_pane_in_tmux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "d")
    monkeypatch.setenv("TOKEN_API_DISPATCH_MENU_CONSUMED", "1")
    monkeypatch.setenv("TMUX_PANE", "%42")
    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--direct",
            "--dir",
            str(ROOT),
            "launch in place",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0, result.stderr
    assert "target:          self" in result.stdout
    assert "TOKEN_API_DISPATCH_TARGET=self" in result.stdout
    assert "TOKEN_API_DISPATCH_RESOLVED_PANE=<tmux-pane>" in result.stdout
    assert "TMUX_PANE=<tmux-pane>" in result.stdout
    assert "--target self" in result.stdout


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
    assert "dispatch_codex_launch_inline" in result.stdout
    assert "codex-dispatch" not in result.stdout


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
    assert "instance_type:   one_off" in result.stdout


def test_dispatch_persona_engine_bindings_and_generic_engine_choice() -> None:
    custodes = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--direct",
            "--no-worktree",
            "--engine",
            "codex",
            "--persona",
            "custodes",
            "--dir",
            str(ROOT),
            "custodes work",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )
    assert custodes.returncode == 0, custodes.stderr
    assert "engine:          claude" in custodes.stdout
    assert "primarch custodes" not in custodes.stdout
    assert "claude-wrapper.sh" in custodes.stdout
    assert "TOKEN_API_PRIMARCH=custodes" in custodes.stdout

    inquisitor = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--direct",
            "--no-worktree",
            "--engine",
            "claude",
            "--persona",
            "inquisitor",
            "--dir",
            str(ROOT),
            "inquisitor work",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )
    assert inquisitor.returncode == 0, inquisitor.stderr
    assert "engine:          codex" in inquisitor.stdout
    assert "TOKEN_API_CODEX_PROFILE=inquisitor" in inquisitor.stdout
    assert "TOKEN_API_PRIMARCH=inquisitor" in inquisitor.stdout
    assert "dispatch_codex_launch_inline" in inquisitor.stdout

    vulkan = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--direct",
            "--no-worktree",
            "--engine",
            "codex",
            "--persona",
            "vulkan",
            "--dir",
            str(ROOT),
            "vulkan work",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )
    assert vulkan.returncode == 0, vulkan.stderr
    assert "engine:          codex" in vulkan.stdout
    assert "TOKEN_API_PRIMARCH=vulkan" in vulkan.stdout
    assert "primarch vulkan" not in vulkan.stdout


def test_dispatch_rejects_deprecated_primarch_flag() -> None:
    result = subprocess.run(
        [str(DISPATCH), "--dry-run", "--primarch", "vulkan", "work"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 64
    assert "--primarch is deprecated; use --persona" in result.stderr


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
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" > "$TMUXCTL_LOG"\nprintf "%%83\\n"\n',
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)
    tmux_log = tmp_path / "tmux.log"
    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$TMUX_LOG"\n',
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)
    env = os.environ.copy()
    env["IMPERIUM"] = str(tmp_path)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TMUXCTL_LOG"] = str(tmuxctl_log)
    env["TMUX_LOG"] = str(tmux_log)
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
    assert "%83" not in result.stdout

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
    # tmuxctl now spawns the pane with a throwaway `clear` warmup that absorbs
    # any leading-char loss from the upstream type-check guard; the real
    # `bash <staged>` is sent via a follow-up tmux send-keys after a settle.
    assert "--command clear" in launched
    assert "%83" not in launched
    tmux_text = tmux_log.read_text(encoding="utf-8", errors="replace")
    assert "send-keys -t %83 bash " in tmux_text
    assert "Enter" in tmux_text
    send_line = next(line for line in tmux_text.splitlines() if "send-keys -t %83 bash " in line)
    staged_path = Path(send_line.rsplit("bash ", 1)[1].rsplit(" Enter", 1)[0].strip())
    staged = staged_path.read_text(encoding="utf-8", errors="replace")
    assert "--append-system-prompt" in staged
    assert "Aspirant Session Startup" in staged
    assert "## Implantation" in staged
    assert "## Trials" in staged


def test_dispatch_human_aspirant_launch_defaults_to_self_pane(tmp_path) -> None:
    vault = tmp_path / "Imperium-ENV"
    vault.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    codex_log = tmp_path / "codex.log"
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" > "$CODEX_LOG"\nexit 0\n',
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)

    env = os.environ.copy()
    env["IMPERIUM"] = str(tmp_path)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_DISPATCH_ORIGIN"] = "d"
    env["TOKEN_API_DISPATCH_MENU_CONSUMED"] = "1"
    env["TMUX_PANE"] = "%55"
    env["CODEX_LOG"] = str(codex_log)
    env["CODEX_BIN"] = str(fake_codex)

    result = subprocess.run(
        [
            str(DISPATCH),
            "--aspirant",
            "--aspirant-kind",
            "dispatch",
            "--engine",
            "codex",
            "--dir",
            str(ROOT),
            "--victory-condition",
            "Tests pass",
            "Launch in this pane",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "launch_action: dispatch --direct --engine codex" in result.stdout
    assert "--target self" in result.stdout
    assert "--target legion:new" not in result.stdout
    assert codex_log.exists()


def test_dispatch_codex_aspirant_launch_respects_engine_without_claude_system_prompt(
    tmp_path,
) -> None:
    vault = tmp_path / "Imperium-ENV"
    vault.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmuxctl_log = tmp_path / "tmuxctl.log"
    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" > "$TMUXCTL_LOG"\nprintf "%%84\\n"\n',
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)
    tmux_log = tmp_path / "tmux.log"
    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$TMUX_LOG"\n',
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)
    env = os.environ.copy()
    env["IMPERIUM"] = str(tmp_path)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TMUXCTL_LOG"] = str(tmuxctl_log)
    env["TMUX_LOG"] = str(tmux_log)

    result = subprocess.run(
        [
            str(DISPATCH),
            "--aspirant",
            "--aspirant-kind",
            "dispatch",
            "--engine",
            "codex",
            "--persona",
            "vulkan",
            "--dir",
            str(ROOT),
            "--target",
            "legion:new",
            "--victory-condition",
            "Tests pass",
            "Implement with codex",
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
    assert "launch_action: dispatch --direct --engine codex" in result.stdout
    assert "--no-gt" in result.stdout
    assert "--system-prompt-file" not in result.stdout
    assert "dispatched codex to legion:new" in result.stdout

    tmux_text = tmux_log.read_text(encoding="utf-8", errors="replace")
    send_line = next(line for line in tmux_text.splitlines() if "send-keys -t %84 bash " in line)
    staged_path = Path(send_line.rsplit("bash ", 1)[1].rsplit(" Enter", 1)[0].strip())
    staged = staged_path.read_text(encoding="utf-8", errors="replace")
    assert "dispatch_codex_launch_inline" in staged
    assert "--append-system-prompt" not in staged
    assert "Aspirant Session Startup" in staged


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


def test_tmux_prefix_space_launcher_uses_large_popup_without_enter_newline_hang():
    conf = (ROOT / "cli-tools" / "tmux" / "tmux-base.conf").read_text(encoding="utf-8")
    bind_line = "bind Space" + conf.split("bind Space", 1)[1].split("\n", 1)[0]
    assert "display-popup" in bind_line
    assert "tmux-legion-prompt-popup" in bind_line
    assert "command-prompt" not in bind_line

    popup = (ROOT / "cli-tools" / "bin" / "tmux-legion-prompt-popup").read_text(encoding="utf-8")
    assert "IFS= read -e -r -p" in popup
    assert "TOKEN_API_DISPATCH_ORIGIN=d" in popup
    assert "--direct" in popup
    assert "--target legion:new" in popup
    assert "trap soft_cancel INT TERM" in popup
    assert "tmux-legion-prompt-popup.log" in popup
    assert "tmux run-shell" not in popup
    assert "stty -echo -icanon" not in popup

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


def test_dispatch_emits_token_api_legion_for_custodes_persona():
    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--direct",
            "--persona",
            "custodes",
            "--target",
            "legion:new",
            "--dir",
            str(ROOT),
            "custodes work",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0, result.stderr
    assert "TOKEN_API_LEGION=custodes" in result.stdout


def test_dispatch_emits_token_api_legion_for_custodes_slot_target():
    # State-hook dispatcher targets the legion:custodes slot without a persona.
    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--direct",
            "--target",
            "legion:custodes",
            "--dir",
            str(ROOT),
            "custodes work",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0, result.stderr
    assert "TOKEN_API_LEGION=custodes" in result.stdout


def test_dispatch_omits_legion_for_non_custodes_target():
    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--direct",
            "--target",
            "mechanicus:new",
            "--dir",
            str(ROOT),
            "work",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0, result.stderr
    assert "TOKEN_API_LEGION=\n" in result.stdout or "TOKEN_API_LEGION=" in result.stdout
    assert "TOKEN_API_LEGION=custodes" not in result.stdout


def test_dispatch_target_dry_run_resolves_public_without_physical(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == "resolve-pane --format id 2:NE" ]]; then echo somnium:NE; exit 0; fi\n'
        'if [[ "$*" == "resolve-pane --format physical 2:NE" ]]; then echo %22; exit 0; fi\n'
        "exit 1\n",
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    result = subprocess.run(
        [str(DISPATCH), "--dry-run", "--direct", "--dir", str(ROOT), "--target", "2:NE", "prompt"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "target:          2:NE" in result.stdout
    assert "resolved_target: somnium:NE" in result.stdout
    assert "%22" not in result.stdout


def test_dispatch_stack_new_bakes_concrete_pane_into_launch_env(tmp_path):
    """A :new stack launch must register the concrete pane the agent lands in.

    Regression guard for the pane-registry wedge: dispatch builds FINAL_COMMAND
    before the pane exists, baking the allocation token (mechanicus:new) into
    TMUX_PANE / TOKEN_API_DISPATCH_RESOLVED_PANE. After the pane is allocated the
    env must be rebuilt with the concrete %NN id so the agent's first
    SessionStart registers a tmux_pane that pane_truth / assert-instance /
    agent-cmd can resolve. TOKEN_API_DISPATCH_TARGET keeps the request token.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    rec = tmp_path / "tmux_calls.txt"

    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "stack" && "$2" == "dispatch" ]]; then echo "%77"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)

    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        f'#!/usr/bin/env bash\nfor a in "$@"; do printf "%s\\0" "$a" >> {rec}; done\nexit 0\n',
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    # Skip the caller-instance resolution network hop so the test is hermetic.
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"

    result = subprocess.run(
        [
            str(DISPATCH),
            "--target",
            "mechanicus:new",
            "--dir",
            str(ROOT),
            "--no-worktree",
            "--no-gt",
            "--prompt",
            "noop",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
    )

    assert result.returncode == 0, result.stderr

    calls = [c for c in rec.read_bytes().decode("utf-8", "replace").split("\0") if c]
    # The staged launcher is sent via `tmux send-keys -t %77 'bash /tmp/...' Enter`.
    staged_arg = next((c for c in calls if c.startswith("bash /")), None)
    assert staged_arg is not None, f"no staged send-keys recorded; calls={calls}"
    staged_path = Path(staged_arg.split(" ", 1)[1])
    content = staged_path.read_text(encoding="utf-8")

    assert "TMUX_PANE=%77" in content, content
    assert "TOKEN_API_DISPATCH_RESOLVED_PANE=%77" in content, content
    assert "TMUX_PANE=mechanicus:new" not in content, content
    assert "TOKEN_API_DISPATCH_RESOLVED_PANE=mechanicus:new" not in content, content
    # The allocation token remains the semantic request target.
    assert "TOKEN_API_DISPATCH_TARGET=mechanicus:new" in content, content
