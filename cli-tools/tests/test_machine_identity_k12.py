"""K12 machine-identity wiring — nas-path.sh and imperium_config.py must agree.

Covers the distinct per-box ids (k12-personal / k12-work), the hostname-based
detection ladder that keeps unknown Linux nodes on the generic ``linux``
fallback, and the single ``_IMPERIUM_TOKEN_API_HOST`` hoist that de-duplicates
the satellite token_api_url derivation.
"""

import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NAS_PATH = ROOT / "cli-tools" / "lib" / "nas-path.sh"
PY_CONFIG = ROOT / "cli-tools" / "lib" / "imperium_config.py"

# Fields expected to match byte-for-byte between the shell registry and the
# Python registry for every machine id. Deliberately excluded:
#   - token_os_runtime: the shell expands $HOME/~ at source time, the Python
#     registry stores the unexpanded literal, so they are not byte-equal.
#   - shell: the Python registry has never modeled this key (pre-existing
#     asymmetry with nas-path.sh, outside B1 machine-identity scope).
_PARITY_KEYS = (
    "nas_imperium",
    "nas_civic",
    "tailscale_ip",
    "token_api_url",
    "tmuxctld_url",
    "ssh_alias",
    "device_name",
)


def _load_imperium_config():
    spec = importlib.util.spec_from_file_location("imperium_config_under_test", PY_CONFIG)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _shell_detect(hostname: str) -> str:
    """IMPERIUM_MACHINE the shell derives for a generic-Linux box named *hostname*."""
    script = f"""
set -e
unset IMPERIUM_MACHINE CLI_TOOLS TOKEN_OS
uname() {{ if [ "$1" = "-r" ]; then echo "6.8.0-134-generic"; else echo "Linux"; fi; }}
hostname() {{ echo "{hostname}"; }}
export -f uname hostname
source {NAS_PATH!s} >/dev/null 2>&1
printf '%s' "$IMPERIUM_MACHINE"
"""
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", script],
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout.strip()


def _shell_cfg(key: str, machine: str) -> str:
    """imperium_cfg <key> <machine> from a sourced nas-path.sh (mac host, so the
    detection ladder never fires and can't perturb the explicit lookup)."""
    script = f"""
set -e
uname() {{ echo Darwin; }}
export -f uname
source {NAS_PATH!s} >/dev/null 2>&1
printf '%s' "$(imperium_cfg {key} {machine})"
"""
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", script],
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout.strip()


# ── Detection ladder ──────────────────────────────────────────────────────────


def test_shell_detects_k12_boxes_by_hostname():
    assert _shell_detect("k12-personal") == "k12-personal"
    assert _shell_detect("k12-work") == "k12-work"
    # A dotted FQDN (e.g. from a fallback `hostname` without -s) must still map
    # to the short id, matching the Python .split(".")[0] behavior.
    assert _shell_detect("k12-work.tailnet.ts.net") == "k12-work"
    assert _shell_detect("k12-personal.local") == "k12-personal"


def test_shell_unknown_linux_stays_generic_fallback():
    # No K12 box may silently inherit generic linux-conditioned behavior, and no
    # random Linux node may be mistaken for a K12 box.
    assert _shell_detect("some-random-box") == "linux"
    assert _shell_detect("token-ci-runner") == "linux"


def test_python_detects_k12_boxes_by_hostname(monkeypatch):
    module = _load_imperium_config()
    monkeypatch.delenv("IMPERIUM_MACHINE", raising=False)
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.os.path, "isdir", lambda p: False)
    monkeypatch.setattr(module.platform, "node", lambda: "k12-personal")
    assert module._detect_machine() == "k12-personal"
    monkeypatch.setattr(module.platform, "node", lambda: "k12-work.tailnet.ts.net")
    assert module._detect_machine() == "k12-work"
    monkeypatch.setattr(module.platform, "node", lambda: "some-random-box")
    assert module._detect_machine() == "linux"


# ── Registry contents + physical boundary ─────────────────────────────────────


def test_k12_personal_row_localhost_and_imperium_only():
    module = _load_imperium_config()
    assert module.cfg("token_api_url", "k12-personal") == "http://localhost:7777"
    assert module.cfg("nas_imperium", "k12-personal") == "/mnt/imperium"
    assert module.cfg("tailscale_ip", "k12-personal") == "100.113.115.32"
    assert module.cfg("ssh_alias", "k12-personal") == "k12-personal"
    # Boundary: the personal box does not mount Civic.
    assert module.cfg("nas_civic", "k12-personal") == ""


def test_k12_work_row_civic_only_no_imperium():
    module = _load_imperium_config()
    assert module.cfg("tailscale_ip", "k12-work") == "100.67.168.105"
    assert module.cfg("nas_civic", "k12-work") == "/mnt/civic"
    assert module.cfg("ssh_alias", "k12-work") == "k12-work"
    # Boundary: the work box does not mount Imperium and runs no Token-OS runtime.
    assert module.cfg("nas_imperium", "k12-work") == ""
    assert module.cfg("token_os_runtime", "k12-work") == ""


def test_generic_linux_fallback_preserved():
    module = _load_imperium_config()
    assert module.cfg("nas_imperium", "linux") == "/mnt/imperium"
    assert module.cfg("nas_civic", "linux") == "/mnt/civic"


def test_k12_ips_registered_for_device_resolution():
    module = _load_imperium_config()
    assert module.DEVICE_IPS.get("100.113.115.32") == "K12-Personal"
    assert module.DEVICE_IPS.get("100.67.168.105") == "K12-Work"


# ── Token-API host hoist ──────────────────────────────────────────────────────


def test_satellite_token_api_urls_derive_from_single_host():
    module = _load_imperium_config()
    host = module._IMPERIUM_TOKEN_API_HOST
    expected = f"http://{host}:7777"
    for machine in ("wsl", "phone", "linux", "k12-work"):
        assert module.cfg("token_api_url", machine) == expected
    # Local-Token-API machines opt out of the shared host.
    assert module.cfg("token_api_url", "mac") == "http://localhost:7777"
    assert module.cfg("token_api_url", "k12-personal") == "http://localhost:7777"


def test_shell_and_python_hoist_agree():
    module = _load_imperium_config()
    shell_host = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            f"uname() {{ echo Darwin; }}; export -f uname; "
            f'source {NAS_PATH!s} >/dev/null 2>&1; printf "%s" "$_IMPERIUM_TOKEN_API_HOST"',
        ],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert shell_host == module._IMPERIUM_TOKEN_API_HOST


# ── Shell/Python registry parity ──────────────────────────────────────────────


def test_shell_and_python_registries_agree_for_all_machines():
    module = _load_imperium_config()
    for machine in module._REGISTRY:
        for key in _PARITY_KEYS:
            py_val = module.cfg(key, machine)
            sh_val = _shell_cfg(key, machine)
            assert py_val == sh_val, (machine, key, py_val, sh_val)
