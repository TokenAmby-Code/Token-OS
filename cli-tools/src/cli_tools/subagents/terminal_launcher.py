"""Platform-aware terminal launcher for Codex sub-agents.

Ported from ProcurementAgentAI/cli with minimal adjustments so the
implementation can be reused across repositories via cli-tools.
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Mapping, Sequence


def detect_terminal_emulator() -> str | None:
    """Return the first available terminal emulator for the host platform."""

    system = platform.system()

    if system == "Linux":
        for candidate in ("gnome-terminal", "xterm"):
            if shutil.which(candidate):
                return candidate
        if _is_wsl():
            if shutil.which("powershell.exe") and shutil.which("wt.exe"):
                return "wsl-ps"
            if shutil.which("cmd.exe") and shutil.which("wsl.exe"):
                return "wsl-cmd"
        return None

    if system == "Darwin":
        if shutil.which("osascript"):
            return "osascript"
        return None

    return None


def launch_in_new_terminal(
    command: Sequence[str],
    cwd: Path | str | None = None,
    title: str | None = None,
    env: Mapping[str, str] | None = None,
    skip_wrapper: bool = False,
) -> subprocess.Popen[bytes] | None:
    """Launch ``command`` in a new terminal window."""

    if not command:
        raise ValueError("launch_in_new_terminal requires a non-empty command.")

    emulator = detect_terminal_emulator()
    if emulator is None:
        return None

    working_dir = Path(cwd) if cwd is not None else _default_project_root()
    working_dir = working_dir.resolve()
    shell_command = _build_shell_command(command, working_dir)

    if emulator == "gnome-terminal":
        proc_args = ["gnome-terminal"]
        if title:
            proc_args.extend(["--title", title])
        proc_args.extend(["--", "bash", "-lc", shell_command])
        return subprocess.Popen(proc_args, cwd=working_dir, env=dict(env) if env else None)

    if emulator == "xterm":
        proc_args = ["xterm", "-hold"]
        if title:
            proc_args.extend(["-T", title])
        proc_args.extend(["-e", "bash", "-lc", shell_command])
        return subprocess.Popen(proc_args, cwd=working_dir, env=dict(env) if env else None)

    if emulator == "osascript":
        script = _build_osascript(shell_command, title)
        proc_args = ["osascript", "-e", script]
        return subprocess.Popen(proc_args, cwd=working_dir, env=dict(env) if env else None)

    windows_cwd = _windows_process_cwd() if emulator in {"wsl-ps", "wsl-cmd"} else None

    if emulator == "wsl-ps":
        if skip_wrapper:
            wsl_path = _normalize_wsl_unc_path(str(working_dir))
            quoted_cwd = shlex.quote(wsl_path)
            command_str = f"cd {quoted_cwd} && {shlex.join(list(command))}"
            wt_title = title or "Codex Agent"
            proc_args = [
                "wt.exe",
                "-p",
                "Ubuntu",
                "--title",
                wt_title,
                "bash",
                "-lc",
                command_str,
            ]
            return subprocess.Popen(
                proc_args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=dict(env) if env else None,
                cwd=windows_cwd,
                start_new_session=True,
            )
        ps_script = _default_project_root() / "deploy" / "launch-terminal.ps1"
        wrapper = _default_project_root() / "deploy" / "deploy-wrapper.sh"
        if not ps_script.exists() or not wrapper.exists():
            emulator = "wsl-cmd"
        else:
            command_args = list(command)
            if command_args and command_args[0] == "bash" and len(command_args) >= 2:
                command_args = command_args[2:]
            proc_args = [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ps_script),
                "-Title",
                title or "Codex Agent",
                "-WrapperScript",
                str(wrapper),
            ]
            proc_args.extend(command_args)
            return subprocess.Popen(proc_args, env=dict(env) if env else None, cwd=windows_cwd)

    if emulator == "wsl-cmd":
        wsl_shell = _build_wsl_shell_command(command, working_dir, keep_open=not skip_wrapper)
        wsl_cwd = _normalize_wsl_unc_path(str(working_dir))
        proc_args = [
            "cmd.exe",
            "/c",
            "start",
            "",
        ]
        proc_args.extend(_build_wsl_command_args(wsl_cwd, wsl_shell))
        return subprocess.Popen(proc_args, env=dict(env) if env else None, cwd=windows_cwd)

    raise RuntimeError(f"Unsupported terminal emulator: {emulator}")


def _default_project_root() -> Path:
    env_dir = os.environ.get("CLI_TOOLS_CALLER_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    return Path.cwd().resolve()


def _build_shell_command(command: Sequence[str], working_dir: Path) -> str:
    quoted_cwd = shlex.quote(str(working_dir))
    return f"cd {quoted_cwd} && {shlex.join(list(command))}; exec bash"


def _build_wsl_shell_command(
    command: Sequence[str], working_dir: Path, keep_open: bool = True
) -> str:
    wsl_path = _normalize_wsl_unc_path(str(working_dir))

    quoted_cwd = shlex.quote(wsl_path)
    command_str = f"cd {quoted_cwd} && {shlex.join(list(command))}"

    if keep_open:
        return f"{command_str}; echo; read -p 'Press enter to close this window...' -r"

    return f"{command_str}; exec bash"


def _normalize_wsl_unc_path(raw_path: str) -> str:
    """Convert Windows UNC paths that point at WSL shares into Linux paths."""

    if not raw_path:
        return raw_path

    normalized = raw_path.replace("/", "\\")
    if normalized.startswith("\\\\?\\UNC\\"):
        normalized = "\\\\" + normalized[8:]

    lowered = normalized.lower()
    for prefix in ("\\\\wsl.localhost\\", "\\\\wsl$\\"):
        if lowered.startswith(prefix):
            remainder = normalized[len(prefix) :]
            parts = [segment for segment in remainder.split("\\") if segment]
            if parts:
                parts = parts[1:]
            if parts:
                return "/" + "/".join(parts)
            return "/"

    return raw_path


def _build_wsl_command_args(working_dir: str, command_str: str) -> list[str]:
    """Return the argument vector for invoking wsl.exe with explicit distro and cwd."""

    args = ["wsl.exe"]
    distro = _current_wsl_distro()
    if distro:
        args.extend(["-d", distro])
    args.extend(["--cd", working_dir, "-e", "bash", "-lc", command_str])
    return args


def _windows_process_cwd() -> str | None:
    """Return a Windows-accessible cwd for launching Windows GUI processes from WSL."""

    env_override = os.environ.get("CLI_TOOLS_WINDOWS_CWD")
    candidates: list[str] = []
    if env_override:
        candidates.append(env_override)

    for var in ("WINDIR", "SYSTEMROOT"):
        value = os.environ.get(var)
        if value:
            candidates.append(value)

    system_drive = os.environ.get("SYSTEMDRIVE", "C:")
    system_drive = system_drive.rstrip("\\/") or "C:"
    candidates.extend(
        [
            f"{system_drive}\\Windows\\System32",
            f"{system_drive}\\Windows",
        ]
    )

    for candidate in candidates:
        wsl_path = _windows_path_to_wsl(candidate)
        if wsl_path and Path(wsl_path).exists():
            return wsl_path

    return None


def _windows_path_to_wsl(path: str) -> str | None:
    """Convert a Windows path (e.g., C:\\Windows) to its WSL /mnt/<drive> form."""

    if not path:
        return None

    if path.startswith("/"):
        return path

    if len(path) >= 2 and path[1] == ":":
        drive = path[0].lower()
        remainder = path[2:]
        remainder = remainder.lstrip("\\/")
        remainder = remainder.replace("\\", "/")
        if remainder:
            return f"/mnt/{drive}/{remainder}"
        return f"/mnt/{drive}"

    if path.startswith("\\") or path.startswith("//"):
        return None

    normalized = path.replace("\\", "/")
    return normalized if normalized else None


def _current_wsl_distro() -> str | None:
    """Return the current WSL distribution name if running inside WSL."""

    return os.environ.get("WSL_DISTRO_NAME")


def _build_osascript(shell_command: str, title: str | None) -> str:
    escaped_command = shell_command.replace('"', r"\"")
    title_snippet = ""
    if title:
        escaped_title = title.replace('"', r"\"")
        title_snippet = f'\n    set custom title of front window to "{escaped_title}"'
    return (
        'tell application "Terminal"\n'
        f'    do script "{escaped_command}"'
        f"{title_snippet}\n"
        "    activate\n"
        "end tell"
    )


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/sys/kernel/osrelease", "r", encoding="utf-8") as handle:
            data = handle.read().lower()
            return "microsoft" in data or "wsl" in data
    except OSError:
        return False
