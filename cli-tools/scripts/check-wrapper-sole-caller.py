#!/usr/bin/env python3
"""Fail if production code names raw agent engine binaries outside the wrapper.

Emperor ruling (2026-07-05): the tracked Token-OS agent wrapper is the sole
caller of raw claude/codex engine binaries. Everything else must invoke a wrapper
front-door (agent-wrapper.sh, or the installed claude/codex shims that enter it).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_ALLOWED = {
    Path("cli-tools/scripts/agent-wrapper.sh"),
    Path("cli-tools/bin/claude"),
    Path("cli-tools/bin/codex"),
    Path("cli-tools/bin/agent-wrapper-install-shims"),
    Path("cli-tools/scripts/check-wrapper-sole-caller.py"),
}
_SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "node_modules",
    "__pycache__",
}
_SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".sqlite", ".db"}

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("raw token-os engine binary", re.compile(r"(?:claude|codex)[.]token-os-real")),
    (
        "absolute Homebrew engine path",
        re.compile(r"/opt/homebrew/bin/(?:node\s+)?(?:claude|codex)(?:\s|$|[\"'])"),
    ),
    (
        "node launching an absolute engine path",
        re.compile(
            r"\bnode\s+/(?:opt/homebrew|usr/local|Users/[^\s'\"]+)/[^\n'\"]*(?:claude|codex)"
        ),
    ),
    (
        "wrapper bypass outside wrapper",
        re.compile(r"TOKEN_API_AGENT_WRAPPER_BYPASS\s*=\s*1"),
    ),
]


def _iter_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        try:
            is_file = path.is_file()
        except OSError:
            continue
        if not is_file or path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        files.append(path)
    return files


def main() -> int:
    violations: list[str] = []
    for path in _iter_files():
        rel = path.relative_to(ROOT)
        if rel in _ALLOWED:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for label, pattern in _PATTERNS:
            for match in pattern.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                line = text.splitlines()[line_no - 1].strip()
                violations.append(f"{rel}:{line_no}: {label}: {line}")
    if violations:
        print("WRAPPER SOLE-CALLER GUARD FAILED", file=sys.stderr)
        print(
            "Raw claude/codex engine binaries may only be referenced by the tracked wrapper/shims.",
            file=sys.stderr,
        )
        for item in violations:
            print(item, file=sys.stderr)
        return 1
    print("wrapper sole-caller guard passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
