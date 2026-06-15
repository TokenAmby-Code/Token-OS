from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from .builder import build_workspace
from .custodes import _pane_pid, pane_has_active_agent
from .enums import AttachmentClass, RestartPhase
from .models import (
    PlannedResume,
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

        holding_session = "_stash"
        self._create_holding_session(holding_session)
        try:
            for attachment in plan.client_attachments:
                if attachment.attachment_class in {
                    AttachmentClass.REMOTE_LEADER,
                    AttachmentClass.REMOTE_GROUPED,
                }:
                    self.adapter.run(
                        "detach-client", "-t", attachment.client_tty, allow_failure=True
                    )
                    detached += 1
                else:
                    self._switch_client(attachment.client_tty, holding_session)
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
            rebuilt_labels = {pane.pane_role for pane in rebuilt.iter_panes() if pane.pane_role}

            for resume in plan.resumes:
                target_pane_ref = self._resume_target_ref(resume)
                if not target_pane_ref:
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

                try:
                    if resume.pane_label and resume.pane_label not in rebuilt_labels:
                        raise ValueError(f"target label missing after rebuild: {resume.pane_label}")
                    if resume.tombstone_role and resume.tombstone_role != resume.pane_label:
                        self._install_tombstone(
                            resume.tombstone_role, resume.tombstone_role, target_pane_ref
                        )

                    command = (
                        self.adapter.run(
                            "display-message",
                            "-t",
                            target_pane_ref,
                            "-p",
                            "#{pane_current_command}",
                            allow_failure=True,
                        )
                        .strip()
                        .lower()
                    )
                    if any(agent in command for agent in ("claude", "codex", "node")):
                        resume_results.append(
                            ResumeResult(
                                instance_id=resume.instance_id,
                                pane_label=resume.pane_label,
                                target_pane_id=target_pane_ref,
                                disposition=resume.disposition,
                                success=False,
                                message="target pane already busy",
                            )
                        )
                        continue

                    pane_target = resume.pane_label or target_pane_ref
                    self.adapter.send_keys(
                        target_pane_ref,
                        f"dispatch --id {shlex.quote(resume.instance_id)} --pane {shlex.quote(pane_target)}",
                        "Enter",
                    )
                    time.sleep(1.5)
                    pane_output = self.adapter.capture_pane(target_pane_ref, lines=8).lower()
                    if any(pattern in pane_output for pattern in RESUME_FAILURE_PATTERNS):
                        self.adapter.send_keys(target_pane_ref, "C-c", allow_failure=True)
                        time.sleep(0.2)
                        self.adapter.send_keys(target_pane_ref, "C-c", allow_failure=True)
                        resume_results.append(
                            ResumeResult(
                                instance_id=resume.instance_id,
                                pane_label=resume.pane_label,
                                target_pane_id=target_pane_ref,
                                disposition=resume.disposition,
                                success=False,
                                message="resume validation failed",
                            )
                        )
                        continue

                    if resume.disposition.value == "resume_and_continue":
                        time.sleep(2.0)
                        self.adapter.send_text_then_submit(target_pane_ref, CONTINUATION_PROMPT)
                    resume_results.append(
                        ResumeResult(
                            instance_id=resume.instance_id,
                            pane_label=resume.pane_label,
                            target_pane_id=target_pane_ref,
                            disposition=resume.disposition,
                            success=True,
                            message="resumed",
                        )
                    )
                except Exception as exc:
                    resume_results.append(
                        ResumeResult(
                            instance_id=resume.instance_id,
                            pane_label=resume.pane_label,
                            target_pane_id=target_pane_ref,
                            disposition=resume.disposition,
                            success=False,
                            message=f"resume raised {type(exc).__name__}: {str(exc)[:160]}",
                        )
                    )
                    continue

            persistent_violations = self._assert_persistent_runtime_panes(plan.session_name)

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
                    self._run_focus_restore(
                        "select-window",
                        "-t",
                        f"{grouped.session_name}:{grouped.selected_window_name}",
                        allow_failure=True,
                    )
                recreated += 1

            for attachment in plan.client_attachments:
                target_session = attachment.session_name
                self._switch_client(attachment.client_tty, target_session)
                restored += 1

            verification = persistent_violations + self._verify(plan, rebuilt, resume_results)
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

        finally:
            self.adapter.run("kill-session", "-t", holding_session, allow_failure=True)

    def _resume_target_ref(self, resume: PlannedResume) -> str:
        """Return the public tmuxctl target for a planned resume.

        Restart execution must not carry volatile tmux ``%N`` ids across
        teardown. The durable pane label (for example ``palace:N`` or
        ``mechanicus:admin``) is the writer-facing target; ``TmuxAdapter`` then
        resolves that public target against the live rebuilt workspace at the
        last possible moment. ``target_pane_id`` should be empty for restart
        plans and is not used as a writer target.
        """
        return resume.pane_label or ""

    def _run_focus_restore(self, *args: str, allow_failure: bool = False) -> str:
        previous = os.environ.get("IMPERIUM_TMUX_FOCUS_RESTORE")
        os.environ["IMPERIUM_TMUX_FOCUS_RESTORE"] = "1"
        try:
            return self.adapter.run(*args, allow_failure=allow_failure)
        finally:
            if previous is None:
                os.environ.pop("IMPERIUM_TMUX_FOCUS_RESTORE", None)
            else:
                os.environ["IMPERIUM_TMUX_FOCUS_RESTORE"] = previous

    def _switch_client(self, client_tty: str, target_session: str) -> None:
        self._run_focus_restore(
            "switch-client",
            "-c",
            client_tty,
            "-t",
            target_session,
            allow_failure=True,
        )

    def _create_holding_session(self, holding_session: str) -> None:
        # Clean up failed pre-patch holding sessions and any stale stash before
        # creating the visible parking page for this restart.
        for stale in ("_tmuxctl_restart", holding_session):
            self.adapter.run("kill-session", "-t", stale, allow_failure=True)
        self.adapter.run(
            "new-session",
            "-d",
            "-s",
            holding_session,
            "-n",
            "_stash",
            "-x",
            "100",
            "-y",
            "30",
            self._holding_shell_command(),
        )
        if not self.adapter.has_session(holding_session):
            raise RuntimeError(f"failed to create holding session {holding_session}")
        self.adapter.run(
            "set-option",
            "-t",
            holding_session,
            "status-left",
            "#[bold] tx restart #[default]",
            allow_failure=True,
        )
        self.adapter.run(
            "set-option",
            "-t",
            holding_session,
            "status-right",
            "returning to main when rebuild completes",
            allow_failure=True,
        )

    def _holding_shell_command(self) -> str:
        script = r"""
clear
tput civis 2>/dev/null || true
trap 'tput cnorm 2>/dev/null || true; exit 0' INT TERM EXIT
cat <<'ART'

        __            __             __
  _____/ /____ ______/ /_     ____  / /_
 / ___/ __/ _ `/ ___/ __ \   / __ \/ __/
(__  ) /_/  __/ /__/ / / /  / /_/ / /_
/____/\__/\_,_/\___/_/ /_/  \____/\__/

        tx restart is rebuilding main
        remain in _stash until returned

ART
frames='|/-\'
i=0
while :; do
  frame=${frames:$((i % 4)):1}
  width=34
  pos=$((i % (width + 1)))
  bar=''
  j=0
  while [ $j -lt $width ]; do
    if [ $j -lt $pos ]; then bar="${bar}#"; else bar="${bar}."; fi
    j=$((j + 1))
  done
  pct=$((pos * 100 / width))
  printf '\r  %s sealing construction zone [%s] %3d%%  ' "$frame" "$bar" "$pct"
  i=$((i + 1))
  sleep 0.12
done
""".strip()
        return f"bash -lc {shlex.quote(script)}"

    def _assert_persistent_runtime_panes(self, session_name: str) -> list[str]:
        """Best-effort post-rebuild repair for panes that should host daemons.

        The restart planner restores registry-backed instances. Some standing
        panes are intentionally hook-driven and may not have a fresh resumable
        registry row after teardown. Assert them after restore so `tx restart`
        leaves the workspace operational instead of with blank FG/Admin shells.

        The civic reservist pane is a special case: `civic-thread fallthrough`
        injects through tmux-resume, which requires an already-running agent TUI.
        If the reservist pane is only a shell, seed a low-cost idle Claude in the
        Civic working dir; the invariant can then deliver its activation prompt.
        """
        violations: list[str] = []

        try:
            from .assertions import assert_instance

            for pane_label in (
                "legion:custodes",
                "mechanicus:fabricator-general",
                "mechanicus:admin",
            ):
                try:
                    result = assert_instance(self.adapter, pane_label)
                except Exception as exc:
                    violations.append(f"persistent pane assertion failed for {pane_label}: {exc}")
                    continue
                if not result.get("ok") and result.get("action") not in {
                    "launched",
                    "persona_correction_sent",
                    "registry_reactivated",
                }:
                    violations.append(
                        f"persistent pane assertion failed for {pane_label}: "
                        f"{result.get('action') or 'none'} {result.get('reason') or ''}".strip()
                    )
        except Exception as exc:
            violations.append(f"persistent persona assertion unavailable: {exc}")

        for label, target, cwd, prompt in (
            (
                "civic reservist",
                "reservists:civic",
                Path(os.environ.get("CIVIC_THREAD_PATH", "/Volumes/Civic")),
                "Stand by as the civic reservist runtime. Do not start new work. "
                "Wait for civic-thread fallthrough or operator instructions.",
            ),
            (
                "Token-OS reservist",
                "reservists:slot",
                self._token_os_dir(),
                "Stand by as the Token-OS reservist runtime. Do not start new work. "
                "Wait for operator or orchestration instructions.",
            ),
        ):
            try:
                violation = self._ensure_reservist_runtime(label, target, cwd, prompt)
                if violation:
                    violations.append(violation)
            except Exception as exc:
                violations.append(f"{label} assertion failed: {exc}")

        return violations

    def _ensure_reservist_runtime(
        self, label: str, target: str, working_dir: Path, prompt: str
    ) -> str:
        pane_id = self._resolve_optional_pane(target)
        if not pane_id:
            return f"{label} pane missing after rebuild"
        if self._pane_has_agent_runtime(pane_id):
            return ""

        if not working_dir.is_dir():
            working_dir = Path.home()
        dispatch_bin = self._dispatch_binary()
        proc = subprocess.run(
            [
                dispatch_bin,
                "--direct",
                "--engine",
                "claude",
                "--model",
                "sonnet",
                "--pane",
                pane_id,
                "--dir",
                str(working_dir),
                "--instance-type",
                "hook_driven",
                "--no-gt",
                "--prompt",
                prompt,
            ],
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.strip()[:160]
            return f"{label} dispatch failed rc={proc.returncode}: {stderr}"
        time.sleep(1.0)
        if not self._pane_has_agent_runtime(pane_id):
            return f"{label} launch did not appear to start"
        return ""

    def _pane_has_agent_runtime(self, pane_id: str) -> bool:
        return pane_has_active_agent(_pane_pid(self.adapter, pane_id))

    def _token_os_dir(self) -> Path:
        imperium = os.environ.get("IMPERIUM")
        if imperium:
            return Path(imperium) / "runtimes" / "token-os" / "live"
        return Path(__file__).resolve().parents[3]

    def _resolve_optional_pane(self, target: str) -> str:
        try:
            from .resolver import resolve_pane

            return resolve_pane(self.adapter, target).pane_id
        except Exception:
            return ""

    def _dispatch_binary(self) -> str:
        candidate = subprocess.run(
            ["bash", "-lc", "command -v dispatch"],
            text=True,
            capture_output=True,
            check=False,
        ).stdout.strip()
        if candidate:
            return candidate
        return str(Path(__file__).resolve().parents[2] / "bin" / "dispatch")

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
        actions.append(
            RestartAction(RestartPhase.REBUILD, "recreate workspace via builder.build_workspace")
        )
        actions.append(
            RestartAction(RestartPhase.REBUILD, "normalize managed windows before restore")
        )
        actions.append(RestartAction(RestartPhase.REBUILD, "clear transient stash windows"))
        for resume in plan.resumes:
            target = resume.pane_label or resume.target_pane_id or "<unresolved>"
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
