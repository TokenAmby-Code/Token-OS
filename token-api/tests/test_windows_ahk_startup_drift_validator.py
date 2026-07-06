from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

VALIDATOR = Path(__file__).resolve().parents[1] / "scripts" / "validate-windows-ahk-startup-drift"
AHK_FILES = [
    "startup.ahk",
    "ahk-nas-wait.bat",
    "ring-remap.ahk",
    "script-compiler.ahk",
    "nested/helper.ahk",
]


def _stage_tree(tmp_path: Path) -> dict[str, Path]:
    live = tmp_path / "live"
    src = live / "ahk"
    cache = tmp_path / "TokenOS" / "ahk"
    profile = tmp_path / "Users" / "colby"
    startup = profile / "Imperium-Startup"
    for path in [src, cache, startup]:
        path.mkdir(parents=True, exist_ok=True)
    for name in AHK_FILES:
        content = f"; {name}\nMsgBox '{name}'\n"
        (src / name).parent.mkdir(parents=True, exist_ok=True)
        (src / name).write_text(content, encoding="utf-8")
        (cache / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / name, cache / name)
    shutil.copy2(src / "startup.ahk", profile / "startup.ahk")
    shutil.copy2(src / "ahk-nas-wait.bat", profile / "ahk-nas-wait.bat")
    shutil.copy2(src / "ring-remap.ahk", startup / "ring-remap.ahk")
    shutil.copy2(src / "script-compiler.ahk", startup / "script-compiler.ahk")
    return {"live": live, "cache": cache, "profile": profile, "startup": startup}


def _run(paths: dict[str, Path]) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "TOKEN_OS_RUNTIME_CHECKOUT": str(paths["live"]),
        "TOKEN_OS_AHK_DIR": str(paths["cache"]),
        "TOKEN_OS_WINDOWS_USER_PROFILE": str(paths["profile"]),
        "TOKEN_OS_STARTUP_ROOT": str(paths["startup"]),
        "TOKEN_OS_VALIDATE_SCHEDULED_TASKS": "false",
    }
    return subprocess.run([str(VALIDATOR)], text=True, capture_output=True, env=env, check=False)


def test_validator_passes_when_cache_and_startup_hashes_match(tmp_path: Path) -> None:
    paths = _stage_tree(tmp_path)
    proc = _run(paths)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Windows AHK startup drift validation passed" in proc.stdout


def test_validator_fails_on_hash_drift_without_self_healing(tmp_path: Path) -> None:
    paths = _stage_tree(tmp_path)
    target = paths["startup"] / "ring-remap.ahk"
    target.write_text("; stale local copy\n", encoding="utf-8")
    before = target.read_text(encoding="utf-8")

    proc = _run(paths)

    assert proc.returncode != 0
    assert "startup:ring-remap.ahk hash drift" in proc.stderr
    assert target.read_text(encoding="utf-8") == before


def test_validator_fails_on_retired_nas_runtime_reference(tmp_path: Path) -> None:
    paths = _stage_tree(tmp_path)
    wrapper = paths["profile"] / "ahk-nas-wait.bat"
    source_wrapper = paths["live"] / "ahk" / "ahk-nas-wait.bat"
    stale = r"set AHK_DIR=\\Token-NAS\Imperium\runtimes\token-os\live\ahk" + "\n"
    wrapper.write_text(stale, encoding="utf-8")
    source_wrapper.write_text(stale, encoding="utf-8")
    shutil.copy2(source_wrapper, paths["cache"] / "ahk-nas-wait.bat")

    proc = _run(paths)

    assert proc.returncode != 0
    assert "retired NAS runtime reference" in proc.stderr
