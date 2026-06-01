import importlib.util
from pathlib import Path


def load_satellite_module():
    module_path = Path(__file__).resolve().parents[1] / "token-satellite.py"
    spec = importlib.util.spec_from_file_location("token_satellite_for_tests", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeDeskFlowWatchdog:
    def __init__(self, module, wait_results):
        self.module = module
        self.real = module.DeskFlowWatchdog()
        self.actions = []
        self.wait_results = list(wait_results)
        self.observation = {
            "deskflow_running": True,
            "deskflow_listening": True,
            "deskflow_connected": False,
            "mac_reachable": True,
            "mac_client_running": False,
        }

    def __getattr__(self, name):
        return getattr(self.real, name)

    def _check_deskflow_connected(self):
        return False

    def _observe(self):
        return dict(self.observation)

    def _wait_for_connection(self, seconds=None):
        self.actions.append("wait")
        if self.wait_results:
            connected = self.wait_results.pop(0)
            if connected:
                self.real._mark_connected()
            return connected
        return False

    def _start_mac_client(self):
        self.actions.append("mac_quick_reconnect")

    def _reload_deskflow_server(self):
        self.actions.append("local_reload")

    def _stop_deskflow_server(self):
        self.actions.append("local_stop")

    def _start_deskflow_server(self):
        self.actions.append("local_start")

    def _reload_mac_client(self):
        self.actions.append("mac_reload")

    def _restart_mac_client(self):
        self.actions.append("mac_full_restart")

    def _schedule_backoff(self):
        self.actions.append("backoff")

    def recover(self):
        return self.module.DeskFlowWatchdog._recover_connection(self, "test")


def test_recovery_stops_after_mac_quick_reconnect():
    module = load_satellite_module()
    watchdog = FakeDeskFlowWatchdog(module, wait_results=[True])

    watchdog.recover()

    assert watchdog.actions == ["mac_quick_reconnect", "wait"]
    assert watchdog.real.state == "running"


def test_recovery_stops_after_local_reload_before_full_restart():
    module = load_satellite_module()
    watchdog = FakeDeskFlowWatchdog(module, wait_results=[False, True])

    watchdog.recover()

    assert watchdog.actions == ["mac_quick_reconnect", "wait", "local_reload", "wait"]
    assert "local_stop" not in watchdog.actions
    assert "mac_full_restart" not in watchdog.actions
    assert watchdog.real.state == "running"


def test_recovery_lock_skips_overlapping_recovery():
    module = load_satellite_module()
    watchdog = FakeDeskFlowWatchdog(module, wait_results=[])
    assert watchdog.real._recovery_lock.acquire(blocking=False)

    try:
        watchdog.recover()
    finally:
        watchdog.real._recovery_lock.release()

    assert watchdog.actions == []
