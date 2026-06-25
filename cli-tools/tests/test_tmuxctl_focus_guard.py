from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import tmuxctl.focus_guard as focus_guard
import tmuxctl.tmux_adapter as tmux_adapter
from tmuxctl.focus_guard import preserve_focus
from tmuxctl.tmux_adapter import TmuxAdapter


class FakeFocusAdapter:
    def __init__(self) -> None:
        self.current_window = "main:1"
        self.current_pane = "%1"
        self.pane_window = {"%1": "main:1", "%2": "main:2"}
        self.commands: list[tuple[str, ...]] = []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.commands.append(args)
        if args[0] == "display-message":
            target = args[args.index("-t") + 1] if "-t" in args else ""
            fmt = args[-1]
            if fmt == "#{session_name}:#{window_index}\t#{pane_id}":
                if target:
                    return (
                        f"{self.pane_window.get(target, '')}\t{target}\n"
                        if target in self.pane_window
                        else ""
                    )
                return f"{self.current_window}\t{self.current_pane}\n"
            if fmt == "#{pane_id}":
                if target:
                    return f"{target}\n" if target in self.pane_window else ""
                return f"{self.current_pane}\n"
            if fmt == "#{session_name}:#{window_index}":
                return self.pane_window.get(target, self.current_window) + "\n"
        if args[0] == "select-window":
            self.current_window = args[args.index("-t") + 1]
        if args[0] == "select-pane":
            pane = args[args.index("-t") + 1]
            self.current_pane = pane
            self.current_window = self.pane_window.get(pane, self.current_window)
        return ""


