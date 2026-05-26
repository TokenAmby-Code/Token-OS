from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .tmux_adapter import TmuxAdapter

LOG_PATH = Path(os.environ.get("IMPERIUM_MECHANICUS_FOCUS_LOG", "/tmp/mechanicus-focus-guard.log"))
GENERAL_LOG_PATH = Path(os.environ.get("IMPERIUM_TMUX_FOCUS_LOG", "/tmp/tmux-focus-guard.log"))
ALLOW_UNTIL_OPTION = "@IMPERIUM_ALLOW_MECHANICUS_FOCUS_UNTIL"
ALLOW_REASON_OPTION = "@IMPERIUM_ALLOW_MECHANICUS_FOCUS_REASON"
HUMAN_FOCUS_CLIENT_OPTION = "@IMPERIUM_HUMAN_MECHANICUS_FOCUS_CLIENT"
HUMAN_FOCUS_REASON_OPTION = "@IMPERIUM_HUMAN_MECHANICUS_FOCUS_REASON"
LAST_NON_MECH_OPTION = "@IMPERIUM_LAST_NON_MECHANICUS_PANE"
DEFAULT_ALLOW_SECONDS = 4.0
RESTORE_ENV = "IMPERIUM_TMUX_FOCUS_RESTORE"


@dataclass(frozen=True)
class FocusSnapshot:
    window: str = ""
    pane: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.window and self.pane)


def _clean_window_name(raw: str) -> str:
    return (raw or "").strip().split("(", 1)[0]


def _write_log(path: Path, payload: dict[str, object]) -> None:
    try:
        with path.open("a") as fh:
            fh.write(json.dumps(payload, sort_keys=True) + "\n")
    except Exception:
        pass


def _log(event: str, **fields: object) -> None:
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        **fields,
    }
    _write_log(LOG_PATH, payload)
    if GENERAL_LOG_PATH != LOG_PATH:
        _write_log(GENERAL_LOG_PATH, payload)


def capture_focus(adapter: "TmuxAdapter") -> FocusSnapshot:
    raw = adapter.run(
        "display-message",
        "-p",
        "#{session_name}:#{window_index}\t#{pane_id}",
        allow_failure=True,
    ).strip()
    if "\t" in raw:
        window, pane = raw.split("\t", 1)
        return FocusSnapshot(window=window.strip(), pane=pane.strip())
    # Older test fakes often only understand the individual formats.
    window = adapter.run(
        "display-message", "-p", "#{session_name}:#{window_index}", allow_failure=True
    ).strip()
    pane = adapter.run("display-message", "-p", "#{pane_id}", allow_failure=True).strip()
    return FocusSnapshot(window=window, pane=pane)


def _focus_exists(adapter: "TmuxAdapter", snapshot: FocusSnapshot) -> bool:
    if not snapshot.pane:
        return False
    return bool(
        adapter.run(
            "display-message",
            "-t",
            snapshot.pane,
            "-p",
            "#{pane_id}",
            allow_failure=True,
        ).strip()
    )


def restore_focus(
    adapter: "TmuxAdapter",
    snapshot: FocusSnapshot,
    *,
    source: str,
    attempted_target: str = "",
    previous: FocusSnapshot | None = None,
) -> bool:
    """Restore the client camera captured by ``capture_focus``.

    This is for automation paths only. Human navigation should call the normal
    tmux selection commands directly.
    """
    if not snapshot.ok:
        return False
    before_restore = capture_focus(adapter)
    if before_restore == snapshot:
        return False
    if not _focus_exists(adapter, snapshot):
        _log(
            "restore-skipped",
            action="missing_previous",
            command_surface=source,
            attempted_target=attempted_target,
            previous_window=snapshot.window,
            previous_pane=snapshot.pane,
            final_window=before_restore.window,
            final_pane=before_restore.pane,
        )
        return False

    old_restore = os.environ.get(RESTORE_ENV)
    os.environ[RESTORE_ENV] = "1"
    try:
        adapter.run("select-window", "-t", snapshot.window, allow_failure=True)
        adapter.run("select-pane", "-t", snapshot.pane, allow_failure=True)
    finally:
        if old_restore is None:
            os.environ.pop(RESTORE_ENV, None)
        else:
            os.environ[RESTORE_ENV] = old_restore
    final = capture_focus(adapter)
    restored = final == snapshot
    _log(
        "restored" if restored else "restore-failed",
        action="restored" if restored else "restore_failed",
        command_surface=source,
        attempted_target=attempted_target,
        previous_window=(previous.window if previous else snapshot.window),
        previous_pane=(previous.pane if previous else snapshot.pane),
        restore_window=snapshot.window,
        restore_pane=snapshot.pane,
        displaced_window=before_restore.window,
        displaced_pane=before_restore.pane,
        final_window=final.window,
        final_pane=final.pane,
    )
    return restored


