import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "cli-tools" / "bin" / "dispatch"


def _staged_command_from_tmuxctld_log(log: Path) -> str:
    text = log.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if " /send-text " in line and " text=bash " in line and " submit=true" in line:
            return line.split(" text=", 1)[1].split(" submit=true", 1)[0]
    raise AssertionError(f"no staged tmuxctld /send-text call recorded; log={text!r}")


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
            "mechanicus:new",
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
    for keyword in ("legion", "mechanicus", "civic", "palace", "somnium"):
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
    assert "mechanicus:new" not in result.stdout


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


def test_dispatch_auto_policy_ignores_resume(monkeypatch) -> None:
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "d")
    server = _run_instance_api_server({})
    monkeypatch.setenv("TOKEN_API_URL", f"http://127.0.0.1:{server.server_port}")
    try:
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
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 0
    assert "dispatch dry-run" in result.stdout
    assert "dispatch aspirant dry-run" not in result.stdout


def test_dispatch_resume_aliases_are_open(monkeypatch) -> None:
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "d")
    for flag in ("--resume", "-r"):
        server = _run_instance_api_server({})
        monkeypatch.setenv("TOKEN_API_URL", f"http://127.0.0.1:{server.server_port}")
        try:
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
        finally:
            server.shutdown()
            server.server_close()
        assert result.returncode == 0, result.stderr
        assert "resume_id:       missing-session" in result.stdout
        assert "dispatch aspirant dry-run" not in result.stdout


def test_dispatch_interactive_session_doc_resume_option(tmp_path, monkeypatch) -> None:
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

    server = _run_instance_api_server(
        {
            "resume-session-id": {
                "id": "resume-session-id",
                "engine": "claude",
                "working_dir": str(ROOT),
                "golden_throne": "1",
                "zealotry": 5,
            }
        }
    )
    monkeypatch.setenv("TOKEN_API_DB", str(db))
    monkeypatch.setenv("TOKEN_API_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_SESSION_DOC", "__resume__")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_RESUME", "resume-session-id")
    try:
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
    finally:
        server.shutdown()
        server.server_close()

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
      d --target mechanicus:new "new pane"
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
    assert lines[5] == "d|--interactive --target mechanicus:new new pane"


def test_shell_aliases_do_not_define_agent_front_doors(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("claude", "codex"):
        path = fake_bin / name
        path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)

    script = """
      set -euo pipefail
      claude() { echo stale; }
      codex() { echo stale; }
      alias claude='echo stale-alias'
      alias codex='echo stale-alias'
      source "$SHELL_ALIASES"
      ! declare -F claude >/dev/null
      ! declare -F codex >/dev/null
      ! alias claude >/dev/null 2>&1
      ! alias codex >/dev/null 2>&1
      [[ "$(type -P claude)" == "$FAKE_CLAUDE" ]]
      [[ "$(type -P codex)" == "$FAKE_CODEX" ]]
    """
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            "SHELL_ALIASES": str(ROOT / "cli-tools" / "lib" / "shell-aliases.sh"),
            "FAKE_CLAUDE": str(fake_bin / "claude"),
            "FAKE_CODEX": str(fake_bin / "codex"),
            "CLI_TOOLS": str(ROOT / "cli-tools"),
            "TOKEN_OS": str(ROOT),
            "IMPERIUM": str(ROOT.parent),
            "TERM": "xterm",
        }
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
    )

    assert result.returncode == 0, result.stderr


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


def test_dispatch_human_origin_uses_codex_harness_state_for_astartes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_file = tmp_path / "dispatch-harness"
    state_file.write_text("codex\n", encoding="utf-8")
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "d")
    monkeypatch.setenv("DISPATCH_HARNESS_STATE_FILE", str(state_file))
    monkeypatch.setenv("DISPATCH_INTERACTIVE_DIR", str(ROOT))
    monkeypatch.setenv("DISPATCH_INTERACTIVE_SESSION_DOC", "__none__")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_PERSONA", "__astartes__")
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
    assert "persona:         Astartes — Codex" in result.stdout
    assert "instance_type:   one_off" in result.stdout
    assert "agent-wrapper.sh codex" in result.stdout
    assert "dispatch_codex_launch_inline" not in result.stdout
    assert "TOKEN_API_PERSONA=codex-dispatch" not in result.stdout
    assert "sisters-of-battle" not in result.stdout
    assert "Sisters" not in result.stdout


def test_dispatch_human_origin_missing_or_invalid_harness_state_defaults_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "d")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_DIR", str(ROOT))
    monkeypatch.setenv("DISPATCH_INTERACTIVE_SESSION_DOC", "__none__")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_PERSONA", "__astartes__")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_MODE", "one_off")

    for contents in (None, "garbage\n"):
        state_file = tmp_path / f"dispatch-harness-{contents is not None}"
        if contents is not None:
            state_file.write_text(contents, encoding="utf-8")
        monkeypatch.setenv("DISPATCH_HARNESS_STATE_FILE", str(state_file))
        result = subprocess.run(
            [str(DISPATCH), "--dry-run", "--direct", "launch directly"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(ROOT),
        )

        assert result.returncode == 0, result.stderr
        assert "engine:          claude" in result.stdout
        assert "persona:         Astartes — Claude" in result.stdout
        assert "Sisters" not in result.stdout


def test_dispatch_noninteractive_ignores_harness_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_file = tmp_path / "dispatch-harness"
    state_file.write_text("codex\n", encoding="utf-8")
    monkeypatch.setenv("DISPATCH_HARNESS_STATE_FILE", str(state_file))

    result = subprocess.run(
        [str(DISPATCH), "--dry-run", "--direct", "--dir", str(ROOT), "launch directly"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0, result.stderr
    assert "engine:          claude" in result.stdout
    assert "persona:         Astartes — Claude" in result.stdout


def test_dispatch_internal_interactive_ignores_harness_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_file = tmp_path / "dispatch-harness"
    state_file.write_text("codex\n", encoding="utf-8")
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "d")
    monkeypatch.setenv("TOKEN_API_INTERNAL_DISPATCH", "1")
    monkeypatch.setenv("DISPATCH_HARNESS_STATE_FILE", str(state_file))
    monkeypatch.setenv("DISPATCH_INTERACTIVE_DIR", str(ROOT))
    monkeypatch.setenv("DISPATCH_INTERACTIVE_SESSION_DOC", "__none__")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_PERSONA", "__astartes__")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_MODE", "one_off")

    result = subprocess.run(
        [str(DISPATCH), "--dry-run", "--interactive", "--direct", "launch directly"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0, result.stderr
    assert "engine:          claude" in result.stdout
    assert "persona:         Astartes — Claude" in result.stdout


def test_dispatch_menu_consumed_prevents_second_interactive_menu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_file = tmp_path / "dispatch-harness"
    state_file.write_text("codex\n", encoding="utf-8")
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "d")
    monkeypatch.setenv("TOKEN_API_DISPATCH_MENU_CONSUMED", "1")
    monkeypatch.setenv("DISPATCH_HARNESS_STATE_FILE", str(state_file))
    monkeypatch.setenv("DISPATCH_INTERACTIVE_DIR", str(ROOT))
    monkeypatch.setenv("DISPATCH_INTERACTIVE_SESSION_DOC", "__none__")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_PERSONA", "__astartes__")
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


def test_dispatch_interactive_vulkan_uses_current_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKEN_API_DISPATCH_ORIGIN", "d")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_DIR", str(ROOT))
    monkeypatch.setenv("DISPATCH_INTERACTIVE_SESSION_DOC", "__none__")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_PERSONA", "vulkan")
    monkeypatch.setenv("DISPATCH_INTERACTIVE_MODE", "one_off")

    for harness in ("codex", "claude"):
        state_file = tmp_path / f"dispatch-harness-{harness}"
        state_file.write_text(f"{harness}\n", encoding="utf-8")
        monkeypatch.setenv("DISPATCH_HARNESS_STATE_FILE", str(state_file))
        result = subprocess.run(
            [str(DISPATCH), "--dry-run", "--direct", "vulkan work"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(ROOT),
        )

        assert result.returncode == 0, result.stderr
        assert f"engine:          {harness}" in result.stdout
        assert "TOKEN_API_PERSONA=vulkan" in result.stdout


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
    assert "agent-wrapper.sh claude" in custodes.stdout
    assert "TOKEN_API_PERSONA=custodes" in custodes.stdout

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
    assert "TOKEN_API_CODEX_PROFILE=inquisitor" not in inquisitor.stdout
    assert "TOKEN_API_CODEX_PROFILE=sisters-of-battle" not in inquisitor.stdout
    assert "TOKEN_API_PERSONA=inquisitor" in inquisitor.stdout
    assert "agent-wrapper.sh codex" in inquisitor.stdout
    assert "dispatch_codex_launch_inline" not in inquisitor.stdout

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
    assert "TOKEN_API_PERSONA=vulkan" in vulkan.stdout
    assert "primarch vulkan" not in vulkan.stdout
    assert "TOKEN_API_CODEX_PROFILE=sisters-of-battle" not in vulkan.stdout


def test_dispatch_mechanicus_persona_prompt_does_not_export_identity() -> None:
    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--direct",
            "--target",
            "mechanicus:new",
            "--persona",
            "mechanicus",
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
    )

    assert result.returncode == 0, result.stderr
    assert "persona:         mechanicus" in result.stdout
    assert "TOKEN_API_DISPATCH_TARGET=mechanicus:new" in result.stdout
    assert "TOKEN_API_PERSONA=" not in result.stdout


def test_dispatch_codex_profile_is_explicit_env_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOKEN_API_CODEX_PROFILE", "custom-profile")
    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--direct",
            "--no-worktree",
            "--engine",
            "codex",
            "--dir",
            str(ROOT),
            "codex work",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0, result.stderr
    assert "TOKEN_API_CODEX_PROFILE=custom-profile" in result.stdout


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
        "#!/usr/bin/env bash\n"
        # Log only the stack-dispatch invocation; the canonical-id resolve probe
        # (resolve-pane --format physical) must answer with the physical pane but
        # not clobber the logged command. Emit the canonical page:index id from
        # stack dispatch so the resolve step is genuinely exercised.
        'if [[ "$1" == "stack" && "$2" == "dispatch" ]]; then\n'
        '  printf "%s\\n" "$*" > "$TMUXCTL_LOG"\n'
        '  printf "mechanicus:1\\n"\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == "resolve-pane" && "$2" == "--format" && "$3" == "physical" ]]; then\n'
        '  printf "%%83\\n"\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)
    tmux_log = tmp_path / "tmux.log"
    ping_log = tmp_path / "tmuxctld-ping.log"
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
    env["TMUXCTLD_PING_LOG"] = str(ping_log)
    env["TMUXCTLD_PING_STACK_DISPATCH_RESULT"] = "mechanicus:1"
    env["TMUXCTLD_PING_RESOLVE_PHYSICAL"] = "%83"
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
            "mechanicus:new",
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
    assert "dispatched claude to mechanicus:new" in result.stdout
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
    assert "--target mechanicus:new" in result.stdout
    assert "--session-doc" in result.stdout
    assert "--system-prompt-file" in result.stdout
    assert "--prompt-file" in result.stdout
    tmux_text = tmux_log.read_text(encoding="utf-8", errors="replace")
    assert "send-keys" not in tmux_text
    ping_text = ping_log.read_text(encoding="utf-8", errors="replace")
    # tmuxctld spawns the pane with a throwaway `clear` warmup; the real
    # `bash <staged>` launch is also sent through tmuxctld using the canonical pane id.
    assert "POST /stack/dispatch base=mechanicus session=main" in ping_text
    assert "command=clear" in ping_text
    assert "POST /send-text pane=mechanicus:1 text=bash " in ping_text
    assert "%83" not in ping_text
    staged_path = Path(_staged_command_from_tmuxctld_log(ping_log).split(" ", 1)[1])
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
    assert "--target mechanicus:new" not in result.stdout
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
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" > "$TMUXCTL_LOG"\n'
        # Campaign contract: stack dispatch emits canonical page:index; dispatch
        # materializes physical only at the raw-tmux send via resolve-pane.
        'if [[ "$1" == "stack" && "$2" == "dispatch" ]]; then printf "mechanicus:3\\n"; exit 0; fi\n'
        'if [[ "$1" == "resolve-pane" && "$2" == "--format" && "$3" == "physical" ]]; then printf "%%84\\n"; exit 0; fi\n'
        'printf "%%84\\n"\n',
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)
    tmux_log = tmp_path / "tmux.log"
    ping_log = tmp_path / "tmuxctld-ping.log"
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
    env["TMUXCTLD_PING_LOG"] = str(ping_log)
    env["TMUXCTLD_PING_STACK_DISPATCH_RESULT"] = "mechanicus:3"
    env["TMUXCTLD_PING_RESOLVE_PHYSICAL"] = "%84"

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
            "mechanicus:new",
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
    assert "dispatched codex to mechanicus:new" in result.stdout

    tmux_text = tmux_log.read_text(encoding="utf-8", errors="replace")
    assert "send-keys" not in tmux_text
    ping_text = ping_log.read_text(encoding="utf-8", errors="replace")
    assert "POST /send-text pane=mechanicus:3 text=bash " in ping_text
    assert "%84" not in ping_text
    staged_path = Path(_staged_command_from_tmuxctld_log(ping_log).split(" ", 1)[1])
    staged = staged_path.read_text(encoding="utf-8", errors="replace")
    assert "agent-wrapper.sh codex" in staged
    assert "dispatch_codex_launch_inline" not in staged
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
            "mechanicus:new",
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
    assert "--target mechanicus:new" in popup
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
            "mechanicus:new",
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
    # State-hook dispatcher targets the council:custodes slot without a persona.
    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--direct",
            "--target",
            "council:custodes",
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


