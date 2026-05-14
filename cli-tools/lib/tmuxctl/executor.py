from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import replace

from .builder import build_workspace
from .enums import AttachmentClass, RestartPhase
from .models import (
    RestartAction,
    RestartExecutionResult,
    RestartPlan,
    ResumeResult,
    WorkspaceSnapshot,
)
from .normalize import normalize_window
from .revert import cleanup_transient_windows, is_transient_window_name
from .snapshot import build_workspace_snapshot
from .tmux_adapter import TmuxAdapter
from .tombstone import install_tombstone

RESUME_FAILURE_PATTERNS = ("no conversation found", "error", "not found", "enoent")
CONTINUATION_PROMPT = "continue where you left off"


class RestartExecutor:
    def __init__(self, adapter: TmuxAdapter | None = None) -> None:
        self.adapter = adapter or TmuxAdapter()

    def dry_run(self, plan: RestartPlan) -> RestartExecutionResult:
        return RestartExecutionResult(
            session_name=plan.session_name,
            phase=RestartPhase.CAPTURE,
            plan=plan,
            actions=tuple(self._planned_actions(plan)),
            coherence_issues=plan.coherence_issues,
        )

    def execute(self, plan: RestartPlan) -> RestartExecutionResult:
        actions: list[RestartAction] = list(self._planned_actions(plan))
        if plan.has_errors:
            return RestartExecutionResult(
                session_name=plan.session_name,
                phase=RestartPhase.COHERENCE_CHECK,
                plan=plan,
                actions=tuple(actions),
                coherence_issues=plan.coherence_issues,
                postcondition_violations=("critical coherence errors present before teardown",),
            )

        parked = 0
        detached = 0
        restored = 0
        recreated = 0
        resume_results: list[ResumeResult] = []

        holding_session = "_tmuxctl_restart"
        self.adapter.run(
            "new-session", "-d", "-s", holding_session, "-x", "80", "-y", "24", allow_failure=True
        )
        for attachment in plan.client_attachments:
            if attachment.attachment_class in {
                AttachmentClass.REMOTE_LEADER,
                AttachmentClass.REMOTE_GROUPED,
            }:
                self.adapter.run("detach-client", "-t", attachment.client_tty, allow_failure=True)
                detached += 1
            else:
                self.adapter.run(
                    "switch-client",
                    "-c",
                    attachment.client_tty,
                    "-t",
                    holding_session,
                    allow_failure=True,
                )
                parked += 1

        grouped_sessions = [
            s for s in plan.grouped_sessions if s.session_name != s.leader_session_name
        ]
        for grouped in grouped_sessions:
            self.adapter.run("kill-session", "-t", grouped.session_name, allow_failure=True)
        self.adapter.run("kill-session", "-t", plan.session_name, allow_failure=True)

        build_workspace(self.adapter, plan.session_name)
        time.sleep(0.5)

        rebuilt = build_workspace_snapshot(self.adapter, plan.session_name)
        for window in rebuilt.windows:
            try:
                normalize_window(self.adapter, plan.session_name, window.window_index)
            except Exception:
                continue
        self._clear_transient_windows(plan.session_name)
        time.sleep(0.2)
        rebuilt = build_workspace_snapshot(self.adapter, plan.session_name)
        pane_by_label = {
            pane.pane_role: pane.pane_id for pane in rebuilt.iter_panes() if pane.pane_role
        }

        for resume in plan.resumes:
            target_pane_id = resume.target_pane_id or pane_by_label.get(resume.pane_label, "")
            if not target_pane_id:
                resume_results.append(
                    ResumeResult(
                        instance_id=resume.instance_id,
                        pane_label=resume.pane_label,
                        target_pane_id="",
                        disposition=resume.disposition,
                        success=False,
                        message="target pane missing after rebuild",
                    )
                )
                continue

            if resume.tombstone_role and resume.tombstone_role != resume.pane_label:
                source_pane_id = pane_by_label.get(resume.tombstone_role, "")
                if source_pane_id and source_pane_id != target_pane_id:
                    self._install_tombstone(source_pane_id, resume.tombstone_role, target_pane_id)

            command = self.adapter.run(
                "display-message",
                "-t",
                target_pane_id,
                "-p",
                "#{pane_current_command}",
                allow_failure=True,
            ).strip()
            if "claude" in command:
                resume_results.append(
                    ResumeResult(
                        instance_id=resume.instance_id,
                        pane_label=resume.pane_label,
                        target_pane_id=target_pane_id,
                        disposition=resume.disposition,
                        success=False,
                        message="target pane already busy",
                    )
                )
                continue

            localized_dir = self._localize_path(resume.working_dir)
            self.adapter.send_keys(
                target_pane_id,
                f"cd {shlex.quote(localized_dir or '$HOME')} && dispatch --id {shlex.quote(resume.instance_id)} --pane {shlex.quote(target_pane_id)}",
                "Enter",
            )
            time.sleep(1.5)
            pane_output = self.adapter.capture_pane(target_pane_id, lines=8).lower()
            if any(pattern in pane_output for pattern in RESUME_FAILURE_PATTERNS):
                self.adapter.send_keys(target_pane_id, "C-c")
                time.sleep(0.2)
                self.adapter.send_keys(target_pane_id, "C-c")
                resume_results.append(
                    ResumeResult(
                        instance_id=resume.instance_id,
                        pane_label=resume.pane_label,
                        target_pane_id=target_pane_id,
                        disposition=resume.disposition,
                        success=False,
                        message="resume validation failed",
                    )
                )
                continue

            if resume.disposition.value == "resume_and_continue":
                time.sleep(2.0)
                self.adapter.send_text_then_submit(target_pane_id, CONTINUATION_PROMPT)
            resume_results.append(
                ResumeResult(
                    instance_id=resume.instance_id,
                    pane_label=resume.pane_label,
                    target_pane_id=target_pane_id,
                    disposition=resume.disposition,
                    success=True,
                    message="resumed",
                )
            )

        for grouped in grouped_sessions:
            self.adapter.run(
                "new-session",
                "-d",
                "-t",
                plan.session_name,
                "-s",
                grouped.session_name,
                allow_failure=True,
            )
            if grouped.selected_window_name:
                self.adapter.run(
                    "select-window",
                    "-t",
                    f"{grouped.session_name}:{grouped.selected_window_name}",
                    allow_failure=True,
                )
            recreated += 1

        for attachment in plan.client_attachments:
            target_session = attachment.session_name
            self.adapter.run(
                "switch-client",
                "-c",
                attachment.client_tty,
                "-t",
                target_session,
                allow_failure=True,
            )
            restored += 1

        self.adapter.run("kill-session", "-t", holding_session, allow_failure=True)

        verification = self._verify(plan, rebuilt, resume_results)
        return RestartExecutionResult(
            session_name=plan.session_name,
            phase=RestartPhase.COMPLETE if not verification else RestartPhase.VERIFY,
            plan=replace(plan, phase=RestartPhase.COMPLETE),
            actions=tuple(actions),
            resume_results=tuple(resume_results),
            coherence_issues=plan.coherence_issues,
            postcondition_violations=tuple(verification),
            clients_parked=parked,
            clients_detached=detached,
            clients_restored=restored,
            grouped_sessions_recreated=recreated,
        )

    def _localize_path(self, path: str) -> str:
        imperium = subprocess.run(
            ["bash", "-lc", "printf '%s' \"${IMPERIUM:-}\""],
            text=True,
            capture_output=True,
            check=False,
        ).stdout.strip()
        if not imperium:
            return path
        for prefix in ("/Volumes/Imperium", "/mnt/imperium"):
            if path.startswith(prefix) and prefix != imperium:
                return imperium + path[len(prefix) :]
        return path

    def _install_tombstone(self, pane_id: str, source_role: str, target_pane_id: str) -> None:
        install_tombstone(self.adapter, pane_id, source_role, target_pane_id)

    def _verify(
        self,
        plan: RestartPlan,
        rebuilt: WorkspaceSnapshot,
        resume_results: list[ResumeResult],
    ) -> list[str]:
        violations: list[str] = []
        pane_labels = {pane.pane_role for pane in rebuilt.iter_panes() if pane.pane_role}
        for resume in plan.resumes:
            if resume.pane_label not in pane_labels:
                violations.append(f"expected pane label missing after rebuild: {resume.pane_label}")
        for result in resume_results:
            if not result.success:
                violations.append(f"resume failed for {result.pane_label}: {result.message}")
        for window in rebuilt.windows:
            if is_transient_window_name(window.window_name):
                violations.append(f"transient stash window survived rebuild: {window.window_name}")
            for warning in window.warnings:
                if "missing" in warning or "duplicate" in warning:
                    violations.append(f"{window.target}: {warning}")
        return violations

    def _clear_transient_windows(self, session_name: str) -> None:
        cleanup_transient_windows(self.adapter, session_name)

    def _planned_actions(self, plan: RestartPlan) -> list[RestartAction]:
        actions: list[RestartAction] = []
        actions.append(
            RestartAction(
                RestartPhase.CAPTURE,
                "freeze workspace, grouped sessions, clients, and registry inputs",
            )
        )
        for attachment in plan.client_attachments:
            verb = "detach" if attachment.is_remote else "park"
            actions.append(
                RestartAction(
                    RestartPhase.TEARDOWN,
                    f"{verb} client {attachment.client_tty} ({attachment.attachment_class.value})",
                )
            )
        for grouped in plan.grouped_sessions:
            if grouped.session_name != grouped.leader_session_name:
                actions.append(
                    RestartAction(
                        RestartPhase.TEARDOWN,
                        f"kill grouped session {grouped.session_name}",
                    )
                )
        actions.append(
            RestartAction(RestartPhase.TEARDOWN, f"kill leader session {plan.session_name}")
        )
        actions.append(RestartAction(RestartPhase.REBUILD, "recreate workspace via builder.build_workspace"))
        actions.append(
            RestartAction(RestartPhase.REBUILD, "normalize managed windows before restore")
        )
        actions.append(RestartAction(RestartPhase.REBUILD, "clear transient stash windows"))
        for resume in plan.resumes:
            target = resume.target_pane_id or f"{resume.pane_label} (resolve post-rebuild)"
            actions.append(
                RestartAction(
                    RestartPhase.RESTORE,
                    f"resume {resume.instance_id[:8]} into {target} with {resume.disposition.value}",
                )
            )
        for grouped in plan.grouped_sessions:
            if grouped.session_name != grouped.leader_session_name:
                actions.append(
                    RestartAction(
                        RestartPhase.RESTORE,
                        f"recreate grouped session {grouped.session_name} on {grouped.selected_window_name or grouped.selected_window_index}",
                    )
                )
        actions.append(RestartAction(RestartPhase.VERIFY, "verify pane labels and resume outcomes"))
        return actions