@contextmanager
def preserve_focus(
    adapter: "TmuxAdapter",
    *,
    source: str,
    attempted_target: str = "",
    enabled: bool = True,
) -> Iterator[FocusSnapshot]:
    snapshot = capture_focus(adapter) if enabled else FocusSnapshot()
    try:
        yield snapshot
    finally:
        if enabled:
            restore_focus(
                adapter,
                snapshot,
                source=source,
                attempted_target=attempted_target,
                previous=snapshot,
            )


def show_global_option(adapter: "TmuxAdapter", option: str) -> str:
    return adapter.run("show-options", "-gqv", option, allow_failure=True).strip()


def set_global_option(adapter: "TmuxAdapter", option: str, value: str) -> None:
    adapter.run("set-option", "-g", option, value, allow_failure=True)


def unset_global_option(adapter: "TmuxAdapter", option: str) -> None:
    adapter.run("set-option", "-gu", option, allow_failure=True)


def target_window_name(adapter: "TmuxAdapter", target: str) -> str:
    if not target:
        return ""
    # Fast path for stable logical/window targets.
    tail = target.rsplit(":", 1)[-1].split(".", 1)[0]
    if tail.startswith("mechanicus"):
        return tail
    return _clean_window_name(
        adapter.run("display-message", "-t", target, "-p", "#{window_name}", allow_failure=True)
    )


def target_is_mechanicus(adapter: "TmuxAdapter", target: str) -> bool:
    if not target:
        return False
    if target.startswith("mechanicus:"):
        return True
    return target_window_name(adapter, target).startswith("mechanicus")


def current_pane(adapter: "TmuxAdapter") -> str:
    return adapter.run("display-message", "-p", "#{pane_id}", allow_failure=True).strip()


def current_client(adapter: "TmuxAdapter") -> str:
    return adapter.run("display-message", "-p", "#{client_tty}", allow_failure=True).strip()


def allow_temporarily(
    adapter: "TmuxAdapter",
    *,
    seconds: float = DEFAULT_ALLOW_SECONDS,
    reason: str = "explicit",
    actor: str = "",
) -> float:
    until = time.time() + max(seconds, 0.5)
    set_global_option(adapter, ALLOW_UNTIL_OPTION, f"{until:.3f}")
    set_global_option(adapter, ALLOW_REASON_OPTION, reason)
    _log("allowed-window-opened", reason=reason, actor=actor, until=until)
    return until


def allow_human_focus(
    adapter: "TmuxAdapter",
    *,
    client: str = "",
    reason: str = "explicit-human-navigation",
    actor: str = "",
) -> None:
    """Mark the current client as intentionally navigating into mechanicus.

    This is not time based. UI bindings set it immediately before a human
    select-window/select-pane. The hook allows mechanicus focus for that client
    until the client selects a non-mechanicus pane/window, at which point the
    marker is cleared.
    """
    client = client or current_client(adapter)
    set_global_option(adapter, HUMAN_FOCUS_CLIENT_OPTION, client or "*")
    set_global_option(adapter, HUMAN_FOCUS_REASON_OPTION, reason)
    _log(
        "human-focus-opened",
        action="allowed",
        reason=reason,
        actor=actor,
        current_client=client,
    )


def clear_human_focus(adapter: "TmuxAdapter", *, client: str = "", reason: str = "") -> None:
    stored_client = show_global_option(adapter, HUMAN_FOCUS_CLIENT_OPTION)
    if not stored_client:
        return
    if client and stored_client not in {"*", client}:
        return
    unset_global_option(adapter, HUMAN_FOCUS_CLIENT_OPTION)
    unset_global_option(adapter, HUMAN_FOCUS_REASON_OPTION)
    _log("human-focus-cleared", action="cleared", current_client=client, reason=reason)


def human_focus_active(adapter: "TmuxAdapter", *, client: str = "") -> bool:
    stored_client = show_global_option(adapter, HUMAN_FOCUS_CLIENT_OPTION)
    if not stored_client:
        return False
    return stored_client == "*" or not client or stored_client == client


def override_active(adapter: "TmuxAdapter") -> bool:
    if os.environ.get("IMPERIUM_ALLOW_MECHANICUS_FOCUS") == "1":
        return True
    raw = show_global_option(adapter, ALLOW_UNTIL_OPTION)
    if not raw:
        return False
    try:
        until = float(raw)
    except ValueError:
        unset_global_option(adapter, ALLOW_UNTIL_OPTION)
        unset_global_option(adapter, ALLOW_REASON_OPTION)
        return False
    if time.time() <= until:
        return True
    unset_global_option(adapter, ALLOW_UNTIL_OPTION)
    unset_global_option(adapter, ALLOW_REASON_OPTION)
    return False


