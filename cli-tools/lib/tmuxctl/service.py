from __future__ import annotations

from .api import build_client_attachments, fetch_instance_registry
from .executor import RestartExecutor
from .inspect import (
    render_pane,
    render_restart_plan,
    render_restart_result,
    render_window,
    render_workspace,
)
from .planner import build_restart_plan
from .models import GroupedSessionSnapshot
from .normalize import normalize_window
from .snapshot import build_window_snapshot, build_workspace_snapshot
from .tmux_adapter import TmuxAdapter


class TmuxControlPlane:
    """Read-first control-plane service for the managed workspace."""

    def __init__(self, adapter: TmuxAdapter | None = None) -> None:
        self.adapter = adapter or TmuxAdapter()

    def inspect_workspace(self, session_name: str) -> str:
        return render_workspace(build_workspace_snapshot(self.adapter, session_name))

    def inspect_window(self, session_name: str, window_index: int) -> str:
        return render_window(build_window_snapshot(self.adapter, session_name, window_index))

    def inspect_pane(self, pane_id: str) -> str:
        pane_target = self.adapter.run(
            "display-message",
            "-t",
            pane_id,
            "-p",
            "#{session_name}\t#{window_index}\t#{pane_id}",
        ).strip()
        session_name, window_index, resolved_pane_id = pane_target.split("\t")
        window = build_window_snapshot(self.adapter, session_name, int(window_index))
        for pane in window.panes:
            if pane.pane_id == resolved_pane_id:
                return render_pane(pane)
        raise ValueError(f"pane not found in snapshot: {pane_id}")

    def inspect_restart_plan(self, session_name: str) -> str:
        plan = self.build_restart_plan(session_name)
        return render_restart_plan(plan)

    def build_restart_plan(self, session_name: str):
        workspace = build_workspace_snapshot(self.adapter, session_name)
        registry = fetch_instance_registry()
        grouped_sessions = self._grouped_sessions(session_name)
        attachments = build_client_attachments(
            self.adapter.list_clients(),
            managed_sessions=grouped_sessions,
        )
        plan = build_restart_plan(
            workspace,
            registry,
            client_attachments=attachments,
            grouped_sessions=grouped_sessions,
        )
        return plan

    def dry_run_restart(self, session_name: str) -> str:
        plan = self.build_restart_plan(session_name)
        return render_restart_result(RestartExecutor(self.adapter).dry_run(plan))

    def execute_restart(self, session_name: str) -> tuple[str, bool]:
        plan = self.build_restart_plan(session_name)
        result = RestartExecutor(self.adapter).execute(plan)
        return render_restart_result(result), result.is_success

    def normalize(self, session_name: str, window_index: int) -> str:
        return normalize_window(self.adapter, session_name, window_index)

    def _grouped_sessions(self, leader_session_name: str) -> tuple[GroupedSessionSnapshot, ...]:
        sessions = []
        for row in self.adapter.list_sessions():
            row_leader = row["session_group"] or row["session_name"]
            if row_leader != leader_session_name:
                continue
            sessions.append(
                GroupedSessionSnapshot(
                    session_name=row["session_name"],
                    leader_session_name=row_leader,
                    selected_window_index=int(row["window_index"]),
                    selected_window_name=row["window_name"],
                )
            )
        if not sessions:
            sessions.append(
                GroupedSessionSnapshot(
                    session_name=leader_session_name,
                    leader_session_name=leader_session_name,
                    selected_window_index=0,
                    selected_window_name="",
                )
            )
        return tuple(sessions)
