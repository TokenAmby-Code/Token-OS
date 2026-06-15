"""Regression tests for manual Deskflow recovery entrypoints.

The Ctrl+Alt+K AHK bind must be a thin route into token-satellite's watchdog.
It must not resurrect the old duplicate implementation that killed/reopened the
Mac GUI client directly and bypassed the headless Token-API lifecycle.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_ctrl_alt_k_routes_through_watchdog_helper() -> None:
    ahk = (ROOT / "ahk" / "script-compiler.ahk").read_text()
    assert "Shell/deskflow-recover reload" in ahk
    assert 'bash -lic "deskflow"' not in ahk
    assert "open -a Deskflow" not in ahk


def test_deskflow_recover_is_endpoint_wrapper_not_duplicate_lifecycle() -> None:
    helper = (ROOT / "Shell" / "deskflow-recover").read_text()
    assert "/health" in helper
    assert "DESKFLOW_RECOVER_STARTUP_TIMEOUT_SECONDS" in helper
    assert "/kvm/control" in helper
    assert "curl -sf --connect-timeout 3 --max-time 60 -X POST" in helper
    assert "open -a Deskflow" not in helper
    assert "killall" not in helper
    assert "deskflow-core" not in helper