def maybe_open_override_from_env(
    adapter: "TmuxAdapter",
    *,
    target: str,
    command: str,
    surface: str,
) -> bool:
    if os.environ.get("IMPERIUM_ALLOW_MECHANICUS_FOCUS") != "1":
        return False
    if not target_is_mechanicus(adapter, target):
        return False
    allow_temporarily(adapter, reason=f"env:{surface}:{command}", actor=surface)
    _log(
        "allowed",
        attempted_target=target,
        previous_pane=current_pane(adapter),
        current_client=current_client(adapter),
        command_surface=surface,
        command=command,
    )
    return True


def log_blocked(
    adapter: "TmuxAdapter",
    *,
    target: str,
    command: str,
    surface: str,
    argv: list[str] | tuple[str, ...],
) -> None:
    _log(
        "wrapper-blocked",
        action="blocked",
        attempted_target=target,
        previous_pane=current_pane(adapter),
        previous_window=adapter.run(
            "display-message",
            "-p",
            "#{session_name}:#{window_index}",
            allow_failure=True,
        ).strip(),
        current_client=current_client(adapter),
        command_surface=surface,
        command=command,
        argv=list(argv),
    )


def remember_or_bounce(
    adapter: "TmuxAdapter",
    *,
    pane: str = "",
    client: str = "",
    surface: str = "after-select",
) -> dict[str, object]:
    selected = current_pane(adapter) if pane in {"", "current"} else pane
    client = client or current_client(adapter)
    window = target_window_name(adapter, selected)
    if not window.startswith("mechanicus"):
        if selected:
            set_global_option(adapter, LAST_NON_MECH_OPTION, selected)
        clear_human_focus(adapter, client=client, reason=f"left-mechanicus:{surface}")
        return {"action": "remembered", "pane": selected, "window": window, "client": client}

    if human_focus_active(adapter, client=client) or override_active(adapter):
        _log(
            "allowed",
            attempted_target=selected,
            previous_pane=show_global_option(adapter, LAST_NON_MECH_OPTION),
            current_client=client,
            command_surface=surface,
            command="hook",
        )
        return {"action": "allowed", "pane": selected, "window": window, "client": client}

    session = adapter.run("display-message", "-p", "#{session_name}", allow_failure=True).strip()
    previous = ""
    for row in adapter.run(
        "list-windows",
        "-t",
        session or "",
        "-F",
        "#{window_index}\t#{window_name}\t#{window_last_flag}",
        allow_failure=True,
    ).splitlines():
        parts = row.split("\t", 2)
        if len(parts) != 3:
            continue
        index, raw_window, last_flag = parts
        if last_flag != "1" or _clean_window_name(raw_window).startswith("mechanicus"):
            continue
        for pane_row in adapter.run(
            "list-panes",
            "-t",
            f"{session}:{index}" if session else index,
            "-F",
            "#{pane_id}\t#{pane_active}",
            allow_failure=True,
        ).splitlines():
            if "\t" not in pane_row:
                continue
            candidate, active = pane_row.split("\t", 1)
            if active == "1":
                previous = candidate
                break
        if previous:
            break

    stored_previous = show_global_option(adapter, LAST_NON_MECH_OPTION)
    if not previous and stored_previous.startswith("%") and not target_is_mechanicus(adapter, stored_previous):
        previous = stored_previous

    if not previous.startswith("%") or target_is_mechanicus(adapter, previous):
        previous = ""
        fmt = "#{pane_id}\t#{session_name}\t#{window_name}"
        for line in adapter.run("list-panes", "-a", "-F", fmt, allow_failure=True).splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            candidate, candidate_session, raw_window = parts
            if session and candidate_session != session:
                continue
            if not _clean_window_name(raw_window).startswith("mechanicus"):
                previous = candidate
                break

    bounced = False
    if previous:
        prev_window = adapter.run(
            "display-message",
            "-t",
            previous,
            "-p",
            "#{session_name}:#{window_index}",
            allow_failure=True,
        ).strip()
        old_restore = os.environ.get(RESTORE_ENV)
        os.environ[RESTORE_ENV] = "1"
        try:
            if prev_window:
                adapter.run("select-window", "-t", prev_window, allow_failure=True)
            adapter.run("select-pane", "-t", previous, allow_failure=True)
        finally:
            if old_restore is None:
                os.environ.pop(RESTORE_ENV, None)
            else:
                os.environ[RESTORE_ENV] = old_restore
        bounced = True
    _log(
        "hook-bounced",
        attempted_target=selected,
        previous_pane=previous,
        current_client=client,
        command_surface=surface,
        command="hook",
        bounced=bounced,
    )
    return {
        "action": "bounced" if bounced else "blocked_no_previous",
        "pane": selected,
        "window": window,
        "previous": previous,
        "client": client,
    }
