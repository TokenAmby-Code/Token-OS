"""Root-cause guard: the test suite must never write session docs into the LIVE
Obsidian vault at /Volumes/Imperium/Imperium-ENV.

History: thousands of placeholder `needs-session-name-*.md` (Terra/Sessions) and
`test-job-*.md` (Mars/Sessions) docs were created in the live vault by test runs.
The cause was import-time-frozen vault-root resolution in `session_doc_helpers` and
`shared`: when IMPERIUM_ENV is unset and /Volumes/Imperium is mounted, the session
dirs resolve to the live vault, and tests that don't reload modules write straight
into it.

These tests pin the fix: (1) an autouse fixture isolates IMPERIUM_ENV for every
test, (2) vault-root resolution is lazy so that env redirection actually takes
effect, and (3) the file-creating chokepoints hard-fail rather than silently
polluting the live vault under pytest.
"""

from pathlib import Path

import pytest

LIVE_VAULT = Path("/Volumes/Imperium/Imperium-ENV")


def _under_live_vault(p: Path) -> bool:
    try:
        resolved = Path(p).resolve()
    except OSError:
        resolved = Path(p)
    return resolved == LIVE_VAULT or LIVE_VAULT in resolved.parents


def test_imperium_env_is_isolated_for_every_test():
    """The suite must run against an isolated vault, never the live one.

    The autouse isolation fixture sets IMPERIUM_ENV to a per-run temp dir.
    """
    import os

    env = os.environ.get("IMPERIUM_ENV")
    assert env, "IMPERIUM_ENV must be set to an isolated temp vault during tests"
    assert not _under_live_vault(Path(env)), f"IMPERIUM_ENV points at the live vault: {env}"


def test_session_doc_dirs_resolve_into_isolated_vault():
    """session_doc_helpers session dirs must resolve below the isolated vault."""
    import session_doc_helpers as sdh

    for resolver in (
        sdh.terra_sessions_dir,
        sdh.mars_sessions_dir,
        sdh.daily_notes_dir,
    ):
        d = resolver()
        assert not _under_live_vault(d), f"{resolver.__name__} resolved into live vault: {d}"


def test_shared_session_dirs_resolve_into_isolated_vault():
    """shared (used by main.py's session-doc API endpoints) must also be lazy."""
    import shared

    for resolver in (shared.default_sessions_dir, shared.mars_sessions_dir):
        d = resolver()
        assert not _under_live_vault(d), f"{resolver.__name__} resolved into live vault: {d}"


def test_unique_human_path_lands_in_isolated_vault():
    """The chokepoint that mints needs-session-name docs must write to tmp."""
    import session_doc_helpers as sdh

    fp = sdh.unique_human_path(sdh.terra_sessions_dir(), "Needs Session Name")
    assert not _under_live_vault(fp), f"unique_human_path produced a live-vault path: {fp}"
    assert fp.parent.is_dir()


def test_chokepoints_hard_fail_on_live_vault_write_under_pytest():
    """Tripwire: even if a path slips through, writing into the live vault under
    pytest must raise, not silently pollute."""
    import session_doc_helpers as sdh

    live_target = LIVE_VAULT / "Terra" / "Sessions" / "needs-session-name-pytest-tripwire.md"

    with pytest.raises(RuntimeError, match="LIVE vault"):
        sdh.unique_human_path(LIVE_VAULT / "Terra" / "Sessions", "Needs Session Name")

    with pytest.raises(RuntimeError, match="LIVE vault"):
        sdh.create_session_doc_file(live_target, "Needs Session Name", 1)

    assert not live_target.exists(), "tripwire must not have created the file"
