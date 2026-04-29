#!/usr/bin/env python3
"""Launch Codex sub-agents in dedicated terminals with repo-scoped logging."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .terminal_launcher import detect_terminal_emulator, launch_in_new_terminal

CLI_TOOLS_ROOT = Path(__file__).resolve().parents[3]
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_EXECUTABLE_NAMES = ("codex", "codex.exe", "codex.bat", "codex.cmd")
_AVAILABLE_TYPES = ("tool-creator", "implementor")
_DEFAULT_ENV_DIRS = (
    ".venv-packaged",
    "packaged-venv",
    ".venv",
    "venv",
    ".env",
    "env",
)

try:  # pragma: no cover - Windows fallback
    import fcntl  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]


@dataclass(frozen=True)
class CodexPaths:
    """Filesystem locations for Codex launches."""

    invocation_root: Path
    logs_dir: Path
    counter_path: Path
    launches_path: Path
    wrapper_path: Path

    @classmethod
    def build(cls) -> CodexPaths:
        invocation_root = _detect_invocation_root()
        logs_dir = invocation_root / "logs" / "agents"
        return cls(
            invocation_root=invocation_root,
            logs_dir=logs_dir,
            counter_path=logs_dir / ".codex-agent-counter",
            launches_path=logs_dir / ".launches.json",
            wrapper_path=CLI_TOOLS_ROOT / "scripts" / "codex-wrapper.sh",
        )


@contextlib.contextmanager
def _locked_counter_file(path: Path):
    """Yield a locked file handle for counter updates."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o664)
    with os.fdopen(fd, "r+", encoding="utf-8") as handle:
        if fcntl is not None:  # pragma: no branch
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield handle
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _locked_json_file(path: Path):
    """Yield a locked file handle for JSON metadata."""

    with _locked_counter_file(path) as handle:
        yield handle


