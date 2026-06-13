"""The python shim is a real-interpreter delegate — it must NEVER wrap into
`uv run`: uv probes the configured interpreter by executing it, so a
python->uv shim causes uv->python->uv recursion. The uv policy for agents
lives in the PreToolUse hook (claude-config/hooks/python-policy-hook.sh).
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "bin" / "python"


def write_executable(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(0o755)
    return path


def run_wrapper(tmp_path: Path, args: list[str], env_extra: dict[str, str] | None = None) -> str:
    log = tmp_path / "log"
    real_python = write_executable(
        tmp_path / "real-python",
        f'#!/usr/bin/env bash\necho real-python "$@" >> {log}\n',
    )
    uv = write_executable(
        tmp_path / "uv",
        f'#!/usr/bin/env bash\necho uv "$@" >> {log}\n',
    )
    env = os.environ.copy()
    env.pop("UV_RUN_RECURSION_DEPTH", None)
    env.update(
        {
            "PATH": f"{tmp_path}:{env.get('PATH', '')}",
            "IMPERIUM_PYTHON_BIN": str(real_python),
            "IMPERIUM_UV_BIN": str(uv),
        }
    )
    if env_extra:
        env.update(env_extra)
    subprocess.run([str(WRAPPER), *args], check=True, env=env)
    return log.read_text().strip()


def test_plain_python_delegates_to_real_interpreter(tmp_path: Path) -> None:
    assert run_wrapper(tmp_path, ["-c", "print(1)"]) == "real-python -c print(1)"


def test_no_uv_legacy_flag_is_stripped(tmp_path: Path) -> None:
    # Bypass flag from the retired uv-wrapping shim: still accepted, dropped
    # before the real interpreter sees it.
    assert run_wrapper(tmp_path, ["--no-uv", "-c", "print(1)"]) == "real-python -c print(1)"


def test_uv_probe_reaches_real_python_without_recursion(tmp_path: Path) -> None:
    # When uv executes the shim to probe the interpreter (recursion depth set),
    # the shim must hand straight off to the real python — never back to uv.
    assert (
        run_wrapper(
            tmp_path,
            ["-m", "pytest"],
            {"UV_RUN_RECURSION_DEPTH": "1"},
        )
        == "real-python -m pytest"
    )
