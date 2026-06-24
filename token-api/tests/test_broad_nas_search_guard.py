from __future__ import annotations

import pytest

from routes import hooks
from routes.hooks import classify_broad_nas_search


def assert_denied(command: str) -> str:
    reason = classify_broad_nas_search(command)
    assert reason is not None
    assert "Blocked broad NAS-root recursive search" in reason
    assert "git grep" in reason
    assert "-maxdepth" in reason
    return reason


def assert_allowed(command: str) -> None:
    assert classify_broad_nas_search(command) is None


def test_denies_find_volumes_root_scan() -> None:
    assert_denied("find /Volumes -type d -name token-api")


def test_denies_bfs_volumes_root_scan() -> None:
    assert_denied("bfs /Volumes -type d -name token-api")


def test_denies_rg_imperium_root_scan() -> None:
    assert_denied("rg foo /Volumes/Imperium")


def test_denies_recursive_grep_civic_root_scan() -> None:
    assert_denied("grep -R foo /Volumes/Civic")


def test_denies_mnt_imperium_root_scan() -> None:
    assert_denied("ugrep foo /mnt/imperium")


def test_allows_relative_rg() -> None:
    assert_allowed("rg foo .")


def test_allows_git_grep() -> None:
    assert_allowed("git grep foo")


def test_allows_bounded_find_inside_vault() -> None:
    assert_allowed("find /Volumes/Imperium/Imperium-ENV -maxdepth 3 -type d -name Mars")


def test_allows_rg_inside_vault_subdirectory() -> None:
    assert_allowed("rg foo /Volumes/Imperium/Imperium-ENV/Mars")


def test_allows_grep_without_recursive_flag() -> None:
    assert_allowed("grep foo /Volumes/Imperium")


def test_denies_env_imperium_root_scan() -> None:
    assert_denied("rg foo $IMPERIUM")


def test_allows_env_imperium_vault_scan() -> None:
    assert_allowed("rg foo $IMPERIUM/Imperium-ENV")


@pytest.mark.asyncio
async def test_pretooluse_bash_denies_broad_nas_search() -> None:
    result = await hooks.handle_pre_tool_use(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "find /Volumes -type d -name token-api"},
        }
    )
    assert result["permissionDecision"] == "deny"
    assert "Root-wide scans" in result["permissionDecisionReason"]
    assert "$IMPERIUM/Imperium-ENV" in result["permissionDecisionReason"]