def test_dispatch_does_not_inherit_dispatcher_legion_into_worker() -> None:
    # P1 (2026-06-18) persona theft via the emperor-commander path: a custodes pane
    # carries an exported TOKEN_API_LEGION=custodes (and TOKEN_API_PERSONA=custodes)
    # from its own launch. Dispatching a worker into a NON-custodes seat with no
    # --persona must NOT forward the dispatcher's legion/persona into the child env —
    # else the server infers persona=custodes and the singleton guard retires the live
    # incumbent. The child legion must resolve fresh (empty here → server assigns a
    # fresh worker identity).
    env = os.environ.copy()
    env["TOKEN_API_LEGION"] = "custodes"
    env["TOKEN_API_PERSONA"] = "custodes"
    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--direct",
            "--target",
            "palace:new",
            "--dir",
            str(ROOT),
            "worker task",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "TOKEN_API_LEGION=custodes" not in result.stdout, result.stdout
    # No --persona was given, so no persona must be forwarded to the child either.
    assert "TOKEN_API_PERSONA=custodes" not in result.stdout, result.stdout


def test_dispatch_palace_shorthand_detaches_and_carries_commander_not_persona() -> None:
    """Custodes-style `dispatch palace ...` must mean palace:new, not inline self.

    The parent instance id is allowed through only as a commander edge. Persona,
    legion, session, and own-instance identity from the caller remain scrubbed so
    the child cannot become a Custodes clone.
    """
    env = os.environ.copy()
    env.update(
        {
            "TOKEN_API_INSTANCE_ID": "custodes-live-id",
            "TOKEN_API_PARENT_INSTANCE_ID": "emperor-or-old-parent",
            "TOKEN_API_LEGION": "custodes",
            "TOKEN_API_PERSONA": "custodes",
            "TOKEN_API_SESSION_DOC_ID": "custodes-doc",
            "TOKEN_API_INSTANCE_NAME": "custodes",
            "TOKEN_API_WRAPPER_ID": "",
            "TOKEN_API_WRAPPER_LAUNCH_ID": "",
            "TMUX_PANE": "",
        }
    )
    result = subprocess.run(
        [
            str(DISPATCH),
            "palace",
            "--dry-run",
            "--direct",
            "--dir",
            str(ROOT),
            "--prompt",
            "worker task",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "target:          palace:new" in result.stdout
    assert "launch_mode:     " not in result.stdout
    assert "TOKEN_API_DISPATCH_TARGET=palace:new" in result.stdout
    assert "TOKEN_API_PARENT_INSTANCE_ID=custodes-live-id" in result.stdout
    assert "TOKEN_API_PARENT_INSTANCE_ID=emperor-or-old-parent" not in result.stdout
    assert "TOKEN_API_PERSONA=custodes" not in result.stdout
    assert "TOKEN_API_LEGION=custodes" not in result.stdout
    assert "-u TOKEN_API_INSTANCE_ID" in result.stdout
    assert "-u TOKEN_API_PERSONA" in result.stdout


def test_dispatch_self_is_explicit_transplant_boundary() -> None:
    env = os.environ.copy()
    env["TOKEN_API_INSTANCE_ID"] = "caller-instance-id"

    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--direct",
            "--self",
            "--dir",
            str(ROOT),
            "--prompt",
            "continue here",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "target:          self" in result.stdout
    assert "--self" in result.stdout
    assert "TOKEN_API_TRANSPLANT_EXPECTED=true" in result.stdout
    assert "TOKEN_API_PARENT_INSTANCE_ID=caller-instance-id" not in result.stdout


def test_dispatch_target_dry_run_carries_canonical_without_physical(tmp_path):
    """The dry-run carries the canonical working id verbatim and never leaks physical.

    With the per-consumer public/physical resolve wrappers retired, dispatch no
    longer eagerly remaps the target for display — `resolved_target` shows the
    canonical working id as given. The load-bearing invariant is unchanged: a
    physical %NNN must never surface on the dry-run diagnostic.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        "#!/usr/bin/env bash\n"
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
    assert "resolved_target: 2:NE" in result.stdout
    assert "%22" not in result.stdout


def test_dispatch_prealloc_new_uses_greedy_first_free_without_stack_spawn(tmp_path: Path) -> None:
    """palace:new is a pre-alloc freelist allocation, not stack spawn.

    Red-first regression for the live failure: the old dispatch path sent every
    :new target through /stack/dispatch, which makes palace:new fail with
    ``not a stack window: palace``.  The fixed path must query the ledger-derived
    freelist, choose the first free pane in the page's configured prealloc order,
    and inject via tmux_target semantics without spawning or tearing down panes.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ping_log = tmp_path / "tmuxctld-ping.log"
    tmux_log = tmp_path / "tmux.log"

    fake_ping = fake_bin / "tmuxctld-ping"
    fake_ping.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf '%s\\n' \"$*\" >> {ping_log}\n"
        'if [[ "${TMUXCTLD_PING_PRINT_RESPONSE:-}" != "1" ]]; then exit 0; fi\n'
        'method="${1:-}"; path="${2:-}"; shift 2 || true\n'
        'target=""\n'
        'for arg in "$@"; do case "$arg" in target=*) target="${arg#target=}" ;; esac; done\n'
        'case "$method $path" in\n'
        '  "GET /freelist") printf \'%s\' \'[{"pane_id":"palace:S","pane_role":"palace:S","window_name":"palace"},{"pane_id":"palace:N","pane_role":"palace:N","window_name":"palace"},{"pane_id":"somnium:W","pane_role":"somnium:W","window_name":"somnium"}]\' | python3 -c \'import json,sys; print(json.dumps({"ok": True, "result": json.loads(sys.stdin.read())}))\' ;;\n'
        '  "POST /stack/dispatch") printf \'%s\' \'{"ok":false,"error":{"message":"not a stack window: palace"}}\' ;;\n'
        '  "GET /resolve-pane"|"POST /resolve-pane") printf \'{"ok":true,"result":"%%88"}\' ;;\n'
        '  "POST /send-text") printf \'{"ok":true,"result":{"delivery":"confirmed"}}\' ;;\n'
        '  "POST /pane-live") printf \'{"ok":true,"result":{"live":true}}\' ;;\n'
        '  *) printf \'{"ok":true,"result":""}\' ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    fake_ping.chmod(0o755)

    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> {tmux_log}\n"
        'if [[ "$1" == "display-message" ]]; then printf \'bash||palace:N|999|\\n\'; exit 0; fi\n'
        'if [[ "$1" == "show-options" ]]; then printf \'palace:N\\n\'; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"

    result = subprocess.run(
        [
            str(DISPATCH),
            "--target",
            "palace:new",
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
    assert "not a stack window: palace" not in result.stderr
    ping_text = ping_log.read_text(encoding="utf-8", errors="replace")
    assert "GET /freelist" in ping_text
    assert "POST /stack/dispatch" not in ping_text
    assert "POST /send-text pane=palace:N text=bash " in ping_text
    assert "POST /send-text pane=palace:S" not in ping_text
    staged = Path(_staged_command_from_tmuxctld_log(ping_log).split(" ", 1)[1]).read_text(
        encoding="utf-8", errors="replace"
    )
    assert "TOKEN_API_LAUNCH_MODE=tmux_target" in staged
    assert "TOKEN_API_DISPATCH_TARGET=palace:new" in staged
    assert "TOKEN_API_DISPATCH_RESOLVED_PANE=%88" in staged
    assert "TOKEN_API_DISPATCH_RESOLVED_PANE=palace:new" not in staged


def test_dispatch_stack_dispatch_has_no_hard_60s_transport_ceiling(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ping_log = tmp_path / "tmuxctld-ping.log"
    tmux_log = tmp_path / "tmux.log"

    fake_ping = fake_bin / "tmuxctld-ping"
    fake_ping.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'method="${1:-}"; path="${2:-}"; shift 2 || true\n'
        f'printf \'%s %s max=%s\\n\' "$method" "$path" "${{TMUXCTLD_MAX_TIME:-unset}}" >> {ping_log}\n'
        'if [[ "${TMUXCTLD_PING_PRINT_RESPONSE:-}" != "1" ]]; then exit 0; fi\n'
        'case "$method $path" in\n'
        '  "POST /stack/dispatch") printf \'{"ok":true,"result":"mechanicus:2"}\' ;;\n'
        '  "GET /resolve-pane"|"POST /resolve-pane") printf \'{"ok":true,"result":"%%77"}\' ;;\n'
        '  "POST /send-text") printf \'{"ok":true,"result":{"delivery":"confirmed"}}\' ;;\n'
        '  "POST /pane-live") printf \'{"ok":true,"result":{"live":true}}\' ;;\n'
        '  "POST /live-agents") printf \'{"ok":true,"result":""}\' ;;\n'
        '  *) printf \'{"ok":true,"result":""}\' ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    fake_ping.chmod(0o755)

    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> {tmux_log}\n"
        'if [[ "$1" == "display-message" ]]; then printf "%%77\\n"; exit 0; fi\n'
        'if [[ "$1" == "show-options" ]]; then printf "mechanicus:2\\n"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"
    env.pop("TMUXCTLD_MAX_TIME", None)
    env.pop("TMUXCTLD_STACK_DISPATCH_MAX_TIME", None)

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
    log = ping_log.read_text(encoding="utf-8")
    assert "POST /stack/dispatch max=0" in log
    assert "POST /stack/dispatch max=60" not in log
    assert "POST /send-text max=60" in log


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
    ping_log = tmp_path / "tmuxctld-ping.log"

    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        "#!/usr/bin/env bash\n"
        # The canonical-id campaign's contract: `stack dispatch` prints page:index.
        'if [[ "$1" == "stack" && "$2" == "dispatch" ]]; then echo "mechanicus:2"; exit 0; fi\n'
        # dispatch materializes physical only at the raw-tmux send site, via the
        # oracle: `resolve-pane --format physical <canonical>` -> %NNN.
        'if [[ "$1" == "resolve-pane" && "$2" == "--format" && "$3" == "physical" ]]; then echo "%77"; exit 0; fi\n'
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
    env["TMUXCTLD_PING_LOG"] = str(ping_log)

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
    # The staged launcher is sent through tmuxctld using the canonical pane id;
    # raw tmux is still used only for non-byte-bearing pane option cleanup.
    assert "%77" in calls, f"physical pane option cleanup did not target %77; calls={calls}"
    ping_text = ping_log.read_text(encoding="utf-8", errors="replace")
    assert "POST /send-text pane=mechanicus:2 text=bash " in ping_text
    send_lines = "\n".join(line for line in ping_text.splitlines() if "POST /send-text" in line)
    assert "%77" not in send_lines
    staged_path = Path(_staged_command_from_tmuxctld_log(ping_log).split(" ", 1)[1])
    content = staged_path.read_text(encoding="utf-8")

    # PR-A keeps the child env (and DB tmux_pane column) PHYSICAL: the concrete
    # pane is materialized into the launch env so SessionStart registers it. The
    # canonical id is dispatch's internal working identity, never the launch env.
    assert "TMUX_PANE=%77" in content, content
    assert "TOKEN_API_DISPATCH_RESOLVED_PANE=%77" in content, content
    assert "TMUX_PANE=mechanicus:new" not in content, content
    assert "TOKEN_API_DISPATCH_RESOLVED_PANE=mechanicus:new" not in content, content
    assert "TOKEN_API_DISPATCH_RESOLVED_PANE=mechanicus:2" not in content, content
    # The allocation token remains the semantic request target.
    assert "TOKEN_API_DISPATCH_TARGET=mechanicus:new" in content, content


def test_dispatch_stack_new_accepts_public_pane_id_return(tmp_path: Path) -> None:
    """Stack dispatch may return the canonical public pane id (mechanicus:N).

    Dispatch must accept that human-facing surface and translate it to the
    physical tmux pane before staging the launch command.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    rec = tmp_path / "tmux_calls.txt"
    ping_log = tmp_path / "tmuxctld-ping.log"

    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "stack" && "$2" == "dispatch" ]]; then echo "mechanicus:6"; exit 0; fi\n'
        'if [[ "$1" == "resolve-pane" && "$2" == "--format" && "$3" == "physical" && "$4" == "mechanicus:6" ]]; then echo "%77"; exit 0; fi\n'
        "exit 1\n",
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
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"
    env["TMUXCTLD_PING_LOG"] = str(ping_log)
    env["TMUXCTLD_PING_STACK_DISPATCH_RESULT"] = "mechanicus:6"
    env["TMUXCTLD_PING_RESOLVE_PHYSICAL"] = "%77"

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
    assert "non-pane id" not in result.stderr

    calls = [c for c in rec.read_bytes().decode("utf-8", "replace").split("\0") if c]
    assert "-t" in calls and "%77" in calls, calls
    ping_text = ping_log.read_text(encoding="utf-8", errors="replace")
    assert "POST /send-text pane=mechanicus:6 text=bash " in ping_text
    send_lines = "\n".join(line for line in ping_text.splitlines() if "POST /send-text" in line)
    assert "%77" not in send_lines
    content = Path(_staged_command_from_tmuxctld_log(ping_log).split(" ", 1)[1]).read_text(
        encoding="utf-8"
    )
    assert "TMUX_PANE=%77" in content, content
    assert "TOKEN_API_DISPATCH_RESOLVED_PANE=%77" in content, content
    assert "TMUX_PANE=mechanicus:6" not in content, content


# --- Step 3: rank-based persona behavior resolver / row-file invariant ---------


def _persona_db(tmp_path: Path, rows: list[tuple[str, str, str]]) -> Path:
    import sqlite3

    db = tmp_path / "agents.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE personas (id TEXT PRIMARY KEY, slug TEXT UNIQUE, display_name TEXT, default_rank TEXT)"
        )
        for idx, (slug, display, rank) in enumerate(rows):
            conn.execute(
                "INSERT INTO personas (id, slug, display_name, default_rank) VALUES (?, ?, ?, ?)",
                (f"p{idx}", slug, display, rank),
            )
    return db


def _persona_env(tmp_path: Path, db: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["TOKEN_API_DB"] = str(db)
    imperium = tmp_path / "vaults" / "Imperium"
    civic = tmp_path / "vaults" / "Civic"
    (imperium / "Imperium-ENV" / "Personas").mkdir(parents=True)
    (civic / "Pax-ENV" / "Personas").mkdir(parents=True)
    # The rank+persona staple invariant now requires a rank doc per persona, so
    # provision the standard Ranks/ tree in both vaults. Tests that exercise a
    # MISSING behavior file still fail closed on the behavior half (the rank half
    # is satisfied here), keeping those assertions about the behavior file.
    for root in (imperium / "Imperium-ENV", civic / "Pax-ENV"):
        ranks = root / "Personas" / "Ranks"
        ranks.mkdir(parents=True, exist_ok=True)
        for rank in ("Astartes", "Overseer", "Primarch"):
            (ranks / f"{rank}.md").write_text(f"{rank} rank doctrine\n", encoding="utf-8")
    env["IMPERIUM"] = str(imperium)
    env["CIVIC"] = str(civic)
    return env


def test_dispatch_persona_invariant_fails_loud_when_astartes_file_missing(tmp_path: Path) -> None:
    db = _persona_db(tmp_path, [("blood-angels", "Blood Angels", "astartes")])
    env = _persona_env(tmp_path, db)

    result = subprocess.run(
        [str(DISPATCH), "--dry-run", "--direct", "--dir", str(ROOT), "work"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
        timeout=60,
    )

    assert result.returncode == 66
    assert "persona behavior file missing: slug=blood-angels" in result.stderr
    assert "persona behavior-file invariant failed" in result.stderr


def test_dispatch_persona_invariant_fails_closed_when_rank_doc_missing(tmp_path: Path) -> None:
    """The staple is rank+persona, so preflight must refuse dispatch when a managed
    persona resolves a behavior doc but no rank doc — as fatal as a missing behavior
    file. (The wrapper fails loud-but-open at runtime; the hard gate is here.)"""
    db = _persona_db(tmp_path, [("blood-angels", "Blood Angels", "astartes")])
    env = _persona_env(tmp_path, db)
    # Behavior half present, rank half removed → the rank-doc invariant must fire.
    (Path(env["IMPERIUM"]) / "Imperium-ENV" / "Personas" / "Astartes.md").write_text(
        "## System Prompt\nGeneric\n", encoding="utf-8"
    )
    shutil.rmtree(Path(env["IMPERIUM"]) / "Imperium-ENV" / "Personas" / "Ranks")

    result = subprocess.run(
        [str(DISPATCH), "--dry-run", "--direct", "--dir", str(ROOT), "work"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
        timeout=60,
    )

    assert result.returncode == 66
    assert "persona rank doc missing: slug=blood-angels" in result.stderr
    assert "persona behavior-file invariant failed" in result.stderr


def test_dispatch_persona_forwards_metadata_env_without_injecting_doctrine_body(
    tmp_path: Path,
) -> None:
    """Dispatch no longer assembles/injects the persona doctrine body — the agent
    wrapper is the sole injector (TOKEN_API_PERSONA → token_wrapper_system_doc).
    Dispatch only resolves the persona (so preflight passes) and forwards the
    *operational* metadata (vault domain/session doc) as env the wrapper
    folds in beneath the staple. The doctrine body must NOT appear on the staged
    command line, and there must be no double-inject via --append-system-prompt."""
    db = _persona_db(tmp_path, [("blood-angels", "Blood Angels", "astartes")])
    env = _persona_env(tmp_path, db)
    (Path(env["IMPERIUM"]) / "Imperium-ENV" / "Personas" / "Astartes.md").write_text(
        "## System Prompt\nGeneric rank behavior\n", encoding="utf-8"
    )

    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--direct",
            "--dir",
            str(ROOT),
            "--persona",
            "blood-angels",
            "work",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "persona:         blood-angels" in result.stdout
    # Operational metadata is forwarded as env; doctrine body is NOT injected here.
    assert "TOKEN_API_INSTANCE_NAME_PREFIX" not in result.stdout
    assert "TOKEN_API_VAULT_DOMAIN=Imperium-ENV" in result.stdout
    assert "Generic rank behavior" not in result.stdout
    assert "--append-system-prompt" not in result.stdout


def test_dispatch_singleton_caller_identity_not_grafted_onto_worker(tmp_path: Path) -> None:
    db = _persona_db(tmp_path, [("blood-angels", "Blood Angels", "astartes")])
    env = _persona_env(tmp_path, db)
    (Path(env["IMPERIUM"]) / "Imperium-ENV" / "Personas" / "Astartes.md").write_text(
        "## System Prompt\nGeneric\n", encoding="utf-8"
    )
    env.update(
        {
            "TMUX_PANE": "%55",
            "TOKEN_API_INSTANCE_ID": "fabricator-general-live-id",
            "TOKEN_API_PERSONA": "fabricator-general",
            "TOKEN_API_PARENT_INSTANCE_ID": "fabricator-general-live-id",
            "TOKEN_API_LEGION": "mechanicus",
            "TOKEN_API_SESSION_DOC_ID": "2058",
            "SESSION_DOC_ID": "2058",
            "TOKEN_API_INSTANCE_NAME": "fabricator-general",
            "INSTANCE_NAME": "fabricator-general",
            "TOKEN_API_DISPLAY_NAME": "Fabricator-General",
            "TOKEN_API_WRAPPER_ID": "",
            "TOKEN_API_WRAPPER_LAUNCH_ID": "",
        }
    )

    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--direct",
            "--dir",
            str(ROOT),
            "--target",
            "mechanicus:new",
            "worker task",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    # The caller instance may be forwarded only as a commander edge. It must not
    # carry the caller's own persona/legion/session/name into the child.
    assert "TOKEN_API_PARENT_INSTANCE_ID=fabricator-general-live-id" in result.stdout
    assert "TOKEN_API_PERSONA=fabricator-general" not in result.stdout
    assert "TOKEN_API_LEGION=mechanicus" not in result.stdout
    assert "TOKEN_API_SESSION_DOC_ID=2058" not in result.stdout
    assert "SESSION_DOC_ID=2058" not in result.stdout
    assert "TOKEN_API_INSTANCE_NAME=fabricator-general" not in result.stdout
    assert "INSTANCE_NAME=fabricator-general" not in result.stdout
    assert "TOKEN_API_DISPLAY_NAME=Fabricator-General" not in result.stdout
    assert "-u TMUX_PANE" in result.stdout
    assert "-u TOKEN_API_INSTANCE_ID" in result.stdout
    assert "-u TOKEN_API_PARENT_INSTANCE_ID" in result.stdout
    assert "-u TOKEN_API_PERSONA" in result.stdout
    assert "-u TOKEN_API_DISPLAY_NAME" in result.stdout
    assert "-u DISPLAY_NAME" in result.stdout
    assert "-u TOKEN_API_SESSION_DOC_ID" in result.stdout
    assert "-u SESSION_DOC_ID" in result.stdout
    assert "-u TOKEN_API_INSTANCE_NAME" in result.stdout
    assert "-u INSTANCE_NAME" in result.stdout
    assert "-u TOKEN_API_LEGION" in result.stdout


def test_dispatch_prompt_file_single_quote_is_shell_safe(tmp_path: Path) -> None:
    db = _persona_db(tmp_path, [("blood-angels", "Blood Angels", "astartes")])
    env = _persona_env(tmp_path, db)
    (Path(env["IMPERIUM"]) / "Imperium-ENV" / "Personas" / "Astartes.md").write_text(
        "## System Prompt\nGeneric\n", encoding="utf-8"
    )
    prompt = tmp_path / "prompt.md"
    prompt.write_text("it's time — don't break ✓\n", encoding="utf-8")

    result = subprocess.run(
        [str(DISPATCH), "--dry-run", "--direct", "--dir", str(ROOT), "--prompt-file", str(prompt)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    final_parts = result.stdout.split("  final_command:\n    ", 1)
    assert len(final_parts) == 2, result.stdout
    final = final_parts[1].splitlines()[0]
    syntax = subprocess.run(["bash", "-n", "-c", final], capture_output=True, text=True, timeout=60)
    assert syntax.returncode == 0, syntax.stderr


def test_dispatch_stack_new_resolves_canonical_id_to_physical_pane(tmp_path: Path) -> None:
    """A :new stack launch must survive tmuxctl emitting a canonical page:index id.

    Regression for the tmuxctl canonical-id campaign: `tmuxctl stack dispatch`
    now prints the public `page:index` id (e.g. mechanicus:2), not a legacy raw
    %NNN. dispatch used to `grep -Eo '%[0-9]+'` that output, get '', and fatal
    with "stack dispatch returned a non-pane id" — every fresh-stack launch dead.
    dispatch must now resolve whatever tmuxctl emits to a physical %NNN via
    `resolve-pane --format physical` and bake THAT into the launch env so the
    agent's first SessionStart registers the real pane (correct persona, not a
    default-astartes ghost).
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    rec = tmp_path / "tmux_calls.txt"
    ping_log = tmp_path / "tmuxctld-ping.log"

    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        "#!/usr/bin/env bash\n"
        # The campaign's canonical output: page:index, NOT %NNN.
        'if [[ "$1" == "stack" && "$2" == "dispatch" ]]; then echo "mechanicus:2"; exit 0; fi\n'
        # resolve_dispatch_physical_target maps the canonical id back to physical.
        'if [[ "$1" == "resolve-pane" && "$2" == "--format" && "$3" == "physical" ]]; then echo "%77"; exit 0; fi\n'
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
    env["TMUXCTLD_PING_LOG"] = str(ping_log)

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

    # Must NOT fatal with the non-pane-id error the grep produced.
    assert result.returncode == 0, result.stderr
    assert "non-pane id" not in result.stderr

    calls = [c for c in rec.read_bytes().decode("utf-8", "replace").split("\0") if c]
    # The launch env still bakes the resolved physical pane, but the byte-bearing
    # launch send itself goes through tmuxctld on the canonical id.
    assert "-t" in calls and "%77" in calls, (
        f"physical pane option cleanup did not target %77; calls={calls}"
    )
    ping_text = ping_log.read_text(encoding="utf-8", errors="replace")
    assert "POST /send-text pane=mechanicus:2 text=bash " in ping_text
    send_lines = "\n".join(line for line in ping_text.splitlines() if "POST /send-text" in line)
    assert "%77" not in send_lines
    staged_path = Path(_staged_command_from_tmuxctld_log(ping_log).split(" ", 1)[1])
    content = staged_path.read_text(encoding="utf-8")

    assert "TMUX_PANE=%77" in content, content
    assert "TOKEN_API_DISPATCH_RESOLVED_PANE=%77" in content, content
    assert "TMUX_PANE=mechanicus:2" not in content, content
    assert "TOKEN_API_DISPATCH_RESOLVED_PANE=mechanicus:2" not in content, content
    # The allocation token remains the semantic request target.
    assert "TOKEN_API_DISPATCH_TARGET=mechanicus:new" in content, content


def test_dispatch_stack_new_rejects_non_canonical_id(tmp_path: Path) -> None:
    """The inverted guard validates the canonical page:index contract.

    Dog-food saturation: dispatch no longer accepts a legacy raw %NNN (or any
    non-canonical token) from `stack dispatch`. A non-canonical emit is a loud
    error, not a silent fallthrough — the inverse of the old `=~ ^%[0-9]+$` guard.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        "#!/usr/bin/env bash\n"
        # A pre-campaign tmuxctl that still emits a raw %NNN must now be rejected.
        'if [[ "$1" == "stack" && "$2" == "dispatch" ]]; then echo "%77"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)

    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_tmux.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"
    env["TMUXCTLD_PING_STACK_DISPATCH_RESULT"] = "%77"

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

    assert result.returncode != 0
    assert "non-canonical id" in result.stderr, result.stderr


def test_normalize_pane_to_canonical_ingest_helper(tmp_path: Path) -> None:
    """The ingest helper canonicalizes inbound pane ids; fails open to physical.

    page:index passthrough; raw %NNN / self -> tmuxctld `/resolve-pane?format=id`; a pane
    with no canonical cardinal (resolver emits nothing) keeps its physical id.
    """
    common = ROOT / "cli-tools" / "lib" / "agent-wrapper-common.sh"

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        'target=""\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in target=*) target="${arg#target=}" ;; esac\n'
        "done\n"
        'if [[ "$target" == "%77" ]]; then printf \'{"result":"mechanicus:2"}\'; else printf \'{"result":""}\'; fi\n',
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    def normalize(value: str) -> str:
        script = f'source "{common}"; normalize_pane_to_canonical "{value}"'
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
        out = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        return out.stdout

    # Already-canonical: passthrough, no resolver hop.
    assert normalize("mechanicus:2") == "mechanicus:2"
    # Raw physical with a cardinal: canonicalized.
    assert normalize("%77") == "mechanicus:2"
    # Raw physical with NO cardinal: fail open to the physical id (no regression).
    assert normalize("%99") == "%99"


def test_dispatch_direct_objective_without_brief_warns(tmp_path: Path) -> None:
    """A direct launch with --objective but no brief boots idle — warn the operator.

    --objective is aspirant-only metadata; on a --direct/--target launch it is
    delivered nowhere. Without --prompt/--prompt-file/--session-doc the agent
    boots with no instructions (the 2026-06-20 briefless-canary trap). dispatch
    must emit a clear warning (non-fatal) pointing the operator at the real
    brief flags.
    """
    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--direct",
            "--dir",
            str(ROOT),
            "--objective",
            "do the important thing",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0, result.stderr
    assert "--objective is aspirant-only metadata" in result.stderr
    assert "boot idle" in result.stderr or "boots idle" in result.stderr


def test_dispatch_stack_new_empty_tmuxctl_output_fails_with_clear_error(tmp_path: Path) -> None:
    """When tmuxctl emits nothing, the operator gets the clear non-canonical error.

    The tail step runs under `set -euo pipefail`; an empty tmuxctl result must
    not abort the assignment silently — it must fall through to the explicit
    'stack dispatch returned a non-canonical id' guard so the failure is legible.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_tmuxctl = fake_bin / "tmuxctl"
    # stack dispatch succeeds but prints nothing; resolve-pane also empty.
    fake_tmuxctl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_tmuxctl.chmod(0o755)
    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_tmux.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"
    env["TMUXCTLD_PING_STACK_DISPATCH_RESULT"] = ""

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

    assert result.returncode != 0
    assert "stack dispatch returned a non-canonical id" in result.stderr


def test_dispatch_stack_new_launch_failure_when_no_live_agent(tmp_path: Path) -> None:
    """A sent launch where NO live agent ever comes up is the only exit-70 case.

    The success criterion is liveness (a live Claude/Codex process in the pane),
    not a DB-row bind. This is the genuine failure mode the guard still catches:
    the newborn pane receives a corrupted/no-op staged command, the pane stays up
    as a bare shell, but no agent process appears — ``tmuxctl pane-live`` reports
    not-live for the whole (advisory) window and dispatch fails exit 70 rather
    than printing a false "dispatched ..." success.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmux_log = tmp_path / "tmux.log"

    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "stack" && "$2" == "dispatch" ]]; then echo "mechanicus:2"; exit 0; fi\n'
        'if [[ "$1" == "resolve-pane" && "$2" == "--format" && "$3" == "physical" ]]; then echo "%77"; exit 0; fi\n'
        # No live agent ever appears in the pane → not-live for the whole window.
        'if [[ "$1" == "pane-live" ]]; then exit 1; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)

    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {tmux_log}\n'
        # The pane survives (display-message succeeds) but hosts no agent — the
        # "staged command died to a bare shell" case, not a vanished pane.
        'if [[ "$1" == "display-message" ]]; then printf "%%77\\n"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"
    env["DISPATCH_LAUNCH_OBSERVE_TIMEOUT"] = "1"
    env["TMUXCTLD_PING_PANE_LIVE"] = "false"

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

    assert result.returncode == 70
    assert "launch failed: no live claude agent appeared" in result.stderr
    assert "dispatched claude" not in result.stdout
    assert "%77" not in result.stderr
    tmux_calls = tmux_log.read_text(encoding="utf-8")
    assert "DISPATCH LAUNCH FAILED" not in tmux_calls
    assert not any(
        line.startswith("send-keys -t %77") and "launch failed" in line.lower()
        for line in tmux_calls.splitlines()
    )


def test_dispatch_stack_new_observer_accepts_live_agent(tmp_path: Path) -> None:
    """A live agent process in the pane is success — regardless of the row bind."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmux_log = tmp_path / "tmux.log"

    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "stack" && "$2" == "dispatch" ]]; then echo "mechanicus:2"; exit 0; fi\n'
        'if [[ "$1" == "resolve-pane" && "$2" == "--format" && "$3" == "physical" ]]; then echo "%77"; exit 0; fi\n'
        # The pane is running a live agent → success criterion met immediately.
        'if [[ "$1" == "pane-live" ]]; then exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)

    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {tmux_log}\n'
        'if [[ "$1" == "display-message" ]]; then printf "%%77\\n"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"
    env["DISPATCH_LAUNCH_OBSERVE_TIMEOUT"] = "1"

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
    assert "dispatched claude to mechanicus:new" in result.stdout
    assert "DISPATCH LAUNCH FAILED" not in tmux_log.read_text(encoding="utf-8")


def test_dispatch_tmux_target_rejects_protected_singleton_seat(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmux_log = tmp_path / "tmux.log"

    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "resolve-pane" && "$2" == "--format" && "$3" == "physical" ]]; then echo "%27"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)

    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {tmux_log}\n'
        'if [[ "$1" == "display-message" ]]; then printf "bash||council:custodes|999|\\n"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"

    result = subprocess.run(
        [
            str(DISPATCH),
            "--target",
            "council:custodes",
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

    assert result.returncode == 73
    assert "protected singleton seat" in result.stderr
    assert "send-keys" not in tmux_log.read_text(encoding="utf-8")


def test_dispatch_tmux_target_sends_existing_canonical_target_via_tmuxctld(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmux_log = tmp_path / "tmux.log"
    ping_log = tmp_path / "tmuxctld-ping.log"

    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "resolve-pane" && "$2" == "--format" && "$3" == "physical" ]]; then echo "%44"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)

    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {tmux_log}\n'
        'if [[ "$1" == "show-options" ]]; then echo "palace:E"; exit 0; fi\n'
        'if [[ "$1" == "display-message" ]]; then printf "bash||palace:E|999|\\n"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"
    env["TMUXCTLD_PING_LOG"] = str(ping_log)

    result = subprocess.run(
        [
            str(DISPATCH),
            "--target",
            "palace:E",
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
    tmux_text = tmux_log.read_text(encoding="utf-8", errors="replace")
    assert "send-keys" not in tmux_text
    ping_text = ping_log.read_text(encoding="utf-8", errors="replace")
    assert "POST /send-text pane=palace:E text=clear submit=true" in ping_text
    assert "POST /send-text pane=palace:E text=bash " in ping_text
    assert "%44" not in ping_text


def test_dispatch_tmux_target_rejects_live_agent_descendant_without_instance_stamp(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmux_log = tmp_path / "tmux.log"

    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "resolve-pane" && "$2" == "--format" && "$3" == "physical" ]]; then echo "%44"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)

    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {tmux_log}\n'
        'if [[ "$1" == "display-message" ]]; then printf "bash||palace:E|999|\\n"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)

    fake_ps = fake_bin / "ps"
    fake_ps.write_text(
        "#!/usr/bin/env bash\n"
        "cat <<'EOF'\n"
        "  999     1 bash\n"
        " 1000   999 /opt/homebrew/bin/node /Users/tokenclaw/.local/bin/claude\n"
        "EOF\n",
        encoding="utf-8",
    )
    fake_ps.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"

    result = subprocess.run(
        [
            str(DISPATCH),
            "--target",
            "palace:E",
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

    assert result.returncode == 73
    assert "live Claude/Codex descendant" in result.stderr
    assert "send-keys" not in tmux_log.read_text(encoding="utf-8")


def test_dispatch_tmux_target_bakes_pane_label_into_launch_env(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "resolve-pane" && "$2" == "--format" && "$3" == "physical" ]]; then echo "%44"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)

    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "show-options" ]]; then echo "palace:E"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"

    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--target",
            "palace:E",
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
    assert "TOKEN_API_DISPATCH_RESOLVED_PANE=<tmux-pane>" in result.stdout
    assert "TOKEN_API_PANE_LABEL=palace:E" in result.stdout


