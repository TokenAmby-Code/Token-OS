import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NAS_PATH = ROOT / "cli-tools" / "lib" / "nas-path.sh"
PY_CONFIG = ROOT / "cli-tools" / "lib" / "imperium_config.py"


def test_python_config_prefers_existing_mac_local_runtime(monkeypatch, tmp_path):
    runtime = Path.home() / "runtimes" / "Token-OS" / "live"
    if not runtime.is_dir():
        monkeypatch.setenv("TOKEN_OS", str(tmp_path / "missing"))
        expected = "/Volumes/Imperium/runtimes/token-os/live"
    else:
        monkeypatch.delenv("TOKEN_OS", raising=False)
        expected = str(runtime)
    monkeypatch.setenv("IMPERIUM_MACHINE", "mac")
    spec = importlib.util.spec_from_file_location("imperium_config_under_test", PY_CONFIG)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module.TOKEN_OS == expected
    assert module.CLI_TOOLS == f"{expected}/cli-tools"


def test_shell_config_keeps_imperium_nas_but_uses_local_runtime_when_present(tmp_path):
    home = tmp_path / "home"
    local = home / "runtimes" / "Token-OS" / "live"
    (local / "cli-tools").mkdir(parents=True)
    script = f"""
set -e
HOME={home!s}
PATH=/usr/bin:/bin
uname() {{ echo Darwin; }}
source {NAS_PATH!s}
printf '%s\n%s\n%s\n' "$IMPERIUM" "$TOKEN_OS" "$CLI_TOOLS"
"""
    proc = subprocess.run(["bash", "-lc", script], text=True, capture_output=True, check=True)
    imperium, token_os, cli_tools = proc.stdout.strip().splitlines()
    assert imperium == "/Volumes/Imperium"
    assert token_os == str(local)
    assert cli_tools == f"{local}/cli-tools"


# ── Quarantine guard: resolution must NEVER land in #recycle / a dated legacy
# archive (incident 2026-06-22). A stale exported TOKEN_OS pointing into the
# Synology recycle bin previously WON Python resolution, making tools run from —
# and cut worktrees bound to — a bare in a purge target → silent data loss. ──


def _load_imperium_config():
    spec = importlib.util.spec_from_file_location("imperium_config_under_test", PY_CONFIG)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_python_config_rejects_recycle_bin_runtime_override(monkeypatch, tmp_path):
    """A stale TOKEN_OS inside #recycle must NOT win, even though the dir exists."""
    recycle_runtime = tmp_path / "#recycle" / "Token-OS.legacy-20260610"
    (recycle_runtime / "cli-tools").mkdir(parents=True)
    monkeypatch.setenv("IMPERIUM_MACHINE", "mac")
    monkeypatch.setenv("TOKEN_OS", str(recycle_runtime))
    module = _load_imperium_config()
    assert "#recycle" not in module.TOKEN_OS
    assert ".legacy-" not in module.TOKEN_OS


def test_python_config_rejects_dated_legacy_archive_override(monkeypatch, tmp_path):
    """A dated legacy archive path (…legacy-YYYYMMDD) is quarantined too."""
    legacy = tmp_path / "Token-OS.legacy-20260610"
    (legacy / "cli-tools").mkdir(parents=True)
    monkeypatch.setenv("IMPERIUM_MACHINE", "mac")
    monkeypatch.setenv("TOKEN_OS", str(legacy))
    module = _load_imperium_config()
    assert ".legacy-" not in module.TOKEN_OS


def test_quarantine_predicate_shell_and_python_agree(tmp_path):
    """The shell helper and the Python helper classify the same paths."""
    module = _load_imperium_config()
    quarantined = [
        "/Volumes/Imperium/#recycle/token-os.git",
        "/Volumes/Imperium/#recycle/Token-OS.legacy-20260610/cli-tools",
        "/Users/x/Token-OS.legacy-20260610",
        "/Users/x/.Trash/token-os.git",
    ]
    clean = [
        "/Users/tokenclaw/runtimes/Token-OS/token-os.git",
        "/Users/tokenclaw/worktrees/Token-OS/wt-decouple-legacy-token-os-paths",
        "/Volumes/Imperium/runtimes/token-os/live",
    ]
    for p in quarantined:
        assert module._is_quarantined(p) is True, p
    for p in clean:
        assert module._is_quarantined(p) is False, p

    # Shell helper in nas-path.sh must agree exactly. Pass the lib path and the
    # candidate as positional args ($1, $2) rather than interpolating them into
    # the command body, to avoid shell-injection / quoting surprises.
    for expect_q, paths in ((0, quarantined), (1, clean)):
        for p in paths:
            proc = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1" 2>/dev/null; imperium_path_is_quarantined "$2"; echo $?',
                    "_",
                    str(NAS_PATH),
                    p,
                ],
                text=True,
                capture_output=True,
            )
            assert proc.stdout.strip() == str(expect_q), (p, proc.stdout, proc.stderr)
