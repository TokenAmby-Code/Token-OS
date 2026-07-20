"""Adversarial legacy-stays-dead: Token-OS does not own the obsidian CLI.

The canonical `obsidian` command is Token-Fleet-owned and delivered on PATH
uniformly across the fleet. Token-OS deleted its wrapper
(cli-tools/bin/obsidian) with no forwarding shim, symlink, alias, or divergent
copy. Callers inside Token-OS invoke `obsidian` by ordinary PATH contract only
— never through a Token-OS file path. This test keeps all of that dead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Historical records, not live surfaces.
HISTORICAL_DIRS = ("token-api/docs/",)

BANNED_PATHS = (
    "cli-tools/bin/obsidian",
    "cli-tools/bin/obsidian-screenshot",
)

# File-path invocation patterns for a Token-OS-owned obsidian binary. Callers
# must use the bare `obsidian` command on PATH instead.
BANNED_STRINGS = (
    "bin/obsidian",
    '"bin" / "obsidian"',
    "'bin' / 'obsidian'",
)


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return out.stdout.splitlines()


def test_no_tracked_obsidian_executable() -> None:
    """No tracked file anywhere in Token-OS may be named `obsidian`."""
    hits = [f for f in _tracked_files() if Path(f).name == "obsidian"]
    assert not hits, f"Token-OS reintroduced an owned obsidian copy: {hits}"


def test_banned_paths_absent() -> None:
    tracked = _tracked_files()
    for banned in BANNED_PATHS:
        hits = [f for f in tracked if f == banned or f.startswith(banned + "/")]
        assert not hits, f"legacy path resurfaced: {hits}"


def test_no_file_path_invocations() -> None:
    this_test = str(Path(__file__).resolve().relative_to(REPO_ROOT))
    offenders: list[str] = []
    for name in _tracked_files():
        if name == this_test or name.startswith(HISTORICAL_DIRS):
            continue
        path = REPO_ROOT / name
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for banned in BANNED_STRINGS:
            if banned in text:
                offenders.append(f"{name}: {banned}")
    assert not offenders, (
        "obsidian file-path invocations resurfaced (use the bare `obsidian` "
        "PATH contract):\n" + "\n".join(offenders)
    )
