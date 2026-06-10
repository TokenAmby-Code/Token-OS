"""Tests for the Mac headless Deskflow client lifecycle in token-api.

The client is a launchd job (com.imperium.deskflow-client) wrapping
deskflow-core in a bounded-retry supervisor. These tests mock subprocess at
the module boundary and assert the launchctl choreography:

  start  = legacy-GUI kill + kickstart -k (atomic converge), bootstrap
           fallback for an unloaded job, keymap guard pre/post
  stop   = launchctl kill SIGTERM + killall -9 fallback (job stays loaded)
  reload = kickstart WITHOUT -k — start-only-if-dead, never bounces a live
           session
"""

import os
from types import SimpleNamespace

import pytest


class FakeProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class SubprocessRecorder:
    """Records every subprocess.run/Popen argv; per-prefix exit-code overrides."""

    def __init__(self):
        self.run_calls = []
        self.popen_calls = []
        self._fail_once = []

    def fail_once(self, *argv_prefix):
        self._fail_once.append(list(argv_prefix))

    def run(self, argv, **kwargs):
        argv = [str(a) for a in argv]
        self.run_calls.append(argv)
        for prefix in list(self._fail_once):
            if argv[: len(prefix)] == prefix:
                self._fail_once.remove(prefix)
                return FakeProcess(returncode=1)
        return FakeProcess(returncode=0)

    def popen(self, argv, **kwargs):
        self.popen_calls.append([str(a) for a in argv])
        return FakeProcess(returncode=0)

    def run_argvs_with(self, *words):
        return [c for c in self.run_calls if all(w in c for w in words)]


@pytest.fixture
def kvm_env(app_env, tmp_path, monkeypatch):
    main = app_env.main
    recorder = SubprocessRecorder()
    monkeypatch.setattr(main.subprocess, "run", recorder.run)
    monkeypatch.setattr(main.subprocess, "Popen", recorder.popen)
    # Keep agent-sync writes out of the real ~/Library.
    helper_dir = tmp_path / "imperium-helper"
    monkeypatch.setattr(main, "DESKFLOW_HELPER_DIR", helper_dir)
    return SimpleNamespace(
        main=main,
        recorder=recorder,
        helper_dir=helper_dir,
        target=f"gui/{os.getuid()}/com.imperium.deskflow-client",
    )


class TestEnsureAgent:
    def test_first_sync_installs_and_bootstraps(self, kvm_env):
        kvm_env.main._ensure_deskflow_client_agent()

        supervisor = kvm_env.helper_dir / "deskflow-client-supervisor.py"
        plist = kvm_env.helper_dir / "com.imperium.deskflow-client.plist"
        assert supervisor.exists()
        assert os.access(supervisor, os.X_OK)
        assert plist.exists()
        # Template __HOME__ placeholder must be expanded.
        assert "__HOME__" not in plist.read_text()
        assert str(kvm_env.main.Path.home()) in plist.read_text()

        assert kvm_env.recorder.run_argvs_with("launchctl", "bootout", kvm_env.target)
        assert kvm_env.recorder.run_argvs_with("launchctl", "bootstrap", str(plist))

    def test_unchanged_content_is_a_noop(self, kvm_env):
        kvm_env.main._ensure_deskflow_client_agent()
        kvm_env.recorder.run_calls.clear()

        kvm_env.main._ensure_deskflow_client_agent()
        assert not kvm_env.recorder.run_calls  # no bootout/bootstrap churn

    def test_plist_change_rebootstraps(self, kvm_env):
        kvm_env.main._ensure_deskflow_client_agent()
        plist = kvm_env.helper_dir / "com.imperium.deskflow-client.plist"
        plist.write_text("stale")
        kvm_env.recorder.run_calls.clear()

        kvm_env.main._ensure_deskflow_client_agent()
        assert kvm_env.recorder.run_argvs_with("launchctl", "bootout")
        assert kvm_env.recorder.run_argvs_with("launchctl", "bootstrap")


class TestStart:
    def test_start_choreography(self, kvm_env):
        kvm_env.main._start_mac_deskflow_client("test")

        calls = kvm_env.recorder.run_calls
        # Legacy GUI killed (its own core supervisor would fight the headless job).
        legacy_kill = calls.index(["killall", "Deskflow"])
        kickstart = calls.index(["launchctl", "kickstart", "-k", kvm_env.target])
        assert legacy_kill < kickstart

        # Keymap guard pre and post around the kickstart.
        guard_calls = [i for i, c in enumerate(calls) if "deskflow-keymap-guard.sh" in c[0]]
        assert len(guard_calls) == 2
        assert guard_calls[0] < kickstart < guard_calls[1]

        # Display wake.
        assert ["caffeinate", "-u", "-t", "5"] in kvm_env.recorder.popen_calls
        # No GUI launch anywhere.
        assert not kvm_env.recorder.run_argvs_with("open")
        assert all(c[0] != "open" for c in kvm_env.recorder.popen_calls)

    def test_start_falls_back_to_bootstrap_when_job_unloaded(self, kvm_env):
        # First kickstart fails (job not loaded, e.g. after Mac reboot).
        kvm_env.main._ensure_deskflow_client_agent()
        kvm_env.recorder.run_calls.clear()
        kvm_env.recorder.fail_once("launchctl", "kickstart", "-k")

        kvm_env.main._start_mac_deskflow_client("test")

        kickstarts = kvm_env.recorder.run_argvs_with("launchctl", "kickstart", "-k")
        bootstraps = kvm_env.recorder.run_argvs_with("launchctl", "bootstrap")
        assert len(kickstarts) == 2  # failed attempt + retry after bootstrap
        assert len(bootstraps) == 1

    def test_start_skips_bootstrap_when_kickstart_succeeds(self, kvm_env):
        kvm_env.main._ensure_deskflow_client_agent()
        kvm_env.recorder.run_calls.clear()

        kvm_env.main._start_mac_deskflow_client("test")

        assert len(kvm_env.recorder.run_argvs_with("launchctl", "kickstart", "-k")) == 1
        assert not kvm_env.recorder.run_argvs_with("launchctl", "bootstrap")


class TestStop:
    def test_stop_choreography(self, kvm_env):
        kvm_env.main._stop_mac_deskflow_client("test")

        calls = kvm_env.recorder.run_calls
        graceful = calls.index(["launchctl", "kill", "SIGTERM", kvm_env.target])
        sweep = calls.index(["killall", "-9", "Deskflow", "deskflow-core"])
        assert graceful < sweep
        # Job is never unloaded on stop — KeepAlive=false means nothing respawns.
        assert not kvm_env.recorder.run_argvs_with("launchctl", "bootout")


class TestReload:
    def test_reload_never_bounces_a_live_session(self, kvm_env):
        kvm_env.main._reload_mac_deskflow_client("test")

        gentle = kvm_env.recorder.run_argvs_with("launchctl", "kickstart")
        assert [c for c in gentle if "-k" not in c]
        assert not [c for c in gentle if "-k" in c]
        # No kills of any kind on the gentle path.
        assert not kvm_env.recorder.run_argvs_with("killall")
        assert not kvm_env.recorder.run_argvs_with("launchctl", "kill", "SIGTERM")
        assert ["caffeinate", "-u", "-t", "5"] in kvm_env.recorder.popen_calls
