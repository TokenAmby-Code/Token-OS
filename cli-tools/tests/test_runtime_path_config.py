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
source {NAS_PATH!s}
printf '%s\n%s\n%s\n' "$IMPERIUM" "$TOKEN_OS" "$CLI_TOOLS"
"""
    proc = subprocess.run(["bash", "-lc", script], text=True, capture_output=True, check=True)
    imperium, token_os, cli_tools = proc.stdout.strip().splitlines()
    assert imperium == "/Volumes/Imperium"
    assert token_os == str(local)
    assert cli_tools == f"{local}/cli-tools"