def _deduplicate_paths(raw_paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in raw_paths:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        ordered.append(resolved)
        seen.add(resolved)
    return ordered


def _candidate_env_roots(invocation_root: Path) -> list[Path]:
    """Return possible virtual environment directories to probe for Codex."""

    candidates: list[Path] = []
    override = os.environ.get("CLI_TOOLS_CODEX_VENV")
    if override:
        candidates.append(Path(override))

    uv_env = os.environ.get("UV_PROJECT_ENVIRONMENT")
    if uv_env:
        env_path = Path(uv_env)
        if not env_path.is_absolute():
            env_path = invocation_root / uv_env
        candidates.append(env_path)

    for var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        value = os.environ.get(var)
        if value:
            candidates.append(Path(value))

    for name in _DEFAULT_ENV_DIRS:
        candidate = invocation_root / name
        if candidate.exists():
            candidates.append(candidate)

    return _deduplicate_paths(candidates)


def _find_codex_in_env_root(env_root: Path) -> Path | None:
    """Look for a Codex executable within a virtual environment directory."""

    for subdir in ("bin", "Scripts"):
        candidate_dir = env_root / subdir
        if not candidate_dir.exists():
            continue
        for executable in _EXECUTABLE_NAMES:
            candidate = candidate_dir / executable
            if candidate.exists():
                return candidate
    return None


def _resolve_codex_executable(paths: CodexPaths) -> str | None:
    """Return a Codex executable path from candidate environments or PATH."""

    for env_root in _candidate_env_roots(paths.invocation_root):
        codex_path = _find_codex_in_env_root(env_root)
        if codex_path is not None:
            return str(codex_path)

    return shutil.which("codex")


def _detect_invocation_root() -> Path:
    """Return the caller's repository root (git) or the working directory."""

    env_dir = os.environ.get("CLI_TOOLS_CALLER_DIR")
    start_dir = Path(env_dir).resolve() if env_dir else Path.cwd().resolve()
    git_root = _probe_git_root(start_dir)
    return git_root if git_root is not None else start_dir


def _probe_git_root(start_dir: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start_dir,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None

    if result.returncode == 0:
        candidate = Path(result.stdout.strip())
        if candidate.exists():
            return candidate.resolve()
    return None


def _get_next_codex_agent_id(paths: CodexPaths) -> int:
    """Return the next sequential Codex agent ID."""

    with _locked_counter_file(paths.counter_path) as handle:
        handle.seek(0)
        raw_value = handle.read().strip()
        current = int(raw_value) if raw_value else 0
        next_id = current + 1
        handle.seek(0)
        handle.truncate()
        handle.write(str(next_id))
        handle.flush()
        os.fsync(handle.fileno())
        return next_id


def _format_codex_agent_id(agent_id: int) -> str:
    return f"{agent_id}"


def _write_codex_launch_status(
    paths: CodexPaths,
    agent_id: str,
    attempt: int,
    status: str,
    command: str,
    log_path: Path,
    message: str | None = None,
) -> None:
    """Persist Codex launch metadata for observability."""

    timestamp = datetime.now(timezone.utc).isoformat()
    paths.logs_dir.mkdir(parents=True, exist_ok=True)

    with _locked_json_file(paths.launches_path) as handle:
        handle.seek(0)
        raw_value = handle.read().strip()
        try:
            data: dict[str, Any] = json.loads(raw_value) if raw_value else {}
        except json.JSONDecodeError:
            data = {}

        attempts: list[dict[str, Any]] = data.setdefault("attempts", [])
        entry = next(
            (
                existing
                for existing in attempts
                if existing.get("agent_id") == agent_id and existing.get("attempt") == attempt
            ),
            None,
        )

        if entry is None:
            entry = {
                "agent_id": agent_id,
                "attempt": attempt,
                "command": command,
                "log_path": str(log_path),
                "status": status,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            if message is not None:
                entry["message"] = message
            attempts.append(entry)
        else:
            entry["status"] = status
            entry["command"] = command
            entry["log_path"] = str(log_path)
            entry.setdefault("created_at", timestamp)
            entry["updated_at"] = timestamp
            if message is not None:
                entry["message"] = message
            elif "message" in entry and status == "launched":
                entry.pop("message", None)

        handle.seek(0)
        handle.truncate()
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _validate_codex_launch(paths: CodexPaths, command_parts: Sequence[str]) -> str:
    """Validate environment preconditions and return codex executable."""

    codex_path = _resolve_codex_executable(paths)
    if codex_path is None:
        hints = [
            "Activate your project virtual environment (e.g., `source .venv/bin/activate`).",
            "Run `uv sync` so the packaged .venv exists (respects UV_PROJECT_ENVIRONMENT).",
            "Set CLI_TOOLS_CODEX_VENV to the virtual environment that contains Codex.",
        ]
        raise SystemExit(
            "codex command not found in the packaged/project virtual environments or PATH.\n"
            + "\n".join(f"- {hint}" for hint in hints)
        )

    emulator = detect_terminal_emulator()
    if emulator is None:
        raise SystemExit(
            "No compatible terminal emulator detected for Codex launches. "
            "Install a supported emulator (e.g., gnome-terminal or Windows Terminal)."
        )

    if not paths.wrapper_path.exists():
        raise SystemExit(
            f"codex-wrapper.sh not found: {paths.wrapper_path}. Ensure cli-tools/scripts/codex-wrapper.sh exists."
        )

    return codex_path


def _relative_display(path: Path, root: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError:
        return path


def _handle_codex(args: argparse.Namespace, paths: CodexPaths) -> None:
    # Handle --list-types
    if getattr(args, "list_types", False):
        print("Available agent types:")
        for agent_type in _AVAILABLE_TYPES:
            print(f"  {agent_type}")
        return

    prompt_file = getattr(args, "prompt_file", None)
    command_parts = getattr(args, "codex_command", []) or []
    agent_type = getattr(args, "type", None)

    if not prompt_file and not command_parts:
        raise SystemExit("No command provided. Use 'subagent <prompt>' or '--prompt-file <file>'.")

    source_file_path: Path | None = None
    prompt_content = ""

    if prompt_file:
        source_file_path = Path(prompt_file).expanduser().resolve()
        if not source_file_path.exists():
            raise SystemExit(f"Prompt file not found: {source_file_path}")
        prompt_content = source_file_path.read_text(encoding="utf-8")
    elif command_parts and command_parts[0].startswith("@") and len(command_parts[0]) > 1:
        file_name = command_parts[0][1:]
        source_file_path = Path(file_name).expanduser().resolve()
        if not source_file_path.exists():
            raise SystemExit(f"Prompt file not found: {source_file_path}")
        prompt_content = source_file_path.read_text(encoding="utf-8")
    else:
        prompt_content = " ".join(command_parts)

    # Prepend type-specific system prompt if --type is specified
    if agent_type:
        type_prompt = _load_type_prompt(agent_type)
        if type_prompt is None:
            raise SystemExit(f"System prompt for type '{agent_type}' not found.")
        prompt_content = type_prompt + "\n\n" + prompt_content
        print(f"🎯 Using agent type: {agent_type}")

    codex_path = _validate_codex_launch(paths, command_parts)
    agent_id_value = _get_next_codex_agent_id(paths)
    agent_id = _format_codex_agent_id(agent_id_value)

    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    temp_prompt_file = paths.logs_dir / f"prompt-{agent_id}.txt"
    _write_prompt_file(temp_prompt_file, prompt_content)

    auto_file_threshold = 8192  # bytes
    use_file = (
        source_file_path is not None
        or len(prompt_content.encode("utf-8")) > auto_file_threshold
        or "\n" in prompt_content
    )

    if use_file and source_file_path:
        try:
            relative_source = source_file_path.relative_to(paths.invocation_root)
        except ValueError:
            relative_source = source_file_path
        print(f"📄 Read prompt from {relative_source}, using temporary file")
    elif use_file:
        print("📄 Using temporary prompt file for long/complex prompt")

    log_path = paths.logs_dir / f"codex-agent-{agent_id}.log"
    relative_log = _relative_display(log_path, paths.invocation_root)

    print(f"🤖 Launching Codex agent {agent_id}...")
    print(f"📝 Logs: {relative_log}")

    prompt_arg = f"@FILE:{temp_prompt_file}"
    wrapper_command = [
        "bash",
        str(paths.wrapper_path),
        agent_id,
        str(log_path),
        codex_path,
        prompt_arg,
    ]

    if source_file_path:
        display_str = f"[FILE: {source_file_path.name}]"
    else:
        trunc = prompt_content[:200] + "..." if len(prompt_content) > 200 else prompt_content
        display_str = trunc or "[TEMP]"

    max_attempts = 3
    backoff_seconds = 0.5
    last_error: str | None = None

    for attempt in range(1, max_attempts + 1):
        attempt_error: str | None = None
        if not temp_prompt_file.exists():
            _write_prompt_file(temp_prompt_file, prompt_content)
            if not temp_prompt_file.exists():
                raise SystemExit(f"Prompt file could not be prepared: {temp_prompt_file}")
        _write_codex_launch_status(paths, agent_id, attempt, "pending", display_str, log_path)

        try:
            process = launch_in_new_terminal(
                wrapper_command,
                cwd=paths.invocation_root,
                title=f"Codex Agent {agent_id}",
                skip_wrapper=True,
            )
            if process is None:
                raise RuntimeError("No compatible terminal emulator detected.")
        except Exception as exc:  # pragma: no cover - platform dependent
            attempt_error = str(exc)
            last_error = attempt_error
            _write_codex_launch_status(
                paths, agent_id, attempt, "failed", display_str, log_path, attempt_error
            )
            print(f"⚠️  Codex launch attempt {attempt} failed: {attempt_error}", file=sys.stderr)
        else:
            if not log_path.exists():
                time.sleep(0.3)
                if not log_path.exists():
                    attempt_error = "Log file not created - wrapper script may not have started"
            elif log_path.stat().st_size == 0:
                time.sleep(0.3)
                if log_path.stat().st_size == 0:
                    attempt_error = "Log file empty - wrapper script may not have started"

            if attempt_error is None:
                _write_codex_launch_status(
                    paths, agent_id, attempt, "launched", display_str, log_path
                )
                print(f"🚀 Codex agent {agent_id} running (attempt {attempt}).")
                try:
                    temp_prompt_file.unlink(missing_ok=True)
                except OSError:
                    pass
                return

            last_error = attempt_error
            _write_codex_launch_status(
                paths, agent_id, attempt, "failed", display_str, log_path, attempt_error
            )
            print(f"⚠️  Codex launch attempt {attempt} failed: {attempt_error}", file=sys.stderr)

        if attempt == max_attempts:
            break
        print(f"⏱️  Retrying in {backoff_seconds:.1f}s...", file=sys.stderr)
        time.sleep(backoff_seconds)
        backoff_seconds = min(backoff_seconds * 2, 4.0)

    try:
        temp_prompt_file.unlink(missing_ok=True)
    except OSError:
        pass

    raise SystemExit(
        f"Codex launch failed after {max_attempts} attempts: {last_error or 'Unknown error'}"
    )


def _write_prompt_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(contents)
        handle.flush()
        os.fsync(handle.fileno())


def _load_type_prompt(agent_type: str) -> str | None:
    """Load the system prompt for a given agent type."""
    # Convert kebab-case to snake_case for filename
    filename = agent_type.replace("-", "_") + ".md"
    prompt_path = PROMPTS_DIR / filename
    if not prompt_path.exists():
        return None
    return prompt_path.read_text(encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subagent",
        description="Launch Codex in a new terminal window with repo-scoped logging.",
    )
    parser.add_argument(
        "codex_command",
        nargs="*",
        metavar="COMMAND",
        help="Prompt to send to Codex. Use quotes for multi-word prompts or '@file' / --prompt-file for files.",
    )
    parser.add_argument(
        "--prompt-file",
        type=str,
        metavar="FILE",
        help="Read the prompt from a file instead of command line arguments. Overrides inline commands.",
    )
    parser.add_argument(
        "--type",
        "-t",
        type=str,
        choices=_AVAILABLE_TYPES,
        metavar="TYPE",
        help=f"Agent type with specialized system prompt. Available: {', '.join(_AVAILABLE_TYPES)}",
    )
    parser.add_argument(
        "--list-types",
        action="store_true",
        help="List available agent types and exit.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    paths = CodexPaths.build()
    _handle_codex(args, paths)


if __name__ == "__main__":
    main()
