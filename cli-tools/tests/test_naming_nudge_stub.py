from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "claude-config" / "hooks" / ("naming-nudge.sh")
SELF = Path(__file__).resolve()


def test_legacy_naming_nudge_hook_is_deleted_and_unreferenced() -> None:
    """Naming delivery is Token-API owned; no shell-hook middleware remains."""
    assert not SCRIPT.exists()

    needle = "naming-nudge.sh"
    offenders: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.resolve() == SELF:
            continue
        rel = path.relative_to(ROOT)
        if any(
            part in {".git", "__pycache__", ".pytest_cache", ".ruff_cache"} for part in rel.parts
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if needle in text:
            offenders.append(str(rel))
    assert offenders == []