def test_dispatch_succeeds_on_liveness_even_when_registry_row_lags(tmp_path: Path) -> None:
    """A live pane whose instances row lags (or is stale) is SUCCESS, not failure.

    Regression for the fleet-wide exit-70 false-failure: the agent process is up
    and engaged in the pane, but the DB row has not bound live (here it is still
    ``stopped`` — the cold-start / codex-undercount reconciliation lag). The old
    code gated success on the row binding and fatal-ed exit 70 even though the
    launch plainly succeeded. The liveness-driven gate returns exit 0 and surfaces
    the lag as an ADVISORY ``registration slow`` warning — never a hard failure.
    """
    import sqlite3

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmux_log = tmp_path / "tmux.log"
    db = tmp_path / "agents.db"
    instance_id = "live-stamped-but-stopped"

    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE instances (id TEXT PRIMARY KEY, status TEXT, working_dir TEXT, tmux_pane TEXT, pane_label TEXT)"
        )
        conn.execute(
            "INSERT INTO instances VALUES (?, 'stopped', ?, '%44', 'palace:E')",
            (instance_id, str(ROOT)),
        )

    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "resolve-pane" && "$2" == "--format" && "$3" == "physical" ]]; then echo "%44"; exit 0; fi\n'
        # The pane is running a live agent (liveness wins over the stale row).
        'if [[ "$1" == "pane-live" ]]; then exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)

    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {tmux_log}\n'
        'if [[ "$1" == "display-message" && "${@: -1}" == "#{@INSTANCE_ID}" ]]; then\n'
        f'  printf "%s\\n" "{instance_id}"\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == "show-options" ]]; then echo "palace:E"; exit 0; fi\n'
        'if [[ "$1" == "display-message" ]]; then printf "bash||palace:E|999|\\n"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_DB"] = str(db)
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"
    env["DISPATCH_LAUNCH_OBSERVE_TIMEOUT"] = "1"

    result = subprocess.run(
        [
            str(DISPATCH),
            "--target",
            "palace:E",
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
    assert "dispatched claude to palace:E" in result.stdout
    # The row lag is advisory only — surfaced as a warning, never a failure.
    assert "registration slow" in result.stderr
    assert "instance row did not bind live" not in result.stderr
    assert "launch failed" not in result.stderr


def test_dispatch_succeeds_on_liveness_when_instance_never_registers(tmp_path: Path) -> None:
    """The codex singleton-undercount path: a live agent that never stamps a row.

    For codex the SessionStart pane stamp / DB row may never land at all. The old
    gate required the @INSTANCE_ID stamp and fatal-ed exit 70, breaking the
    stop-hook/completion contract for a launch that genuinely succeeded. With the
    liveness-driven gate, a live agent process in the pane is success (exit 0) even
    when nothing ever registers — the row is reconciled advisorily.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmux_log = tmp_path / "tmux.log"

    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "stack" && "$2" == "dispatch" ]]; then echo "mechanicus:2"; exit 0; fi\n'
        'if [[ "$1" == "resolve-pane" && "$2" == "--format" && "$3" == "physical" ]]; then echo "%77"; exit 0; fi\n'
        # A live agent is running even though nothing ever stamps the pane/row.
        'if [[ "$1" == "pane-live" ]]; then exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)

    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {tmux_log}\n'
        # @INSTANCE_ID is never stamped (undercount), but the pane survives.
        'if [[ "$1" == "display-message" && "$*" == *"@INSTANCE_ID"* ]]; then exit 0; fi\n'
        'if [[ "$1" == "display-message" ]]; then printf "%%77\\n"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"
    env["DISPATCH_LAUNCH_OBSERVE_TIMEOUT"] = "1"

    result = subprocess.run(
        [
            str(DISPATCH),
            "--engine",
            "codex",
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
    assert "dispatched codex to mechanicus:new" in result.stdout
    assert "launch failed" not in result.stderr
    tmux_calls = tmux_log.read_text(encoding="utf-8")
    assert "DISPATCH LAUNCH FAILED" not in tmux_calls


def test_dispatch_direct_with_prompt_does_not_warn_on_objective(tmp_path: Path) -> None:
    """The objective guard must not fire when a real brief is present."""
    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--direct",
            "--dir",
            str(ROOT),
            "--objective",
            "do the important thing",
            "--prompt",
            "here is the actual brief",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    assert result.returncode == 0, result.stderr
    assert "aspirant-only metadata" not in result.stderr


def test_dispatch_mints_fresh_wrapper_id_not_inheriting_dispatcher():
    """A dispatched worker must mint its OWN wrapper_id, never inherit the
    dispatching agent's.

    token-api SessionStart treats ``wrapper_id`` as unique per wrapper
    launch and adopts (re-keys) the most-recent row carrying it (routes/hooks.py
    branch 5). When ``dispatch`` runs from inside an agent it used to inherit that
    agent's ``TOKEN_API_WRAPPER_ID`` and inject it into the worker, so the
    worker supplanted the dispatcher's registry row and clobbered its (singleton)
    persona — the systemic fleet-wide singleton-decapitation channel. The id must
    be freshly minted, and the inherited value must also be scrubbed from the
    child env as defense in depth.
    """
    sentinel = "operator-wrapper-sentinel-0000"
    env = os.environ.copy()
    env["TOKEN_API_WRAPPER_ID"] = sentinel

    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--direct",
            "--engine",
            "claude",
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
        env=env,
    )

    assert result.returncode == 0, result.stderr
    # The worker's injected wrapper id must NOT be the dispatcher's.
    assert f"TOKEN_API_WRAPPER_ID={sentinel}" not in result.stdout, (
        "worker inherited the dispatcher's wrapper_id"
    )
    # A fresh id is still injected (the field is present, just not the sentinel).
    assert "TOKEN_API_WRAPPER_ID=" in result.stdout
    # Defense in depth: the inherited env value is scrubbed before the child runs.
    assert "-u TOKEN_API_WRAPPER_ID" in result.stdout, (
        "dispatch must scrub an inherited TOKEN_API_WRAPPER_ID from the child env"
    )


def _run_instance_api_server(payloads: dict[str, dict[str, Any]]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            prefix = "/api/instances/"
            if not self.path.startswith(prefix):
                self.send_response(404)
                self.end_headers()
                return
            iid = unquote(self.path[len(prefix) :])
            if iid not in payloads:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"detail":"Instance not found"}')
                return
            body = json.dumps(payloads[iid]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_dispatch_resume_by_id_uses_token_api_without_engine_or_dir(tmp_path: Path) -> None:
    iid = "resume-api-row"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    server = _run_instance_api_server(
        {
            iid: {
                "id": iid,
                "engine": "codex",
                "launcher": "codex",
                "target_working_dir": str(work_dir),
                "working_dir": "/stale/working-dir",
                "dispatch_session_doc_path": "Sessions/resume-api-row.md",
                "instance_type": "one_off",
                "zealotry": 5,
                "persona": {"slug": "mechanicus"},
            }
        }
    )
    try:
        env = os.environ.copy()
        env["TOKEN_API_URL"] = f"http://127.0.0.1:{server.server_port}"
        env["TOKEN_API_DB"] = str(tmp_path / "missing-agents.db")
        result = subprocess.run(
            [str(DISPATCH), "--dry-run", "--id", iid, "--pane", "mechanicus:new"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(ROOT),
            env=env,
            timeout=60,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 0, result.stderr
    assert "engine:          codex" in result.stdout
    assert f"dir:             {work_dir}" in result.stdout
    assert "resume_db:       true" in result.stdout
    assert f"dispatch --id {iid} --pane mechanicus:new" in result.stdout
    assert "--engine" not in result.stdout
    assert "--dir" not in result.stdout


def test_dispatch_resume_missing_id_requires_explicit_engine_and_dir(tmp_path: Path) -> None:
    server = _run_instance_api_server({})
    try:
        env = os.environ.copy()
        env["TOKEN_API_URL"] = f"http://127.0.0.1:{server.server_port}"
        env["TOKEN_API_DB"] = str(tmp_path / "missing-agents.db")
        result = subprocess.run(
            [str(DISPATCH), "--dry-run", "--id", "not-found", "--pane", "mechanicus:new"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(ROOT),
            env=env,
            timeout=60,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 66
    assert "pass explicit --engine and --dir" in result.stderr


def test_dispatch_resume_api_failure_is_not_treated_as_missing_row(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["TOKEN_API_URL"] = "http://127.0.0.1:1"
    env["TOKEN_API_DB"] = str(tmp_path / "missing-agents.db")

    result = subprocess.run(
        [
            str(DISPATCH),
            "--dry-run",
            "--id",
            "lookup-failure",
            "--engine",
            "claude",
            "--dir",
            str(ROOT),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
        timeout=60,
    )

    assert result.returncode == 70
    assert "metadata lookup failed via Token API" in result.stderr
    assert "pass explicit --engine and --dir" not in result.stderr


def test_dispatch_resume_api_rejects_mismatched_id(tmp_path: Path) -> None:
    requested_iid = "requested-session"
    server = _run_instance_api_server(
        {
            requested_iid: {
                "id": "different-session",
                "engine": "claude",
                "working_dir": str(ROOT),
            }
        }
    )
    try:
        env = os.environ.copy()
        env["TOKEN_API_URL"] = f"http://127.0.0.1:{server.server_port}"
        env["TOKEN_API_DB"] = str(tmp_path / "missing-agents.db")
        result = subprocess.run(
            [
                str(DISPATCH),
                "--dry-run",
                "--id",
                requested_iid,
                "--engine",
                "claude",
                "--dir",
                str(ROOT),
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(ROOT),
            env=env,
            timeout=60,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 70
    assert f"invalid payload for {requested_iid}" in result.stderr
    assert "metadata lookup failed via Token API" in result.stderr
    assert "pass explicit --engine and --dir" not in result.stderr


def test_dispatch_resume_api_rejects_incomplete_metadata(tmp_path: Path) -> None:
    requested_iid = "incomplete-session"
    server = _run_instance_api_server(
        {
            requested_iid: {
                "id": requested_iid,
                "engine": "claude",
            }
        }
    )
    try:
        env = os.environ.copy()
        env["TOKEN_API_URL"] = f"http://127.0.0.1:{server.server_port}"
        env["TOKEN_API_DB"] = str(tmp_path / "missing-agents.db")
        result = subprocess.run(
            [
                str(DISPATCH),
                "--dry-run",
                "--id",
                requested_iid,
                "--engine",
                "claude",
                "--dir",
                str(ROOT),
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(ROOT),
            env=env,
            timeout=60,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 70
    assert f"incomplete metadata for {requested_iid}" in result.stderr
    assert "metadata lookup failed via Token API" in result.stderr
    assert "pass explicit --engine and --dir" not in result.stderr


def test_dispatch_liveness_success_runs_naming_step_when_row_lags(tmp_path: Path) -> None:
    """The core repro: row lags but the pane is live → exit 0 AND naming runs.

    A false exit-70 before the naming step is exactly why agents land needs-name
    and untracked. With the liveness-driven gate the launch reaches exit 0, so the
    launched agent is neither reaped nor retried. Naming is no longer delivered
    by dispatch-derived prefixes; it happens only through the naming interview
    / instance-name boundary.
    """
    db = _persona_db(tmp_path, [("blood-angels", "Blood Angels", "astartes")])
    env = _persona_env(tmp_path, db)
    (Path(env["IMPERIUM"]) / "Imperium-ENV" / "Personas" / "Astartes.md").write_text(
        "## System Prompt\nGeneric rank behavior\n", encoding="utf-8"
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    rec = tmp_path / "tmux_calls.txt"
    ping_log = tmp_path / "tmuxctld-ping.log"

    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "stack" && "$2" == "dispatch" ]]; then echo "mechanicus:2"; exit 0; fi\n'
        'if [[ "$1" == "resolve-pane" && "$2" == "--format" && "$3" == "physical" ]]; then echo "%77"; exit 0; fi\n'
        # Live agent in the pane; the DB row never binds (cold-start lag).
        'if [[ "$1" == "pane-live" ]]; then exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)

    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        f'#!/usr/bin/env bash\nfor a in "$@"; do printf "%s\\0" "$a" >> {rec}; done\n'
        # @INSTANCE_ID never stamped → row never binds; pane stays present.
        'if [[ "$1" == "display-message" ]]; then exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)

    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"
    env["DISPATCH_LAUNCH_OBSERVE_TIMEOUT"] = "1"
    env["TMUXCTLD_PING_LOG"] = str(ping_log)

    result = subprocess.run(
        [
            str(DISPATCH),
            "--target",
            "mechanicus:new",
            "--persona",
            "blood-angels",
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
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "dispatched claude to mechanicus:new" in result.stdout
    assert "registration slow" in result.stderr
    assert "launch failed" not in result.stderr

    # Proof the success path reaches the pane without smuggling a dispatch-derived
    # naming prefix into the staged launch command.
    calls = [c for c in rec.read_bytes().decode("utf-8", "replace").split("\0") if c]
    assert "%77" in calls, f"physical pane option cleanup did not target %77; calls={calls}"
    ping_text = ping_log.read_text(encoding="utf-8", errors="replace")
    assert "POST /send-text pane=mechanicus:2 text=bash " in ping_text
    send_lines = "\n".join(line for line in ping_text.splitlines() if "POST /send-text" in line)
    assert "%77" not in send_lines
    staged = Path(_staged_command_from_tmuxctld_log(ping_log).split(" ", 1)[1]).read_text(
        encoding="utf-8", errors="replace"
    )
    assert "TOKEN_API_INSTANCE_NAME_PREFIX" not in staged, staged
    assert "On startup, name this instance" not in staged, staged


def test_dispatch_refuses_to_stack_second_agent_into_live_worktree(tmp_path: Path) -> None:
    """Resume/--no-worktree into a worktree with a LIVE agent must REFUSE.

    The corruption FG hit: a false exit-70 was retried via ``--no-worktree --dir``
    into the orphaned worktree and launched a SECOND live agent racing one git
    worktree. The duplicate-refusal guard detects an already-live agent rooted in
    the target dir by LIVENESS (``tmuxctl live-agents``, process tree + pane cwd —
    so a row-less / undercounted duplicate is still caught) and refuses, sending
    no launch.
    """
    work_dir = tmp_path / "wt-live"
    work_dir.mkdir()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmux_log = tmp_path / "tmux.log"

    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        "#!/usr/bin/env bash\n"
        # A live agent is already rooted in the target worktree.
        'if [[ "$1" == "live-agents" ]]; then\n'
        f'  printf "%s\\t%s\\t%s\\t%s\\n" "mechanicus:3" "%91" "codex" "{work_dir}"\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == "stack" && "$2" == "dispatch" ]]; then echo "mechanicus:2"; exit 0; fi\n'
        'if [[ "$1" == "resolve-pane" && "$2" == "--format" && "$3" == "physical" ]]; then echo "%77"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)

    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> {tmux_log}\nexit 0\n',
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"
    # Re-enable the guard the conftest disables for hermeticity.
    env["DISPATCH_WORKTREE_DUP_CHECK"] = "1"
    # The guard is scoped to the ~/worktrees tree; point that root at the tmp dir
    # holding work_dir so the dir counts as a worktree and the guard fires.
    # Include a trailing slash to pin normalization before the prefix check.
    env["IMPERIUM_WORKTREES_ROOT"] = f"{tmp_path}/"
    env["TMUXCTLD_PING_LIVE_AGENTS"] = f"mechanicus:3\t%91\tcodex\t{work_dir}"

    result = subprocess.run(
        [
            str(DISPATCH),
            "--target",
            "mechanicus:new",
            "--dir",
            str(work_dir),
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
        timeout=60,
    )

    assert result.returncode == 73, result.stderr
    assert "a live agent is already running in the target worktree" in result.stderr
    assert "do NOT stack a second" in result.stderr
    assert "%91" not in result.stderr
    assert "mechanicus:3\t<tmux-pane>\tcodex" in result.stderr
    assert "dispatched" not in result.stdout
    # No launch was staged: the guard refused before the send.
    tmux_calls = tmux_log.read_text(encoding="utf-8")
    assert "send-keys" not in tmux_calls


def test_dispatch_dup_guard_force_occupied_override_allows_launch(tmp_path: Path) -> None:
    """TOKEN_API_DISPATCH_FORCE_OCCUPIED=1 overrides the duplicate-refusal guard."""
    work_dir = tmp_path / "wt-live"
    work_dir.mkdir()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "live-agents" ]]; then\n'
        f'  printf "%s\\t%s\\t%s\\t%s\\n" "mechanicus:3" "%91" "codex" "{work_dir}"\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == "stack" && "$2" == "dispatch" ]]; then echo "mechanicus:2"; exit 0; fi\n'
        'if [[ "$1" == "resolve-pane" && "$2" == "--format" && "$3" == "physical" ]]; then echo "%77"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)

    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        '#!/usr/bin/env bash\nif [[ "$1" == "display-message" ]]; then printf "%%77\\n"; fi\nexit 0\n',
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"
    env["DISPATCH_WORKTREE_DUP_CHECK"] = "1"
    env["IMPERIUM_WORKTREES_ROOT"] = str(tmp_path)
    env["TOKEN_API_DISPATCH_FORCE_OCCUPIED"] = "1"
    # Liveness gate disabled so the test asserts only the guard override, fast.
    env["DISPATCH_LAUNCH_OBSERVE_TIMEOUT"] = "0"

    result = subprocess.run(
        [
            str(DISPATCH),
            "--target",
            "mechanicus:new",
            "--dir",
            str(work_dir),
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
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "a live agent is already running" not in result.stderr
    assert "dispatched claude to mechanicus:new" in result.stdout


def test_dispatch_dup_guard_scoped_to_worktrees_allows_shared_checkout(
    tmp_path: Path,
) -> None:
    """The dup-guard de-duplicates WITHIN ~/worktrees ONLY, never globally.

    Dispatching several agents into a shared non-worktree checkout (e.g. the
    Imperium-ENV vault) is legitimate: 1-branch-1-worktree-1-PR does not apply
    there. A live agent already rooted in such a dir must NOT refuse a second
    dispatch — the guard only fires for dirs under the worktrees root.
    """
    # work_dir is OUTSIDE the worktrees root → guard must not fire.
    work_dir = tmp_path / "vault" / "Imperium-ENV"
    work_dir.mkdir(parents=True)
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        "#!/usr/bin/env bash\n"
        # A live agent IS rooted in the target dir — but it is not a worktree, so
        # the guard should never even ask tmuxctl about it.
        'if [[ "$1" == "live-agents" ]]; then\n'
        f'  printf "%s\\t%s\\t%s\\t%s\\n" "mechanicus:3" "%91" "codex" "{work_dir}"\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == "stack" && "$2" == "dispatch" ]]; then echo "mechanicus:2"; exit 0; fi\n'
        'if [[ "$1" == "resolve-pane" && "$2" == "--format" && "$3" == "physical" ]]; then echo "%77"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)

    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        '#!/usr/bin/env bash\nif [[ "$1" == "display-message" ]]; then printf "%%77\\n"; fi\nexit 0\n',
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"
    env["DISPATCH_WORKTREE_DUP_CHECK"] = "1"
    env["IMPERIUM_WORKTREES_ROOT"] = str(wt_root)
    # Liveness gate disabled so the test asserts only the guard scope, fast.
    env["DISPATCH_LAUNCH_OBSERVE_TIMEOUT"] = "0"

    result = subprocess.run(
        [
            str(DISPATCH),
            "--target",
            "mechanicus:new",
            "--dir",
            str(work_dir),
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
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "a live agent is already running" not in result.stderr
    assert "dispatched claude to mechanicus:new" in result.stdout


# ---------------------------------------------------------------------------
# Occupancy guard: pane_has_active_agent_process self-referential false-positive
#
# The resume path types `dispatch ... --engine codex ...` into the *target pane's
# own shell* (executor.send_keys), so the running dispatch — argv carrying the
# literal "codex" — is a child of pane_pid. The occupancy guard walks pane_pid's
# subtree; the old `claude|codex` substring needle matched dispatch's own
# `--engine codex` argument, so dispatch refused itself on every resume. These
# tests use live process fixtures (per shop convention) to pin the fix:
#   - a dispatch self-invocation with --engine codex must NOT be counted
#   - a genuine live claude/codex *program* in the subtree must still be counted
#   - a real agent that is part of dispatch's own (self) lineage is excluded
# ---------------------------------------------------------------------------

_GUARD_FN = "pane_has_active_agent_process"


def _extract_bash_function(name: str) -> str:
    text = DISPATCH.read_text(encoding="utf-8")
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


# Distinct sentinel exit codes so a harness/shell failure (unbound var, missing
# fixture, syntax error → exit 1/2/127/…) can never be mistaken for a genuine
# guard "clear" verdict. The guard's own 0/1 is mapped onto these before exit.
_GUARD_FOUND = 10  # guard counted a live agent (dispatch would refuse)
_GUARD_CLEAR = 11  # guard found nothing (dispatch would proceed)


def _sleep_bin() -> str:
    """Absolute path to the real `sleep` binary, portable across mac/Linux CI.

    The genuine-agent fixtures symlink an agent-named launcher onto this so the
    process actually runs; a hardcoded /bin/sleep is absent on some Linux runners
    (sleep lives at /usr/bin/sleep), which silently kills the fixture.
    """
    found = shutil.which("sleep")
    assert found, "no `sleep` on PATH"
    return found


def _run_guard_scenario(scenario: str, tmp_path: Path) -> int:
    """Run the live occupancy guard against a real process tree.

    `scenario` is bash that must spawn the fixture processes, append their pids to
    the ``PIDS`` array, and set ``ROOT`` (the pane_pid subtree to scan) and ``SELF``
    (dispatch's own pid, whose lineage is excluded). Returns the guard's exit
    status: 0 = a live agent was found (dispatch would refuse), 1 = clear.
    """
    harness = "\n".join(
        [
            "set -u",
            _extract_bash_function(_GUARD_FN),
            "PIDS=()",
            scenario,
            "sleep 0.5",
            f'if {_GUARD_FN} "$ROOT" "$SELF"; then rc={_GUARD_FOUND}; else rc={_GUARD_CLEAR}; fi',
            'for p in "${PIDS[@]}"; do pkill -P "$p" 2>/dev/null || true; '
            'kill "$p" 2>/dev/null || true; done',
            "exit $rc",
        ]
    )
    script = tmp_path / "guard_harness.sh"
    script.write_text(harness, encoding="utf-8")
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, check=False)
    assert proc.returncode in (_GUARD_FOUND, _GUARD_CLEAR), (
        f"harness failed (rc={proc.returncode}): {proc.stderr}"
    )
    return 0 if proc.returncode == _GUARD_FOUND else 1


def test_guard_does_not_self_refuse_dispatch_with_engine_codex_arg(tmp_path: Path) -> None:
    # Bug repro: dispatch typed into the target pane's shell is a child of pane_pid
    # and its argv carries "--engine codex". It must not be counted as a live agent.
    scenario = (
        '( exec -a "bash /x/cli-tools/bin/dispatch --id u --pane palace:5 '
        '--engine codex --dir /tmp/wt" /bin/sleep 30 ) &\n'
        "child=$!\n"
        'PIDS+=("$child")\n'
        "ROOT=$$\n"  # the harness shell stands in for the pane shell (pane_pid)
        "SELF=$child\n"  # dispatch passes its own pid; its lineage is excluded
    )
    assert _run_guard_scenario(scenario, tmp_path) == 1


def test_guard_does_not_self_refuse_dispatch_with_engine_claude_arg(tmp_path: Path) -> None:
    # Same bug, claude engine: "--engine claude" as an argument is not a live agent.
    scenario = (
        '( exec -a "bash /x/cli-tools/bin/dispatch --id u --pane palace:5 '
        '--engine claude --dir /tmp/wt" /bin/sleep 30 ) &\n'
        "child=$!\n"
        'PIDS+=("$child")\n'
        "ROOT=$$\n"
        "SELF=$child\n"
    )
    assert _run_guard_scenario(scenario, tmp_path) == 1


def test_guard_refuses_genuine_live_codex_descendant(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "codex").symlink_to(_sleep_bin())
    scenario = (
        f'"{bindir}/codex" 30 &\n'
        "agent=$!\n"
        'PIDS+=("$agent")\n'
        "sleep 30 &\n"  # dispatch's own pid is a sibling of a pre-existing agent,
        "self=$!\n"  # never its ancestor, so self-exclusion must not apply here
        'PIDS+=("$self")\n'
        "ROOT=$$\n"
        "SELF=$self\n"
    )
    assert _run_guard_scenario(scenario, tmp_path) == 0


def test_guard_refuses_genuine_live_claude_real_descendant(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "claude.token-os-real").symlink_to(_sleep_bin())
    scenario = (
        f'"{bindir}/claude.token-os-real" 30 &\n'
        "agent=$!\n"
        'PIDS+=("$agent")\n'
        "sleep 30 &\n"  # sibling self pid (dispatch's own), not an ancestor
        "self=$!\n"
        'PIDS+=("$self")\n'
        "ROOT=$$\n"
        "SELF=$self\n"
    )
    assert _run_guard_scenario(scenario, tmp_path) == 0


def test_guard_excludes_real_agent_in_self_lineage(tmp_path: Path) -> None:
    # A genuine codex program, but spawned as a descendant of the dispatch process
    # (self) — e.g. dispatch's staged child tree. It must never count against self.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "codex").symlink_to(_sleep_bin())
    parent = tmp_path / "self_parent.sh"
    parent.write_text(f'#!/bin/bash\n"{bindir}/codex" 30 &\nsleep 30\n', encoding="utf-8")
    parent.chmod(0o755)
    scenario = (
        f'bash "{parent}" &\n'
        "self=$!\n"
        'PIDS+=("$self")\n'
        "ROOT=$$\n"
        "SELF=$self\n"  # the codex agent is a child of $self -> excluded by lineage
    )
    assert _run_guard_scenario(scenario, tmp_path) == 1


def _write_codex_dispatch_probe_fakes(
    tmp_path: Path, *, live: bool, send_ok: bool = True
) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ping_log = tmp_path / "tmuxctld-ping.log"

    send_response = (
        '{"ok":true,"result":{"delivery":"failed",'
        '"advisory":"submit did not clear composer",'
        '"capture_excerpt":"draft still visible"}}'
        if send_ok
        else '{"ok":false,"error":{"message":"send suppressed before bytes"}}'
    )
    live_json = "true" if live else "false"
    fake_ping = fake_bin / "tmuxctld-ping"
    fake_ping.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf '%s\\n' \"$*\" >> {ping_log}\n"
        'if [[ "${TMUXCTLD_PING_PRINT_RESPONSE:-}" != "1" ]]; then exit 0; fi\n'
        'method="${1:-}"; path="${2:-}"; shift 2 || true\n'
        'case "$method $path" in\n'
        '  "GET /freelist") printf \'%s\' \'[{"pane_id":"palace:N","pane_role":"palace:N","window_name":"palace"}]\' | python3 -c \'import json,sys; print(json.dumps({"ok": True, "result": json.loads(sys.stdin.read())}))\' ;;\n'
        '  "GET /resolve-pane"|"POST /resolve-pane") printf \'{"ok":true,"result":"%%88"}\' ;;\n'
        f"  \"POST /send-text\") printf '{send_response}' ;;\n"
        f'  "POST /pane-live") printf \'{{"ok":true,"result":{{"live":{live_json}}}}}\' ;;\n'
        '  "POST /live-agents") printf \'{"ok":true,"result":""}\' ;;\n'
        '  *) printf \'{"ok":true,"result":""}\' ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    fake_ping.chmod(0o755)

    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == "display-message" ]]; then printf \'bash||palace:N|999|\\n\'; exit 0; fi\n'
        'if [[ "${1:-}" == "show-options" ]]; then printf \'palace:N\\n\'; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)
    return fake_bin, ping_log


def test_codex_dispatch_argv_launch_uses_liveness_not_composer_failure(tmp_path: Path) -> None:
    fake_bin, ping_log = _write_codex_dispatch_probe_fakes(tmp_path, live=True)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"
    env["DISPATCH_WORKTREE_DUP_CHECK"] = "0"

    prompt = "argv delivery probe marker"
    result = subprocess.run(
        [
            str(DISPATCH),
            "--engine",
            "codex",
            "--target",
            "palace:new",
            "--dir",
            str(ROOT),
            "--no-worktree",
            "--no-gt",
            "--prompt",
            prompt,
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "codex launch delivery will be verified by argv launch + live process" in result.stderr
    staged = Path(_staged_command_from_tmuxctld_log(ping_log).split(" ", 1)[1]).read_text(
        encoding="utf-8", errors="replace"
    )
    assert "agent-wrapper.sh' codex" in staged or "agent-wrapper.sh codex" in staged
    assert "argv\\ delivery\\ probe\\ marker" in staged


def test_codex_dispatch_argv_launch_still_fails_when_process_never_starts(tmp_path: Path) -> None:
    fake_bin, _ping_log = _write_codex_dispatch_probe_fakes(tmp_path, live=False)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"
    env["DISPATCH_WORKTREE_DUP_CHECK"] = "0"
    env["DISPATCH_LAUNCH_OBSERVE_TIMEOUT"] = "1"

    result = subprocess.run(
        [
            str(DISPATCH),
            "--engine",
            "codex",
            "--target",
            "palace:new",
            "--dir",
            str(ROOT),
            "--no-worktree",
            "--no-gt",
            "--prompt",
            "argv negative probe",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
    )

    assert result.returncode != 0
    assert "no live codex agent appeared" in result.stderr


def test_codex_dispatch_still_fails_when_daemon_reports_no_bytes_written(tmp_path: Path) -> None:
    fake_bin, _ping_log = _write_codex_dispatch_probe_fakes(tmp_path, live=True, send_ok=False)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_PARENT_INSTANCE_ID"] = "test-parent"
    env["TOKEN_API_INTERNAL_DISPATCH"] = "1"
    env["DISPATCH_WORKTREE_DUP_CHECK"] = "0"

    result = subprocess.run(
        [
            str(DISPATCH),
            "--engine",
            "codex",
            "--target",
            "palace:new",
            "--dir",
            str(ROOT),
            "--no-worktree",
            "--no-gt",
            "--prompt",
            "argv send failure probe",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
    )

    assert result.returncode != 0
    assert "send suppressed before bytes" in result.stderr
