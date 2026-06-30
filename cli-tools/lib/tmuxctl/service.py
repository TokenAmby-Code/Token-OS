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
from .models import (
    CoherenceIssue,
    GroupedSessionSnapshot,
    InstanceRegistrySnapshot,
    RestartPlan,
)
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
    insert_invocation_in_pane,
    insert_text,
    invoke_skill_in_pane,
    move_to_prompt_end,
    move_to_prompt_start,
    send_invocation_to_pane,
    send_skill_invocation_to_pane,
)
from .snapshot import build_window_snapshot, build_workspace_snapshot
from .tmux_adapter import TmuxAdapter
from .tombstone import install_tombstone, jump_tombstone


class TmuxControlPlane:
    """Read-first control-plane service for the managed workspace."""

    def __init__(self, adapter: TmuxAdapter | None = None) -> None:
        self.adapter = adapter or TmuxAdapter()

    def inspect_workspace(self, session_name: str, *, physical: bool = False) -> str:
        """Render the full workspace snapshot for a session as text."""
        return render_workspace(
            build_workspace_snapshot(self.adapter, session_name), physical=physical
        )

    def inspect_window(
        self, session_name: str, window_index: int, *, physical: bool = False
    ) -> str:
        """Render a single window's snapshot for a session as text."""
        return render_window(
            build_window_snapshot(self.adapter, session_name, window_index), physical=physical
        )

    def inspect_pane(self, pane_id: str, *, physical: bool = False) -> str:
        """Render a single pane's snapshot, located via its live window snapshot."""
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
                return render_pane(pane, physical=physical)
        raise ValueError(f"pane not found in snapshot: {pane_id}")

    def inspect_restart_plan(self, session_name: str) -> str:
        """Build and render the restart plan for a session as text."""
        plan = self.build_restart_plan(session_name)
        return render_restart_plan(plan)

    def doctor(self, session_name: str) -> str:
        """Render the workspace coherence/health report for a session."""
        return render_doctor(build_workspace_snapshot(self.adapter, session_name))

    def build_restart_plan(self, session_name: str) -> RestartPlan:
        """Build the restart plan for a session (registry-degraded if unavailable)."""
        """Build the restart plan from the live snapshot, registry, and clients.

        Falls back to a tmux-only plan with a warning issue if the registry is
        unavailable.
        """
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
        """Render the dry-run result of executing the session's restart plan."""
        plan = self.build_restart_plan(session_name)
        return render_restart_result(RestartExecutor(self.adapter).dry_run(plan))

    def execute_restart(self, session_name: str) -> tuple[str, bool]:
        """Execute the session's restart plan; return (rendered result, success)."""
        plan = self.build_restart_plan(session_name)
        result = RestartExecutor(self.adapter).execute(plan)
        return render_restart_result(result), result.is_success

    def normalize(self, session_name: str, window_index: int) -> str:
        """Normalize the layout/options of a window to its canonical archetype."""
        return normalize_window(self.adapter, session_name, window_index)

    def focus(self, session_name: str, window_index: int, mode: str) -> str:
        """Apply a focus mode to a window (e.g. zoom/spread the panes)."""
        return focus_window(self.adapter, session_name, window_index, mode)

    def resolve_pane(self, target: str) -> str:
        """Resolve a pane target and render its identity, role, kind, and agent."""
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

    def instance_id_for_pane(self, pane: str) -> dict:
        """Reverse of :meth:`resolve_instance`: read a pane's live ``@INSTANCE_ID``.

        The pane's stamp is the authoritative ``pane -> instance_id`` bridge.
        Fails closed: an unstamped or dead pane yields ``instance_id:""`` and
        ``found:False`` — never a guess. Returns
        ``{pane, instance_id, found}``.
        """
        resolved_pane = self._resolve_current(pane)
        value = self.adapter.show_pane_option(resolved_pane, "@INSTANCE_ID")
        return {"pane": resolved_pane, "instance_id": value or "", "found": bool(value)}

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
        """Resolve a target to its raw tmux %physical pane id."""
        return resolve_to_physical(self.adapter, target)

    def public_pane_id(self, target: str) -> str:
        """Resolve a target to its stable public (cardinal) pane id."""
        return resolve_to_public(self.adapter, target)

    def session_doc_for_pane(self, target: str) -> dict:
        """Fetch the session doc associated with a target pane's cardinal label."""
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
        """Invoke a skill in a target pane via the agent's invocation primitive."""
        return invoke_skill_in_pane(self.adapter, target, skill, agent=agent, arguments=arguments)

    def insert_invocation(
        self,
        target: str,
        name: str,
        *,
        agent: str = "auto",
        kind: str = "skill",
        arguments: str | None = None,
    ) -> dict:
        """Insert a kind-aware invocation (skill or command) at a pane's prompt start."""
        return insert_invocation_in_pane(
            self.adapter, target, name, agent=agent, kind=kind, arguments=arguments
        )

    def send_skill(
        self,
        target: str,
        skill: str,
        *,
        agent: str = "auto",
        arguments: str | None = None,
        clear_prompt: bool = False,
    ) -> str:
        """Send a skill invocation as literal text into a target pane's prompt."""
        return send_skill_invocation_to_pane(
            self.adapter,
            target,
            skill,
            agent=agent,
            arguments=arguments,
            clear_prompt=clear_prompt,
        )

    def send_invocation(
        self,
        target: str,
        name: str,
        *,
        agent: str = "auto",
        kind: str = "skill",
        arguments: str | None = None,
        clear_prompt: bool = False,
    ) -> str:
        """Send and submit a kind-aware invocation (skill or command)."""
        return send_invocation_to_pane(
            self.adapter,
            target,
            name,
            agent=agent,
            kind=kind,
            arguments=arguments,
            clear_prompt=clear_prompt,
        )

    def move_to_prompt_start(self, target: str, *, page_ups: int = 50) -> None:
        """Move the cursor to the start of a pane's prompt via page-up keys."""
        move_to_prompt_start(self.adapter, target, page_ups=page_ups)

    def insert_text(self, target: str, text: str) -> None:
        """Insert literal text into a pane's prompt without submitting (draft mode)."""
        insert_text(self.adapter, target, text)

    def move_to_prompt_end(self, target: str, *, page_downs: int = 50) -> None:
        """Move the cursor to the end of a pane's prompt via page-down keys."""
        move_to_prompt_end(self.adapter, target, page_downs=page_downs)

    def audience_toggle(self, target: str, *, client: str = "") -> str:
        """Toggle the audience (spotlight) view on a target pane for a client."""
        return audience_toggle(self.adapter, target, client=client)

    def audience_return(self, target: str, *, client: str = "") -> str:
        """Return a client from the audience (spotlight) view to its prior layout."""
        return audience_return(self.adapter, target, client=client)

    def tombstone_jump(self, target: str, *, client: str = "") -> str:
        """Jump a client to the tombstone recorded for a target pane."""
        return jump_tombstone(self.adapter, target, client=client)

    def tombstone_install(self, slot_pane: str, source_role: str, target_pane: str) -> str:
        """Install a tombstone in a slot pane pointing from a source role to a target."""
        return install_tombstone(self.adapter, slot_pane, source_role, target_pane)

    def create_workspace(self, session_name: str = SESSION_NAME) -> str:
        """Build the managed workspace session if it does not already exist."""
        if self.adapter.has_session(session_name):
            return f"session '{session_name}' already exists"
        build_workspace(self.adapter, session_name)
        return f"created workspace '{session_name}'"

    def rebuild_window(self, session_name: str, window_index: int) -> str:
        """Rebuild a palace or somnium window from a single survivor pane."""
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

    # ------------------------------------------------------------------
    # Façade completion — thin delegators over the free functions cli.py
    # already dispatches. One logic implementation, no fork: each method
    # wraps the *same* free function the CLI calls. These exist so the
    # daemon (and the deferred cli.py migration) can consume a complete
    # ``TmuxControlPlane`` surface in-process.
    # ------------------------------------------------------------------

    def metal_observe(self, session_name: str) -> list[dict]:
        """Observe and resolve the session at the metal layer; return dict records."""
        from .metal_resolver import observation_to_dict, observe_and_resolve

        return [observation_to_dict(obs) for obs in observe_and_resolve(self.adapter, session_name)]

    def metal_restart(self, session_name: str, *, dry_run: bool = False) -> dict:
        """Run the metal-layer restart for a session; return ``{ok, output}``."""
        from .metal_restart import metal_restart, render_metal_restart_result

        result = metal_restart(self.adapter, session_name, dry_run=dry_run)
        return {"ok": result.ok, "output": render_metal_restart_result(result)}

    def translate_ids(self, text: str, *, unresolved: str = "unresolved") -> str:
        """Translate raw physical pane ids in text to their public ids."""
        from .public_ids import physical_to_public_id_map, translate_physical_ids

        mapping = physical_to_public_id_map(self.adapter)
        return translate_physical_ids(text, mapping, unresolved=unresolved)

    def clear_runtime(self, pane: str) -> dict:
        """Clear the agent runtime in a pane, leaving the pane itself alive."""
        from .close import clear_runtime

        return clear_runtime(self.adapter, pane)

    def reap_dead_husk(self, pane: str, *, pane_role: str = "") -> dict:
        """Kill a dead remain-on-exit pane husk after runtime scrub."""
        from .close import reap_dead_husk

        return reap_dead_husk(self.adapter, pane, pane_role=pane_role)

    def close_pane(self, pane: str, *, timeout: float = 3.0) -> dict:
        """Close a single pane, gracefully clearing its runtime first."""
        from .close import close_pane

        return close_pane(self.adapter, pane, timeout=timeout)

    def close_instance(
        self,
        instance_id: str,
        *,
        lifecycle: str = "retire",
        mode: str = "now",
        pane: str = "",
        timeout: float = 3.0,
    ) -> dict:
        """Close an instance by id with the given lifecycle/mode (retire, etc.)."""
        from .close import close_instance

        return close_instance(
            self.adapter,
            instance_id,
            lifecycle=lifecycle,
            mode=mode,
            pane=pane or None,
            timeout=timeout,
        )

    def assert_instance(self, pane: str) -> dict:
        """Assert/repair the instance stamp invariants for a single pane."""
        from .assertions import assert_instance

        return assert_instance(self.adapter, pane)

    def reconcile_personas(self, session: str | None = None) -> list[dict]:
        """Re-seat every must-fill singleton persona seat against the live session.

        The event-driven replacement for the retired 2-min ``assert-personas``
        poll: loops ``PERSONA_LABELS`` and asserts each — ``assert_instance``
        respawns the thin shim into any seat whose runtime is dead and no-ops a
        healthy one (idempotent). Nothing polls this; the daemon reconciles only
        when asked (restart completion, a persona ``pane-died`` event).
        """
        from .assertions import sweep_persona_panes

        return sweep_persona_panes(self.adapter, session=session)

    def assert_personas(self) -> list[dict]:
        """Deprecated alias for :meth:`reconcile_personas` (ambient session)."""
        return self.reconcile_personas()

    def handle_event(self, event: str, *, pane: str = "", session: str | None = None) -> dict:
        """Ingest a tmux lifecycle event and reconcile a vacated must-fill seat.

        Today the one actionable event is ``pane-died`` on a perpetual seat: a
        persona singleton lost its agent → re-seat it (respawn the shim through
        ``assert_instance``). Stack-worker deaths are fill-if-row and handled by
        the existing stack reconcile, so they no-op here. Reservist deaths are
        must-fill but have no persona launcher yet (follow-on), so they are noted,
        not acted on. Never raises on an unknown/benign event.
        """
        from .assertions import (
            PERSONA_LABELS,
            assert_instance,
            seat_vacancy_policy,
        )
        from .enums import SeatVacancyPolicy

        normalized = (event or "").strip().lower().replace("_", "-")
        if normalized != "pane-died":
            return {"ok": True, "action": "ignored", "reason": f"unhandled_event:{event}"}
        if not pane:
            return {"ok": False, "action": "ignored", "reason": "no_pane"}

        try:
            resolved = resolve_pane(self.adapter, pane, session_name=session)
        except Exception as exc:  # noqa: BLE001 — a dead/missing pane is benign here
            return {"ok": True, "action": "ignored", "reason": f"unresolved_pane:{exc}"}

        pane_id = resolved.pane_id
        pane_label = resolved.pane_role or self.adapter.show_pane_option(pane_id, "@PANE_ID")
        pane_type = self.adapter.show_pane_option(pane_id, "@PANE_TYPE")
        policy = seat_vacancy_policy(pane_label, pane_type)
        if policy is not SeatVacancyPolicy.MUST_FILL:
            return {
                "ok": True,
                "action": "ignored",
                "reason": f"not_must_fill:{pane_label or pane_type or 'unknown'}",
                "pane_label": pane_label,
            }

        if pane_label in PERSONA_LABELS:
            result = assert_instance(self.adapter, pane_label, session=session)
            return {
                "ok": bool(result.get("ok")),
                "action": result.get("action") or "none",
                "reason": result.get("reason") or "",
                "pane_label": pane_label,
            }

        # Reservist seat: must-fill, but no persona launcher exists yet. The restart
        # executor seats reservists; mid-session reservist refill is a follow-on.
        return {
            "ok": True,
            "action": "noted",
            "reason": "reservist_refill_followon",
            "pane_label": pane_label,
        }

    def rotate_persona_engine(
        self,
        pane: str,
        *,
        engine: str | None = None,
        toggle: bool = False,
        session: str | None = None,
    ) -> dict:
        """Hot-swap exactly one protected persona pane.

        This mutating primitive lives on the tmuxctld control plane. CLI/keybinding
        callers must POST here instead of respawning panes in-process, so the
        daemon remains the sole persona launcher and the blast radius is the
        single resolved physical pane supplied by the caller.
        """
        from .persona_engine import rotate_persona_engine

        return rotate_persona_engine(
            self.adapter,
            pane,
            engine=engine,
            toggle=toggle,
            session=session,
        )

    def resolve_agent(
        self, pane: str = "current", agent: str = "auto", *, default: str = "claude"
    ) -> str:
        """Resolve the effective agent (cli) for a pane, applying overrides/default."""
        from .skill_invoke import resolve_agent_for_pane

        return resolve_agent_for_pane(self.adapter, pane, agent, default=default)

    def send_text(
        self,
        pane: str,
        text: str,
        *,
        clear_prompt: bool = False,
        submit: bool = True,
    ) -> dict:
        """Deliver literal text to a pane through the gated send primitive.

        ``submit=False`` routes through the insert-only primitive (draft mode —
        never issues C-m), mirroring ``tmuxctl send-text --no-submit``.
        Dispatch launcher payloads (``clear`` warmups and staged
        ``dispatch-agent`` commands) are additionally checked against the live
        occupancy ledger before any byte-bearing send.
        """
        from .occupancy import (
            assert_dispatch_target_available,
            looks_like_dispatch_launcher_payload,
        )

        if looks_like_dispatch_launcher_payload(text):
            assert_dispatch_target_available(self.adapter, pane)
        if not submit:
            self.insert_text(pane, text)
            return {"status": "inserted", "pane": pane}
        self.adapter.send_text_then_submit(pane, text, clear_prompt=clear_prompt)
        return {"status": "submitted", "pane": pane}

    def stack_add(
        self, base: str, *, cwd: str | None = None, session: str = "main", focus: bool = True
    ) -> str:
        """Add a new pane to a stack base; return its public id or 'unresolved'."""
        from .stack import add_stack_pane

        pane_id = add_stack_pane(self.adapter, session, base, cwd=cwd, focus=focus)
        return self._public_or_unresolved(pane_id)

    def stack_dispatch(
        self,
        base: str,
        command: str,
        *,
        cwd: str | None = None,
        session: str = "main",
        focus: bool = True,
        settle_seconds: float = 0.5,
    ) -> str:
        """Add a stack pane and dispatch a command into it; return its public id."""
        from .stack import dispatch_stack_command

        pane_id = dispatch_stack_command(
            self.adapter,
            session,
            base,
            command,
            cwd=cwd,
            focus=focus,
            settle_seconds=settle_seconds,
        )
        return self._public_or_unresolved(pane_id)

    def stack_adopt(
        self,
        base: str,
        pane: str,
        *,
        cwd: str | None = None,
        session: str = "main",
        focus: bool = True,
    ) -> str:
        """Adopt an existing pane into a stack base under focus preservation."""
        from .focus_guard import preserve_focus
        from .occupancy import assert_dispatch_target_available
        from .stack import add_stack_pane

        assert_dispatch_target_available(self.adapter, pane)
        with preserve_focus(
            self.adapter,
            source="tmuxctld stack adopt",
            attempted_target=f"{session}:{base}",
        ):
            pane_id = add_stack_pane(
                self.adapter, session, base, cwd=cwd, focus=focus, adopt_pane=pane
            )
        return self._public_or_unresolved(pane_id)

    def stack_enforce(
        self,
        *,
        pane: str = "current",
        window: str = "",
        focus: bool = False,
        admit: bool = False,
        kill_pending_clear: bool = False,
    ) -> str:
        """Enforce the canonical stack layout for a pane's or window's session."""
        from .stack import enforce_stack_layout

        if window:
            target = window
            focused_pane = ""
        else:
            focused_pane = self._resolve_current(pane)
            target = self.adapter.run(
                "display-message", "-t", focused_pane, "-p", "#{session_name}:#{window_index}"
            ).strip()
        return enforce_stack_layout(
            self.adapter,
            target,
            focused_pane=focused_pane,
            focus=focus,
            admit=admit,
            kill_pending_clear=kill_pending_clear,
        )

    def stack_sweep(self, *, session: str = "main", kill_pending_clear: bool = True) -> str:
        """Sweep all stacks in a session, asserting their layout invariants."""
        from .stack import sweep_stack_assertions

        return sweep_stack_assertions(self.adapter, session, kill_pending_clear=kill_pending_clear)

    def mechanicus_focus_selected(self, pane: str = "current") -> str:
        """Focus the currently selected pane within its mechanicus stack."""
        from .stack import focus_selected

        return focus_selected(self.adapter, self._resolve_current(pane))

    def mechanicus_enforce(self, pane: str = "current") -> str:
        """Enforce the stack layout for a pane's window and focus that pane."""
        from .stack import enforce_stack_layout

        resolved = self._resolve_current(pane)
        target = self.adapter.run(
            "display-message", "-t", resolved, "-p", "#{session_name}:#{window_index}"
        ).strip()
        return enforce_stack_layout(self.adapter, target, focused_pane=resolved, focus=True)

    def mechanicus_focus_guard(
        self, *, pane: str = "", client: str = "", surface: str = "after-select"
    ) -> dict:
        """Remember the prior focus or bounce a stray focus back (focus guard)."""
        from .focus_guard import remember_or_bounce

        return remember_or_bounce(self.adapter, pane=pane, client=client, surface=surface)

    def allow_mechanicus_focus(self, *, seconds: float = 4.0, reason: str = "explicit") -> float:
        """Temporarily allow mechanicus-initiated focus changes; return the deadline."""
        from .focus_guard import allow_temporarily

        return allow_temporarily(self.adapter, seconds=seconds, reason=reason, actor="tmuxctld")

    def allow_human_mechanicus_focus(
        self, *, client: str = "", reason: str = "explicit-human-navigation"
    ) -> str:
        """Allow human-initiated focus changes for a client through the focus guard."""
        from .focus_guard import allow_human_focus

        allow_human_focus(self.adapter, client=client, reason=reason, actor="tmuxctld")
        return "ok"

    def pane_select(self, *, mode: str, direction: str, client: str = "") -> str:
        """Select a pane by mode and direction (stack-aware navigation)."""
        from .pane_select import select_pane

        return select_pane(self.adapter, mode=mode, direction=direction, client=client)

    # ------------------------------------------------------------------
    # Instance-id-aware ops. Each resolves ``instance_id -> live pane`` via
    # ``resolve_instance()`` and FAILS CLOSED — no live pane carrying the
    # stamp means ``{found: False}`` and a structured no-op, never a tmux
    # action against the wrong (or a dead) pane. Pre-stages the deferred
    # token-api integration pass.
    # ------------------------------------------------------------------

    def instance_show_option(self, instance_id: str, option: str) -> dict:
        """Read a pane option on an instance's live pane; fails closed if unresolved."""
        resolved = self.resolve_instance(instance_id)
        if not resolved["found"]:
            return {"instance_id": instance_id, "found": False, "option": option, "value": ""}
        return {
            "instance_id": instance_id,
            "found": True,
            "option": option,
            "value": self.adapter.show_pane_option(resolved["pane_id"], option),
            "pane_role": resolved["pane_role"],
        }

    def instance_set_option(self, instance_id: str, option: str, value: str) -> dict:
        """Set a pane option on an instance's live pane; fails closed if unresolved."""
        resolved = self.resolve_instance(instance_id)
        if not resolved["found"]:
            return {"instance_id": instance_id, "found": False, "option": option}
        self.adapter.run("set-option", "-p", "-t", resolved["pane_id"], option, value)
        return {
            "instance_id": instance_id,
            "found": True,
            "option": option,
            "value": value,
            "pane_role": resolved["pane_role"],
        }

    def instance_unset_option(self, instance_id: str, option: str) -> dict:
        """Unset a pane option on an instance's live pane; fails closed if unresolved."""
        resolved = self.resolve_instance(instance_id)
        if not resolved["found"]:
            return {"instance_id": instance_id, "found": False, "option": option}
        self.adapter.run("set-option", "-pu", "-t", resolved["pane_id"], option)
        return {"instance_id": instance_id, "found": True, "option": option}

    def instance_send_text(
        self, instance_id: str, text: str, *, clear_prompt: bool = False, submit: bool = True
    ) -> dict:
        """Deliver text to an instance's live pane; fails closed if unresolved."""
        resolved = self.resolve_instance(instance_id)
        if not resolved["found"]:
            return {"instance_id": instance_id, "found": False}
        if not submit:
            self.insert_text(resolved["pane_id"], text)
            return {"instance_id": instance_id, "found": True, "status": "inserted"}
        self.adapter.send_text_then_submit(resolved["pane_id"], text, clear_prompt=clear_prompt)
        return {"instance_id": instance_id, "found": True, "status": "submitted"}

    def instance_tint(self, instance_id: str, color: str) -> dict:
        """Tint an instance's live pane background; fails closed if unresolved."""
        resolved = self.resolve_instance(instance_id)
        if not resolved["found"]:
            return {"instance_id": instance_id, "found": False}
        bg = color or "default"
        self.adapter.set_pane_tint(resolved["pane_id"], bg)
        return {"instance_id": instance_id, "found": True, "tint": bg}

    def instance_clear_tint(self, instance_id: str) -> dict:
        """Clear the tint on an instance's live pane; fails closed if unresolved."""
        resolved = self.resolve_instance(instance_id)
        if not resolved["found"]:
            return {"instance_id": instance_id, "found": False}
        self.adapter.clear_pane_style(resolved["pane_id"])
        return {"instance_id": instance_id, "found": True, "tint": "default"}

    def instance_focus(self, instance_id: str, *, allow: bool = False, client: str = "") -> dict:
        """Focus an instance's live pane; fails closed if unresolved."""
        resolved = self.resolve_instance(instance_id)
        if not resolved["found"]:
            return {"instance_id": instance_id, "found": False}
        # Allow-flag threaded as an EXPLICIT param (not os.environ mutation) so
        # concurrent daemon requests never race a shared process-global.
        if allow:
            from .focus_guard import allow_temporarily

            allow_temporarily(self.adapter, reason="instance-focus", actor="tmuxctld")
        # Honour an explicit client: point THAT client at the pane's window first
        # (the switch-client -c idiom used by audience/tombstone), so a multi-client
        # attach focuses the right viewer rather than only the latest-active one.
        if client:
            window = self.adapter.run(
                "display-message",
                "-t",
                resolved["pane_id"],
                "-p",
                "#{session_name}:#{window_index}",
            ).strip()
            # An explicit client was requested: if the retarget fails (stale/invalid
            # client), let it raise rather than silently focusing the pane for the
            # WRONG client and reporting success — fail loud, fail closed.
            self.adapter.run("switch-client", "-c", client, "-t", window)
        self.adapter.run("select-pane", "-t", resolved["pane_id"])
        return {"instance_id": instance_id, "found": True, "focused": resolved["pane_role"]}

    # -- small shared helpers --------------------------------------------------

    def _resolve_current(self, pane: str) -> str:
        if pane == "current":
            return self.adapter.run("display-message", "-p", "#{pane_id}").strip()
        return pane

    def _public_or_unresolved(self, target: str | None) -> str:
        if not target:
            return "unresolved"
        try:
            return self.public_pane_id(target)
        except Exception:
            return "unresolved"

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