class FakeTmuxServer:
    """Stateful stand-in for the real tmux server behind TmuxAdapter.run().

    Models the camera (current window/pane), per-window zoom, and the
    client_activity epoch so displacement/zoom/human-wins semantics can be
    exercised through the REAL adapter classification and counter.
    """

    def __init__(self) -> None:
        self.window = "main:1"
        self.pane = "%1"
        self.pane_window = {"%1": "main:1", "%2": "main:2"}
        self.window_names = {"main:1": "palace", "main:2": "builds"}
        self.zoomed_windows: set[str] = set()
        self.client_activity = 1_000
        self.argv_log: list[tuple[str, ...]] = []

    def patch_into(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = self

        class _Completed:
            def __init__(self, stdout: str) -> None:
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def _fake_run(cmd, *args, **kwargs):
            return _Completed(server.exec(list(cmd[1:])))

        monkeypatch.setattr(tmux_adapter.subprocess, "run", _fake_run)

    def exec(self, argv: list[str]) -> str:
        self.argv_log.append(tuple(argv))
        cmd = argv[0]
        if cmd == "display-message":
            target = argv[argv.index("-t") + 1] if "-t" in argv else ""
            window = self.pane_window.get(target, self.window) if target else self.window
            pane = target if target else self.pane
            out = argv[-1]
            out = out.replace("#{session_name}:#{window_index}", window)
            out = out.replace("#{window_name}", self.window_names.get(window, "palace"))
            out = out.replace("#{pane_id}", pane)
            out = out.replace(
                "#{window_zoomed_flag}", "1" if window in self.zoomed_windows else "0"
            )
            out = out.replace("#{client_activity}", str(self.client_activity))
            return out + "\n"
        if cmd == "select-window":
            self.window = argv[argv.index("-t") + 1]
        elif (
            cmd == "select-pane"
            and "-t" in argv
            and not ("-T" in argv and "-Z" not in argv and "-P" not in argv)
        ):
            # Real tmux selects the target for `select-pane -P` and for
            # select/zoom combinations even when they also set a title.
            pane = argv[argv.index("-t") + 1]
            self.pane = pane
            self.window = self.pane_window.get(pane, self.window)
            if "-Z" not in argv:
                self.zoomed_windows.discard(self.window)
        elif cmd == "resize-pane" and "-Z" in argv:
            target = argv[argv.index("-t") + 1] if "-t" in argv else self.pane
            window = self.pane_window.get(target, self.window)
            if window in self.zoomed_windows:
                self.zoomed_windows.discard(window)
            else:
                self.zoomed_windows.add(window)
        return ""

    def human_moves_to(self, pane: str) -> None:
        self.pane = pane
        self.window = self.pane_window.get(pane, self.window)
        self.client_activity += 5


def _general_log_events() -> list[dict]:
    path = pathlib.Path(os.environ["IMPERIUM_TMUX_FOCUS_LOG"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_preserve_focus_restores_window_and_pane_after_automation_snap() -> None:
    adapter = FakeFocusAdapter()

    with preserve_focus(adapter, source="test", attempted_target="%2"):
        adapter.run("select-window", "-t", "main:2")
        adapter.run("select-pane", "-t", "%2")

    assert adapter.current_window == "main:1"
    assert adapter.current_pane == "%1"
    assert ("select-window", "-t", "main:1") in adapter.commands
    assert ("select-pane", "-Z", "-t", "%1") in adapter.commands


def test_preserve_focus_skips_restore_when_op_never_moved_camera(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely camera-neutral option write must not yank the human back to
    a stale start-of-op snapshot when they navigated mid-operation."""
    server = FakeTmuxServer()
    server.patch_into(monkeypatch)
    adapter = TmuxAdapter(tmux_binary="tmux")

    with preserve_focus(adapter, source="SessionEnd", attempted_target="%1"):
        adapter.run(
            "set-option",
            "-p",
            "-t",
            "%1",
            "window-style",
            "bg=colour52",
            allow_failure=True,
        )
        server.human_moves_to("%2")

    assert server.pane == "%2", "human's camera position must stand"
    assert server.window == "main:2"
    assert not any(argv[0] in {"select-window", "select-pane"} for argv in server.argv_log), (
        "no restore commands may be issued for a camera-neutral op"
    )
    assert not any(event["event"] == "restored" for event in _general_log_events())


def test_preserve_focus_treats_select_pane_title_plus_zoom_as_camera_mutating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeTmuxServer()
    server.patch_into(monkeypatch)
    adapter = TmuxAdapter(tmux_binary="tmux")

    with preserve_focus(adapter, source="title-plus-zoom", attempted_target="%2"):
        adapter.run("select-pane", "-t", "%2", "-Z", "-T", "worker", allow_failure=True)

    assert server.window == "main:1"
    assert server.pane == "%1"
    assert ("select-window", "-t", "main:1") in server.argv_log


def test_preserve_focus_treats_select_pane_style_as_camera_mutating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real tmux selects the target and clears zoom for `select-pane -P`; this
    must be classified as focus-mutating until production callers stop using it."""
    server = FakeTmuxServer()
    server.zoomed_windows.add("main:1")
    server.patch_into(monkeypatch)
    adapter = TmuxAdapter(tmux_binary="tmux")

    with preserve_focus(adapter, source="style-regression", attempted_target="%2"):
        adapter.run("select-pane", "-t", "%2", "-P", "bg=colour52", allow_failure=True)

    assert server.window == "main:1"
    assert server.pane == "%1"
    assert "main:1" in server.zoomed_windows
    assert ("select-window", "-t", "main:1") in server.argv_log
    assert ("select-pane", "-Z", "-t", "%1") in server.argv_log
    assert any(event["event"] == "restored" for event in _general_log_events())


def test_preserve_focus_restores_when_op_displaced_camera(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeTmuxServer()
    server.patch_into(monkeypatch)
    adapter = TmuxAdapter(tmux_binary="tmux")

    with preserve_focus(adapter, source="stack-enforce", attempted_target="%2"):
        adapter.run("select-window", "-t", "main:2", allow_failure=True)
        adapter.run("select-pane", "-t", "%2", allow_failure=True)

    assert server.pane == "%1"
    assert server.window == "main:1"
    assert any(event["event"] == "restored" for event in _general_log_events())


def test_preserve_focus_restore_keeps_zoom(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restoring into a zoomed window must not collapse the zoom; a zoom the
    op itself collapsed is re-applied."""
    server = FakeTmuxServer()
    server.zoomed_windows.add("main:1")
    server.patch_into(monkeypatch)
    adapter = TmuxAdapter(tmux_binary="tmux")

    with preserve_focus(adapter, source="stack-enforce", attempted_target="%2"):
        # The op unzooms the palace window and walks off to another window.
        adapter.run("select-pane", "-t", "%1", allow_failure=True)
        adapter.run("select-window", "-t", "main:2", allow_failure=True)

    assert server.pane == "%1"
    assert server.window == "main:1"
    assert "main:1" in server.zoomed_windows, "zoom must survive the restore"


def test_preserve_focus_cedes_to_human_input_even_when_op_displaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt + suspenders: client_activity advanced past the snapshot means the
    human acted mid-op — never fight them for the camera."""
    server = FakeTmuxServer()
    server.patch_into(monkeypatch)
    adapter = TmuxAdapter(tmux_binary="tmux")

    with preserve_focus(adapter, source="assert-instance", attempted_target="%2"):
        adapter.run("select-window", "-t", "main:2", allow_failure=True)
        server.human_moves_to("%2")

    assert server.pane == "%2"
    assert server.window == "main:2"
    events = _general_log_events()
    assert any(event["event"] == "restore-ceded" for event in events)
    assert not any(event["event"] == "restored" for event in events)


def test_conftest_redirects_focus_logs_away_from_live_tmp() -> None:
    """The pollution regression: test-suite fake-adapter events must never land
    in the live /tmp focus logs."""
    redirected = os.environ["IMPERIUM_TMUX_FOCUS_LOG"]
    assert redirected != "/tmp/tmux-focus-guard.log"
    unique = f"sentinel-{id(object())}"

    focus_guard._log("restored", action=unique)

    live = pathlib.Path("/tmp/tmux-focus-guard.log")
    if live.exists():
        assert unique not in live.read_text()
    assert unique in pathlib.Path(redirected).read_text()


def test_focus_log_paths_resolve_env_at_call_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    target = tmp_path / "redirected-after-import.log"
    monkeypatch.setenv("IMPERIUM_TMUX_FOCUS_LOG", str(target))
    monkeypatch.setenv("IMPERIUM_MECHANICUS_FOCUS_LOG", str(target))

    focus_guard._log("hook-bounced", action="test")

    assert target.exists()
    assert "hook-bounced" in target.read_text()


def test_general_events_skip_the_mechanicus_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    general = tmp_path / "general.log"
    mech = tmp_path / "mech.log"
    monkeypatch.setenv("IMPERIUM_TMUX_FOCUS_LOG", str(general))
    monkeypatch.setenv("IMPERIUM_MECHANICUS_FOCUS_LOG", str(mech))

    focus_guard._log("restored", action="restored")
    focus_guard._log("wrapper-blocked", action="blocked")

    general_text = general.read_text()
    assert "restored" in general_text and "wrapper-blocked" in general_text
    mech_text = mech.read_text()
    assert "wrapper-blocked" in mech_text
    assert "restored" not in mech_text


def test_focus_log_rolls_over_past_size_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    log = tmp_path / "general.log"
    monkeypatch.setenv("IMPERIUM_TMUX_FOCUS_LOG", str(log))
    monkeypatch.setenv("IMPERIUM_MECHANICUS_FOCUS_LOG", str(log))
    log.write_bytes(b"x" * (focus_guard._LOG_ROLLOVER_BYTES + 1))

    focus_guard._log("restored", action="restored")

    rolled = log.with_name(log.name + ".1")
    assert rolled.exists() and rolled.stat().st_size > focus_guard._LOG_ROLLOVER_BYTES
    assert "restored" in log.read_text()


def test_preserve_focus_restore_failure_does_not_mask_body_exception() -> None:
    class FailingRestoreAdapter(FakeFocusAdapter):
        fail_restore = False

        def run(self, *args: str, allow_failure: bool = False) -> str:
            if self.fail_restore and args[0] == "display-message":
                raise OSError(24, "Too many open files")
            return super().run(*args, allow_failure=allow_failure)

    adapter = FailingRestoreAdapter()

    with pytest.raises(RuntimeError, match="real failure"):
        with preserve_focus(adapter, source="test", attempted_target="%2"):
            adapter.run("select-window", "-t", "main:2")
            adapter.fail_restore = True
            raise RuntimeError("real failure")
