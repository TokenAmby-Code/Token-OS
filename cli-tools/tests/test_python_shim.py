"""Tests for the `bin/python` interpreter shim.

The shim MUST remain a pure real-interpreter delegate. It must NOT wrap back
into `uv run`: uv probes the configured interpreter by executing it, so a
python->uv shim causes uv->python->uv recursion (the bug these tests guard).
uv-backed-python policy lives in the PreToolUse companion hook instead
(see test_uv_python_policy_hook.py).
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


def _fakes(tmp_path: Path):
    """Create fake `real-python` and `uv` that log their invocation."""
    log = tmp_path / "log"
    real_python = write_executable(
        tmp_path / "real-python",
        f'#!/usr/bin/env bash\necho real-python "$@" >> {log}\n',
    )
    uv = write_executable(
        tmp_path / "uv",
        f'#!/usr/bin/env bash\necho uv "$@" >> {log}\n',
    )
    return log, real_python, uv


def run_wrapper(tmp_path: Path, args: list[str], env_extra: dict[str, str] | None = None):
    log, real_python, uv = _fakes(tmp_path)
    env = os.environ.copy()
    env.pop("UV_RUN_RECURSION_DEPTH", None)
    env.update(
        {
            # Put tmp first so the fake uv shadows any real uv on PATH; if the
            # shim ever calls uv again we will see it in the log.
            "PATH": f"{tmp_path}:{env.get('PATH', '')}",
            "IMPERIUM_PYTHON_BIN": str(real_python),
            "IMPERIUM_UV_BIN": str(uv),
        }
    )
    if env_extra:
        env.update(env_extra)
    subprocess.run([str(WRAPPER), *args], check=True, env=env)
    return log.read_text().strip() if log.exists() else ""


def test_plain_python_delegates_directly_to_real_python(tmp_path):
    """Plain `python ...` execs the real interpreter directly — never via uv."""
    out = run_wrapper(tmp_path, ["-c", "print(1)"])
    assert out == "real-python -c print(1)"
    assert "uv" not in out  # the recursion source must be gone


def test_uv_is_never_invoked(tmp_path):
    """The shim must not shell out to uv under any normal invocation."""
    out = run_wrapper(tmp_path, ["-m", "pytest", "-q"])
    assert out == "real-python -m pytest -q"
    assert "uv run" not in out


def test_self_referential_python_bin_is_skipped(tmp_path):
    """Recursion guard: if IMPERIUM_PYTHON_BIN points at the shim itself, the
    shim must REJECT it (cand_real == self_real) and fall through to a real
    interpreter via REAL_PYTHON — proving the self-exclusion that prevents the
    python->uv->python recursion.
    """
    log, real_python, _uv = _fakes(tmp_path)
    env = os.environ.copy()
    env.pop("UV_RUN_RECURSION_DEPTH", None)
    env.update(
        {
            "PATH": f"{tmp_path}:{env.get('PATH', '')}",
            # Poisoned: points the "real python" env hint back at the shim.
            "IMPERIUM_PYTHON_BIN": str(WRAPPER),
            "REAL_PYTHON": str(real_python),
        }
    )
    subprocess.run([str(WRAPPER), "-c", "print(1)"], check=True, env=env, timeout=15)
    # The shim skipped the self-referential hint and used the genuine real python.
    assert log.read_text().strip() == "real-python -c print(1)"


def test_shim_named_python_resolves_a_different_real_interpreter(tmp_path):
    """If the shim is itself first on PATH as `python`, invoking it must resolve
    a DIFFERENT real interpreter, not re-invoke itself (the recursion path).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Copy the shim in as `python` so it is first on PATH.
    shim_as_python = bin_dir / "python"
    shim_as_python.write_text(WRAPPER.read_text())
    shim_as_python.chmod(0o755)

    log = tmp_path / "log"
    real_python = write_executable(
        tmp_path / "real-python",
        f'#!/usr/bin/env bash\necho real-python "$@" >> {log}\n',
    )
    env = os.environ.copy()
    env.pop("UV_RUN_RECURSION_DEPTH", None)
    env.pop("IMPERIUM_PYTHON_BIN", None)
    env.update(
        {
            "PATH": f"{bin_dir}:{tmp_path}:{env.get('PATH', '')}",
            "REAL_PYTHON": str(real_python),
        }
    )
    subprocess.run([str(shim_as_python), "-c", "print(1)"], check=True, env=env, timeout=15)
    assert log.read_text().strip() == "real-python -c print(1)"
