import argparse
import json
import os
from pathlib import Path

import pytest

from cli_tools.subagents import main as subagent_main


def _build_paths(tmp_path: Path) -> subagent_main.CodexPaths:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    logs_dir = repo_root / "logs" / "agents"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    agent_wrapper = scripts_dir / "agent-wrapper.sh"
    agent_wrapper.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    return subagent_main.CodexPaths(
        invocation_root=repo_root,
        logs_dir=logs_dir,
        counter_path=logs_dir / ".codex-agent-counter",
        launches_path=logs_dir / ".launches.json",
        agent_wrapper_path=agent_wrapper,
    )


def _create_codex_stub(bin_dir: Path, filename: str = "codex") -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / filename
    stub.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    os.chmod(stub, 0o755)
    return stub


def test_write_prompt_file_creates_parent_dirs(tmp_path: Path) -> None:
    prompt_path = tmp_path / "logs" / "agents" / "prompt-1.txt"
    subagent_main._write_prompt_file(prompt_path, "hello")
    assert prompt_path.read_text(encoding="utf-8") == "hello"


def test_get_next_codex_agent_id_increments(tmp_path: Path) -> None:
    paths = _build_paths(tmp_path)

    first = subagent_main._get_next_codex_agent_id(paths)
    second = subagent_main._get_next_codex_agent_id(paths)

    assert first == 1
    assert second == 2
    assert paths.counter_path.read_text(encoding="utf-8").strip() == "2"


def test_handle_codex_launch_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _build_paths(tmp_path)
    launches: dict[str, object] = {}

    class DummyProcess:
        pass

    def fake_launch(command, cwd=None, title=None, env=None, skip_wrapper=False):
        log_path = Path(command[4])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("started\n", encoding="utf-8")
        launches["command"] = command
        launches["cwd"] = cwd
        launches["title"] = title
        launches["skip_wrapper"] = skip_wrapper
        return DummyProcess()

    monkeypatch.setattr(subagent_main, "launch_in_new_terminal", fake_launch)
    monkeypatch.setattr(subagent_main, "detect_terminal_emulator", lambda: "gnome-terminal")
    monkeypatch.setattr(
        subagent_main.shutil, "which", lambda cmd: "/usr/bin/codex" if cmd == "codex" else None
    )
    monkeypatch.setattr(subagent_main.time, "sleep", lambda *_: None)

    args = argparse.Namespace(prompt_file=None, codex_command=["echo", "hello"])

    subagent_main._handle_codex(args, paths)

    out = capsys.readouterr().out
    assert "Launching Codex agent 1" in out
    assert "Codex agent 1 running" in out

    command = launches["command"]
    assert command[0] == "bash"
    assert command[2] == "codex"
    assert command[3] == "1"
    assert Path(command[1]) == paths.agent_wrapper_path
    assert launches["cwd"] == paths.invocation_root
    assert launches["title"] == "Codex Agent 1"
    assert launches["skip_wrapper"] is True

    payload = json.loads(paths.launches_path.read_text(encoding="utf-8"))
    attempts = payload["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["status"] == "launched"


def test_handle_codex_uses_packaged_env_when_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _build_paths(tmp_path)
    packaged_env = paths.invocation_root / ".packaged-venv" / "bin"
    codex_stub = _create_codex_stub(packaged_env)
    launches: dict[str, object] = {}

    class DummyProcess:
        pass

    def fake_launch(command, cwd=None, title=None, env=None, skip_wrapper=False):
        log_path = Path(command[4])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("started\n", encoding="utf-8")
        launches["command"] = command
        return DummyProcess()

    monkeypatch.setattr(subagent_main, "launch_in_new_terminal", fake_launch)
    monkeypatch.setattr(subagent_main, "detect_terminal_emulator", lambda: "gnome-terminal")
    monkeypatch.setattr(subagent_main.time, "sleep", lambda *_: None)
    monkeypatch.setattr(subagent_main.shutil, "which", lambda cmd: None)
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", ".packaged-venv")

    args = argparse.Namespace(prompt_file=None, codex_command=["echo", "hello"])
    subagent_main._handle_codex(args, paths)

    command = launches["command"]
    assert command[5] == str(codex_stub)


def test_handle_codex_falls_back_to_repo_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _build_paths(tmp_path)
    codex_stub = _create_codex_stub(paths.invocation_root / ".venv" / "bin")
    launches: dict[str, object] = {}

    class DummyProcess:
        pass

    def fake_launch(command, cwd=None, title=None, env=None, skip_wrapper=False):
        log_path = Path(command[4])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("started\n", encoding="utf-8")
        launches["command"] = command
        return DummyProcess()

    monkeypatch.setattr(subagent_main, "launch_in_new_terminal", fake_launch)
    monkeypatch.setattr(subagent_main, "detect_terminal_emulator", lambda: "gnome-terminal")
    monkeypatch.setattr(subagent_main.time, "sleep", lambda *_: None)
    monkeypatch.setattr(subagent_main.shutil, "which", lambda cmd: None)
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)

    args = argparse.Namespace(prompt_file=None, codex_command=["echo", "hello"])
    subagent_main._handle_codex(args, paths)

    command = launches["command"]
    assert command[5] == str(codex_stub)


def test_handle_codex_launch_retries_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _build_paths(tmp_path)

    monkeypatch.setattr(subagent_main, "launch_in_new_terminal", lambda *_, **__: None)
    monkeypatch.setattr(subagent_main, "detect_terminal_emulator", lambda: "gnome-terminal")
    monkeypatch.setattr(
        subagent_main.shutil, "which", lambda cmd: "/usr/bin/codex" if cmd == "codex" else None
    )
    monkeypatch.setattr(subagent_main.time, "sleep", lambda *_: None)

    args = argparse.Namespace(prompt_file=None, codex_command=["echo", "fail"])

    with pytest.raises(SystemExit) as excinfo:
        subagent_main._handle_codex(args, paths)

    assert "Codex launch failed after 3 attempts" in str(excinfo.value)

    payload = json.loads(paths.launches_path.read_text(encoding="utf-8"))
    attempts = payload["attempts"]
    assert len(attempts) == 3
    assert all(entry["status"] == "failed" for entry in attempts)
