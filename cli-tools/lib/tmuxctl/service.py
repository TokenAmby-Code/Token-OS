from __future__ import annotations

from .api import (
    RegistryError,
    build_client_attachments,
    fetch_instance_registry,
    fetch_session_doc_for_pane_label,
)
from .audience import audience_return, audience_toggle
from .builder import (
    PALACE_WINDOW,
    SESSION_NAME,
    SOMNIUM_WINDOW,
    build_palace_window,
    build_somnium_window,
    build_workspace,
)
from .enums import CoherenceSeverity
from .executor import RestartExecutor
from .focus import focus_window
from .inspect import (
    render_doctor,
    render_pane,
    render_restart_plan,
    render_restart_result,
    render_window,
    render_workspace,
)
from .labels import canonical_pane_role
from .models import CoherenceIssue, GroupedSessionSnapshot, InstanceRegistrySnapshot
from .normalize import normalize_window
from .planner import build_restart_plan
from .resolver import (
    list_free_panes,
    resolve_instance,
    resolve_pane,
    resolve_to_physical,
    resolve_to_public,
)
from .skill_invoke import (
    insert_text,
    invoke_skill_in_pane,
    move_to_prompt_end,
    move_to_prompt_start,
    send_skill_invocation_to_pane,
)
from .snapshot import build_window_snapshot, build_workspace_snapshot
from .tmux_adapter import TmuxAdapter
from .tombstone import install_tombstone, jump_tombstone


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

    def doctor(self, session_name: str) -> str:
        return render_doctor(build_workspace_snapshot(self.adapter, session_name))

    def build_restart_plan(self, session_name: str):
        workspace = build_workspace_snapshot(self.adapter, session_name)
        registry_error = ""
        try:
            registry = fetch_instance_registry()
        except RegistryError as exc:
            registry_error = str(exc)
            registry = InstanceRegistrySnapshot(device_id="", instances=())
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
        if registry_error:
            plan = plan.__class__(
                session_name=plan.session_name,
                phase=plan.phase,
                resumes=plan.resumes,
                skipped=plan.skipped,
                client_attachments=plan.client_attachments,
                grouped_sessions=plan.grouped_sessions,
                coherence_issues=(
                    *plan.coherence_issues,
                    CoherenceIssue(
                        severity=CoherenceSeverity.WARNING,
                        code="registry_unavailable",
                        message=f"instance registry unavailable; restoring from live tmux snapshot only: {registry_error}",
                    ),
                ),
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

    def focus(self, session_name: str, window_index: int, mode: str) -> str:
        return focus_window(self.adapter, session_name, window_index, mode)

    def resolve_pane(self, target: str) -> str:
        resolved = resolve_pane(self.adapter, target)
        from .skill_invoke import resolve_agent_for_pane

        public_id = canonical_pane_role(resolved.pane_role) if resolved.pane_role else ""
        chain = " -> ".join(resolved.chain)
        try:
            agent = resolve_agent_for_pane(self.adapter, resolved.pane_id, default="auto")
        except Exception:
            agent = "auto"
        lines = [
            f"requested: {resolved.requested}",
            f"pane_id: {public_id or '(unset)'}",
            f"role: {resolved.pane_role or '(unset)'}",
            f"kind: {resolved.pane_kind.value}",
            f"agent: {agent}",
            f"live_agent: {str(agent != 'auto').lower()}",
        ]
        if chain:
            lines.append(f"chain: {chain}")
        return "\n".join(lines)

    def resolve_instance(self, instance_id: str) -> dict:
        """Resolve an instance UUID to its live pane (pure tmux, fail-closed).

        Returns ``{instance_id, pane_id, pane_role, found, agent, live_agent}``.
        When no live pane carries the stamp, ``found`` is False,
        ``pane_id``/``pane_role`` are empty strings, ``agent`` is ``auto``, and
        ``live_agent`` is False.
        """
        resolved = resolve_instance(self.adapter, instance_id)
        agent = "auto"
        if resolved.pane_id:
            from .skill_invoke import resolve_agent_for_pane

            try:
                agent = resolve_agent_for_pane(self.adapter, resolved.pane_id, default="auto")
            except Exception:
                agent = "auto"
        return {
            "instance_id": resolved.instance_id,
            "pane_id": resolved.pane_id or "",
            "pane_role": resolved.pane_role or "",
            "found": resolved.found,
            "agent": agent,
            "live_agent": agent != "auto",
        }

    def freelist(self) -> list[dict]:
        """List the clean, agent-free panes (the freelist).

        Purely derived from the live ``@PANE_CLEAN`` stamps — no stored state.
        Each entry is ``{pane_id, pane_role, window_name}``.
        """
        return [
            {
                "pane_id": p.pane_id,
                "pane_role": p.pane_role or "",
                "window_name": p.window_name,
            }
            for p in list_free_panes(self.adapter)
        ]

    def cardinal_pane_label(self, target: str) -> str:
        """Resolve a target to its stable cardinal @PANE_ID label.

        Raw tmux %pane ids are intentionally not returned. Callers that need a
        durable identity should use cardinal pane labels only.
        """
        if target == "current":
            target = self.adapter.run("display-message", "-p", "#{@PANE_ID}").strip()
        if not target:
            raise ValueError("current pane has no cardinal @PANE_ID")
        if target.startswith("%"):
            raise ValueError("raw tmux %pane ids are not valid cardinal ids")
        return resolve_to_public(self.adapter, target)

    def physical_pane_id(self, target: str) -> str:
        return resolve_to_physical(self.adapter, target)

    def public_pane_id(self, target: str) -> str:
        return resolve_to_public(self.adapter, target)

    def session_doc_for_pane(self, target: str) -> dict:
        pane_label = self.cardinal_pane_label(target)
        return fetch_session_doc_for_pane_label(pane_label)

    def invoke_skill(
        self,
        target: str,
        skill: str,
        *,
        agent: str = "auto",
        arguments: str | None = None,
    ) -> str:
        return invoke_skill_in_pane(self.adapter, target, skill, agent=agent, arguments=arguments)

    def send_skill(
        self,
        target: str,
        skill: str,
        *,
        agent: str = "auto",
        arguments: str | None = None,
        clear_prompt: bool = False,
    ) -> str:
        return send_skill_invocation_to_pane(
            self.adapter,
            target,
            skill,
            agent=agent,
            arguments=arguments,
            clear_prompt=clear_prompt,
        )

    def move_to_prompt_start(self, target: str, *, page_ups: int = 50) -> None:
        move_to_prompt_start(self.adapter, target, page_ups=page_ups)

    def insert_text(self, target: str, text: str) -> None:
        insert_text(self.adapter, target, text)

    def move_to_prompt_end(self, target: str, *, page_downs: int = 50) -> None:
        move_to_prompt_end(self.adapter, target, page_downs=page_downs)

    def audience_toggle(self, target: str, *, client: str = "") -> str:
        return audience_toggle(self.adapter, target, client=client)

    def audience_return(self, target: str, *, client: str = "") -> str:
        return audience_return(self.adapter, target, client=client)

    def tombstone_jump(self, target: str, *, client: str = "") -> str:
        return jump_tombstone(self.adapter, target, client=client)

    def tombstone_install(self, slot_pane: str, source_role: str, target_pane: str) -> str:
        return install_tombstone(self.adapter, slot_pane, source_role, target_pane)

    def create_workspace(self, session_name: str = SESSION_NAME) -> str:
        if self.adapter.has_session(session_name):
            return f"session '{session_name}' already exists"
        build_workspace(self.adapter, session_name)
        return f"created workspace '{session_name}'"

    def rebuild_window(self, session_name: str, window_index: int) -> str:
        target = f"{session_name}:{window_index}"
        panes = self.adapter.list_panes(target)
        if not panes:
            raise ValueError(f"window has no panes: {target}")

        window_name_raw = panes[0]["window_name"]
        window_base = window_name_raw.split("(", 1)[0]
        if window_base == PALACE_WINDOW:
            builder = build_palace_window
        elif window_base == SOMNIUM_WINDOW:
            builder = build_somnium_window
        else:
            raise ValueError(
                f"rebuild-window supports palace and somnium archetypes (got '{window_base}')"
            )

        survivor = panes[0]["pane_id"]
        for record in panes[1:]:
            self.adapter.run("kill-pane", "-t", record["pane_id"], allow_failure=True)

        self.adapter.run("respawn-pane", "-k", "-t", survivor, allow_failure=True)
        for opt in ("@PANE_ID", "@GRID_STATE", "@PANE_TYPE", "@GRID_RESERVED"):
            self.adapter.run("set-option", "-pu", "-t", survivor, opt, allow_failure=True)

        builder(self.adapter, session_name, window_name_raw)
        return f"rebuilt {target}"

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
