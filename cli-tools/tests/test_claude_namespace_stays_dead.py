"""Adversarial legacy-stays-dead: the claude namespace is out of the
skills/commands system.

Skills and commands are generic Token-Fleet surfaces (shared/skills,
shared/commands) reached through one whole-directory symlink per harness,
enforced by Token-Fleet's shared/bin/agent-surfaces-converge. The per-skill
link farm installer (cli-tools/bin/skills-sync), its CLAUDE_COMMAND_SKILLS
special case, and claude-config/commands must never resurface.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Historical records, not live surfaces.
HISTORICAL_FILES = ("WIP-MERGE-PLAN.md",)
HISTORICAL_DIRS = ("token-api/docs/handoffs/",)

BANNED_PATHS = (
    "claude-config/commands",
    "claude-config/skills",
    "cli-tools/bin/skills-sync",
)

BANNED_STRINGS = (
    "skills-sync",
    "skills_sync",
    "CLAUDE_COMMAND_SKILLS",
    "claude-config/commands",
    "claude-config/skills",
    "TOKEN_WRAPPER_SYNC_SHARED_SKILLS",
)


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return out.stdout.splitlines()


def test_banned_paths_absent() -> None:
    tracked = _tracked_files()
    for banned in BANNED_PATHS:
        hits = [f for f in tracked if f == banned or f.startswith(banned + "/")]
        assert not hits, f"legacy path resurfaced: {hits}"


def test_banned_strings_absent() -> None:
    this_test = str(Path(__file__).resolve().relative_to(REPO_ROOT))
    offenders: list[str] = []
    for name in _tracked_files():
        if name == this_test or name in HISTORICAL_FILES or name.startswith(HISTORICAL_DIRS):
            continue
        path = REPO_ROOT / name
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for banned in BANNED_STRINGS:
            if banned in text:
                offenders.append(f"{name}: {banned}")
    assert not offenders, "legacy claude-namespace references resurfaced:\n" + "\n".join(offenders)
