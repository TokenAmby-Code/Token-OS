"""Focused tests for the nas-grep NAS-safe search wrapper."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import nas_grep

REPO_ROOT = Path(__file__).resolve().parents[2]
NAS_GREP = REPO_ROOT / "cli-tools" / "bin" / "nas-grep"


def test_rg_command_has_conservative_defaults():
    config = nas_grep.SearchConfig(pattern="needle", paths=("/Volumes/Imperium/Vault",))

    command = nas_grep.build_rg_command(config, rg_path="rg")

    assert command[:6] == [
        "rg",
        "--color=never",
        "--line-number",
        "--with-filename",
        "--no-heading",
        "--no-messages",
    ]
    assert "--max-filesize" in command
    assert command[command.index("--max-filesize") + 1] == "5M"
    assert "--threads" in command
    assert command[command.index("--threads") + 1] == "2"
    assert [command[index + 1] for index, value in enumerate(command) if value == "--glob"].count(
        "!**/.git/**"
    ) == 1
    assert "!**/node_modules/**" in command
    assert "!**/.venv/**" in command
    assert "!**/__pycache__/**" in command
    assert "!**/Trash/**" in command
    assert "!*.bak" in command
    assert command[-3:] == ["--", "needle", "/Volumes/Imperium/Vault"]


def test_python_backend_excludes_pathological_dirs_and_backup_files(tmp_path: Path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "keep.md").write_text("alpha\nneedle one\n", encoding="utf-8")
    (root / "backup.bak").write_text("needle backup\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "package.txt").write_text("needle node\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "packed-refs").write_text("needle git\n", encoding="utf-8")
    (root / "Obsidian").mkdir()
    (root / "Obsidian" / "Trash").mkdir()
    (root / "Obsidian" / "Trash" / "old.md").write_text("needle trash\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(NAS_GREP), "--tool", "python", "needle", str(root)],
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert proc.returncode == 0
    assert "keep.md:2:needle one" in proc.stdout
    assert "node_modules" not in proc.stdout
    assert ".git" not in proc.stdout
    assert "Trash" not in proc.stdout
    assert "backup.bak" not in proc.stdout


def test_python_backend_caps_total_results(tmp_path: Path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "one.md").write_text("needle 1\nneedle 2\n", encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(NAS_GREP),
            "--tool",
            "python",
            "--max-results",
            "1",
            "needle",
            str(root),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert proc.returncode == 0
    assert proc.stdout.count("needle") == 1
    assert "stopped after --max-results=1" in proc.stderr


def test_nas_path_detection_includes_known_mounts():
    assert nas_grep.is_nas_path("/Volumes/Imperium")
    assert nas_grep.is_nas_path("/Volumes/Imperium/Terra")
    assert nas_grep.is_nas_path("/mnt/imperium/Terra")
    assert not nas_grep.is_nas_path("/tmp/not-imperium")
