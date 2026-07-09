from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from typing import Any

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


def _token_api_url() -> str:
    return os.environ.get("TOKEN_API_URL", "http://localhost:7777").rstrip("/")


def _token_api_json(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: float = 8.0,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        _token_api_url() + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
            payload = json.loads(text) if text.strip() else {}
            return payload if isinstance(payload, dict) else {"response": payload}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise ValueError(f"Token-API {method} {path} failed: {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Token-API unavailable at {_token_api_url()}: {exc}") from exc


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

    def grid_expand(
        self,
        *,
        pane: str = "",
        client: str = "",
        expand: bool = False,
        retract: bool = False,
    ) -> dict:
        """Toggle native tmux zoom for a pane or client-selected pane.

        This is the daemon-native equivalent of ``tmux-grid-expand``. It uses
        native tmux zoom only (``resize-pane -Z``), preserving pane identity,
        window membership, running processes, and explicit pane targets.
        """

        info_format = "\t".join(
            [
                "#{pane_id}",
                "#{@PANE_ID}",
                "#{session_name}:#{window_index}",
                "#{window_zoomed_flag}",
            ]
        )
        if pane:
            raw_info = self.adapter.run("display-message", "-t", pane, "-p", info_format).strip()
        elif client:
            raw_info = self.adapter.run("display-message", "-c", client, "-p", info_format).strip()
        else:
            raw_info = self.adapter.run("display-message", "-p", info_format).strip()

        parts = raw_info.split("\t")
        if len(parts) != 4:
            raise ValueError("grid-expand could not resolve target pane")
        target_pane, pane_label, target_window, raw_zoomed_before = (part.strip() for part in parts)
        if not target_pane:
            raise ValueError("grid-expand could not resolve target pane")
        if not target_window:
            raise ValueError(f"grid-expand could not resolve target window for {target_pane}")
        zoomed_before = raw_zoomed_before == "1"

        pane_label = canonical_pane_role(pane_label) if pane_label else ""
        if not pane_label and pane and not pane.startswith("%") and pane != "current":
            pane_label = canonical_pane_role(pane)

        for option in (
            "@GRID_EXPANDED",
            "@GRID_STASH",
            "@GENERIC_EXPANDED",
            "@GENERIC_STASH",
            "@SIDE_EXPANDED",
        ):
            value = "" if option.endswith("_STASH") else "none"
            self.adapter.run(
                "set-option",
                "-w",
                "-t",
                target_window,
                option,
                value,
                allow_failure=True,
            )

        action = "noop"
        if retract:
            if zoomed_before:
                self.adapter.run("resize-pane", "-Z", "-t", target_pane)
                action = "retract"
        elif expand:
            if not zoomed_before:
                self.adapter.run("resize-pane", "-Z", "-t", target_pane)
                action = "expand"
        else:
            self.adapter.run("resize-pane", "-Z", "-t", target_pane)
            action = "toggle"

        zoomed_after = (
            self.adapter.run(
                "display-message", "-t", target_window, "-p", "#{window_zoomed_flag}"
            ).strip()
            == "1"
        )
        return {
            "status": "ok",
            "pane": pane_label or "unresolved",
            "window": target_window,
            "action": action,
            "zoomed_before": zoomed_before,
            "zoomed_after": zoomed_after,
        }

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
        """Resolve an instance UUID to its live pane via the wrapper ledger.

        Returns ``{instance_id, pane_id, pane_role, found, agent, live_agent}``.
        When no active ledger row carries the instance, ``found`` is False,
        ``pane_id``/``pane_role`` are empty strings, ``agent`` is ``auto``, and
        ``live_agent`` is False.
        """
        from .wrapper_ledger import LEDGER

        row = LEDGER.resolve(instance_id=instance_id)
        if row is not None:
            return {
                "instance_id": row.instance_id,
                "pane_id": row.pane_positional_id,
                "pane_role": row.pane_positional_id,
                "found": True,
                "agent": row.engine or "auto",
                "live_agent": bool(row.engine),
                "ledger": row.as_dict(),
            }
        try:
            from .resolver import resolve_instance as resolve_instance_from_tmux

            resolved = resolve_instance_from_tmux(self.adapter, instance_id)
            if resolved.found:
                pane_role = canonical_pane_role(resolved.pane_role or "")
                return {
                    "instance_id": instance_id,
                    "pane_id": pane_role,
                    "pane_role": pane_role,
                    "found": True,
                    "agent": "auto",
                    "live_agent": False,
                }
        except Exception:
            pass
        return {
            "instance_id": instance_id,
            "pane_id": "",
            "pane_role": "",
            "found": False,
            "agent": "auto",
            "live_agent": False,
        }

    def instance_id_for_pane(self, pane: str) -> dict:
        """Reverse of :meth:`resolve_instance`: resolve through the wrapper ledger.

        Fails closed: an unstamped or dead pane yields ``instance_id:""`` and
        ``found:False`` — never a guess. Returns
        ``{pane, instance_id, found}``.
        """
        from .wrapper_ledger import LEDGER

        pane_positional_id = pane
        if pane == "current" or pane.startswith("%"):
            try:
                target = self._resolve_current(pane)
                pane_positional_id = self.adapter.show_pane_option(target, "@PANE_ID")
            except Exception:
                pane_positional_id = pane
        row = LEDGER.resolve(pane_positional_id=pane_positional_id)
        if row is not None and row.instance_id:
            return {
                "pane": row.pane_positional_id,
                "instance_id": row.instance_id,
                "found": True,
                "ledger": row.as_dict(),
            }
        # Ledger miss, OR a ledger row whose instance_id is not yet bound. Fall
        # back to the live @INSTANCE_ID stamp — the same fail-closed tmux truth the
        # forward resolver uses, in reverse. Codex workers are never entered in the
        # wrapper ledger and an OPEN wrapper row can precede instance binding; both
        # still self-identify via their pane stamp. The old fallback passed an
        # unresolved public page:id straight to show-pane-option (only "current"/%NN
        # were resolved), so a public id read nothing and returned "" — and an
        # unbound-but-present ledger row short-circuited before the stamp read
        # entirely. Either way the pane resolved to "", which forces a false
        # ``unverified`` on every delivered send, since the ack sniffer keys on
        # instance_id (an empty id makes the wait return immediately).
        try:
            from .resolver import instance_id_for_pane as instance_id_for_pane_from_tmux

            stamp_instance_id = instance_id_for_pane_from_tmux(self.adapter, pane_positional_id)
            if stamp_instance_id:
                return {
                    "pane": pane_positional_id,
                    "instance_id": stamp_instance_id,
                    "found": True,
                }
        except Exception:
            pass
        # No live stamp. Preserve any ledger row metadata (unbound instance_id)
        # rather than dropping it, so callers still see the wrapper row.
        if row is not None:
            return {
                "pane": row.pane_positional_id,
                "instance_id": row.instance_id,
                "found": bool(row.instance_id),
                "ledger": row.as_dict(),
            }
        return {"pane": pane_positional_id, "instance_id": "", "found": False}

    def ledger_upsert(
        self,
        *,
        wrapper_id: str,
        instance_id: str = "",
        persona: str = "",
        pane_positional_id: str = "",
        engine: str = "",
        working_dir: str = "",
        born_epoch: float | str | None = None,
        state: str = "OPEN",
    ) -> dict:
        """Upsert one wrapper-ledger row and return the authoritative row."""
        from .wrapper_ledger import LEDGER

        return LEDGER.upsert(
            wrapper_id=wrapper_id,
            instance_id=instance_id,
            persona=persona,
            pane_positional_id=pane_positional_id,
            engine=engine,
            working_dir=working_dir,
            born_epoch=born_epoch,
            state=state,
        ).as_dict()

    def ledger_close(self, wrapper_id: str) -> dict:
        """Mark a wrapper-ledger row closed."""
        from .wrapper_ledger import LEDGER

        row = LEDGER.close(wrapper_id)
        return {"closed": bool(row), "row": row.as_dict() if row else None}

    def ledger_resolve(
        self,
        value: str = "",
        *,
        wrapper_id: str = "",
        instance_id: str = "",
        pane_positional_id: str = "",
        include_closed: bool = False,
    ) -> dict:
        """Resolve wrapper_id, instance_id, or positional pane id to one row."""
        from .wrapper_ledger import LEDGER

        row = LEDGER.resolve(
            value,
            wrapper_id=wrapper_id,
            instance_id=instance_id,
            pane_positional_id=pane_positional_id,
            include_closed=include_closed,
        )
        return {"found": bool(row), "row": row.as_dict() if row else None}

    def ledger_rows(self, *, include_closed: bool = True) -> list[dict]:
        """Return wrapper-ledger rows."""
        from .wrapper_ledger import LEDGER

        return [row.as_dict() for row in LEDGER.rows(include_closed=include_closed)]

    def ledger_reconcile(self) -> dict:
        """Rebuild active wrapper-ledger rows and reconcile pane chrome from it.

        Pane chrome is a pure derivative of the wrapper→pane bind ledger when
        bind truth is trustworthy. After active rows are rebuilt from live tmux,
        unbound non-singleton panes without a live agent are scrubbed and reported
        via ``chrome_scrubbed_unbound_panes``/``*_count``. Unbound panes with a
        live agent are split-brain bind divergences, not free residue; they are
        excluded from scrubbing and reported via
        ``chrome_unbound_live_divergences``/``*_count`` with reason
        ``live_agent_without_bind``. This lets a released bind release
        tint/title/runtime chrome transactionally without erasing live TUIs whose
        bind has failed to land.
        """
        from .wrapper_ledger import LEDGER

        out = dict(LEDGER.reconcile_from_tmux(self.adapter))

        from .occupancy import scan_ledger_dispatch_availability

        scrubbed: list[str] = []
        live_divergences: list[dict[str, str]] = []
        for pane in scan_ledger_dispatch_availability(self.adapter):
            # chrome = f(bind), but only when the bind truth is trustworthy.  A
            # live TUI with no active bind is split-brain, not free residue; do
            # not erase operator-visible chrome from a running agent until the
            # liveness/bind-repair lane can reconcile it.  Report it loudly so
            # callers can route the divergence instead of mistaking it for a
            # clean free slot.
            if pane.singleton or pane.instance_id:
                continue
            if pane.live_agent:
                live_divergences.append(
                    {
                        "pane": pane.pane_id,
                        "pane_label": pane.pane_role or "",
                        "reason": "live_agent_without_bind",
                    }
                )
                continue
            target = pane.pane_id
            self.adapter.clear_runtime_state(target)
            scrubbed.append(target)
        out["chrome_scrubbed_unbound_panes"] = scrubbed
        out["chrome_scrubbed_unbound_count"] = len(scrubbed)
        out["chrome_unbound_live_divergences"] = live_divergences
        out["chrome_unbound_live_divergence_count"] = len(live_divergences)
        return out

    def freelist(self) -> list[dict]:
        """List the unoccupied, agent-free panes (the freelist).

        Purely derived from the live daemon occupancy ledger — no stored state.
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
        """Re-seat every must-fill singleton persona AND reservist seat.

        The event-driven replacement for the retired 2-min ``assert-personas``
        poll: loops ``PERSONA_LABELS`` and asserts each — ``assert_instance``
        respawns the thin shim into any seat whose runtime is dead and no-ops a
        healthy one (idempotent). It then sweeps the two reservist heartbeat seats
        the same way (fill-on-absence = "keep the pulse"), so restart ``/reconcile``
        and any on-demand reconcile seat reservists too — the daemon is now the sole
        reservist launcher, replacing the retired executor ``dispatch --direct``
        writer. Nothing polls this; the daemon reconciles only when asked (restart
        completion, a persona/reservist ``pane-died`` event).
        """
        from .assertions import sweep_persona_panes, sweep_reservist_panes

        return sweep_persona_panes(self.adapter, session=session) + sweep_reservist_panes(
            self.adapter, session=session
        )

    def assert_personas(self) -> list[dict]:
        """Deprecated alias for :meth:`reconcile_personas` (ambient session)."""
        return self.reconcile_personas()

    def handle_event(self, event: str, *, pane: str = "", session: str | None = None) -> dict:
        """Ingest a tmux lifecycle event and reconcile a vacated must-fill seat.

        Today the one actionable event is ``pane-died`` on a perpetual seat: a
        persona singleton lost its agent → re-seat it (respawn the shim through
        ``assert_instance``). Stack-worker deaths are fill-if-row and handled by
        the existing stack reconcile, so they no-op here. Reservist deaths are
        must-fill and now re-seated too (respawn the standby agent through
        ``assert_reservist_seat``; a fully-killed pane defers to F2's layout heal).
        Never raises on an unknown/benign event.
        """
        from .assertions import (
            PERSONA_LABELS,
            assert_instance,
            assert_reservist_seat,
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
        if policy is SeatVacancyPolicy.MUST_FILL:
            if pane_label in PERSONA_LABELS:
                # PERPETUAL class: a persona singleton seat — REVIVE it (reseat the
                # thin shim through assert_instance; idempotent, boot-grace dedups).
                result = assert_instance(self.adapter, pane_label, session=session)
                return {
                    "ok": bool(result.get("ok")),
                    "action": result.get("action") or "none",
                    "reason": result.get("reason") or "",
                    "pane_label": pane_label,
                }

            # Reservist seat: must-fill heartbeat — REVIVE it on the low-latency
            # death path (respawn the standby agent through the daemon-native
            # reservist launcher; idempotent, boot-grace dedups). If the pane itself
            # was fully killed, ``assert_reservist_seat`` returns ``pane_missing``
            # and defers to F2's layout heal — this branch never recreates the pane.
            result = assert_reservist_seat(self.adapter, pane_label, session=session)
            return {
                "ok": bool(result.get("ok")),
                "action": result.get("action") or "none",
                "reason": result.get("reason") or "",
                "pane_label": pane_label,
            }

        # Not a must-fill seat. Three sub-cases, ONE decision — all hand to the
        # unified pane-class teardown router (the SAME dispatcher WrapperEnd uses):
        #   * policy is None       — a pre-allocated palace/somnium SLOT or a
        #     dynamically-created WORKER.
        #   * FILL_IF_ROW          — a mechanicus stack worker whose pane just died.
        # A dying stack worker is a dead HUSK, not a seat to hold open: leaving it
        # behind is the "Pane is dead" graveyard this pane-died event exists to
        # prevent (it accumulates until a human cull and risks a tmux geometry
        # allocation failure). The router culls a dead WORKER/stack-worker husk,
        # clears a palace/somnium SLOT in place (PRESERVED — returned to the
        # freelist, never culled), and preserves a PERPETUAL singleton for the
        # caller to revive. ``reap_dead_husk`` only kills a pane tmux confirms
        # dead, so a still-live pane is never a collateral kill and a singleton is
        # protected by ``classify_pane`` (PERPETUAL) even if it slips through here.
        return self.teardown_pane(pane_id, pane_label=pane_label, source="pane-died")

    def teardown_pane(
        self,
        pane: str,
        *,
        pane_label: str = "",
        window_name: str | None = None,
        source: str = "",
    ) -> dict:
        """Class-gated pane teardown shared by WrapperEnd and the pane-died hook.

        Classifies the pane (PERPETUAL / SLOT / WORKER) and applies the matching
        action via the unified router: a SLOT is cleared in place and PRESERVED
        (returned to the freelist — never culled), a WORKER is culled, a PERPETUAL
        seat is preserved for the caller to revive.
        """
        from .teardown import PaneClass, apply_teardown, classify_pane

        if not pane_label:
            pane_label = self.adapter.show_pane_option(pane, "@PANE_ID")
        if window_name is None:
            window_name = self.adapter.run(
                "display-message", "-t", pane, "-p", "#{window_name}", allow_failure=True
            ).strip()
        pane_class = classify_pane(pane_label, window_name)
        result = apply_teardown(self.adapter, pane, pane_class, pane_role=pane_label)
        action = {
            PaneClass.SLOT: "cleared_in_place",
            PaneClass.WORKER: "culled",
            PaneClass.PERPETUAL: "preserved",
        }[pane_class]
        return {
            "ok": result.get("status") != "failed",
            "action": action,
            "reason": result.get("status", ""),
            "pane_label": pane_label,
            "pane_class": pane_class.value,
            "source": source,
            "result": result,
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
        ``dispatch-agent`` commands) require a ledger-free pane; all other
        comms require a wrapper-ledger-occupied managed agent before any
        byte-bearing send.
        """
        from .occupancy import (
            assert_comms_delivery_target_occupied,
            assert_dispatch_target_available,
            looks_like_dispatch_launcher_payload,
        )

        physical_pane = pane if pane.startswith("%") else resolve_to_physical(self.adapter, pane)
        if looks_like_dispatch_launcher_payload(text):
            assert_dispatch_target_available(self.adapter, physical_pane)
        else:
            assert_comms_delivery_target_occupied(self.adapter, physical_pane)
        if not submit:
            self.insert_text(physical_pane, text)
            return {"status": "inserted", "pane": pane, "physical_pane": physical_pane}
        self.adapter.send_text_then_submit(physical_pane, text, clear_prompt=clear_prompt)
        return {"status": "submitted", "pane": pane, "physical_pane": physical_pane}

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
    # Human keybind daemon endpoints. These are deliberately thin wrappers over
    # the tmux primitives the existing keybind scripts already used; interactive
    # UI choices stay outside the daemon unless a safe, named payload exists.
    # ------------------------------------------------------------------

    def _keybind_target_pane(self, pane: str = "current", *, client: str = "") -> str:
        if pane and pane != "current":
            return pane
        if client:
            return self.adapter.run("display-message", "-c", client, "-p", "#{pane_id}").strip()
        return self.adapter.run("display-message", "-p", "#{pane_id}").strip()

    @staticmethod
    def detect_mode_from_capture(capture: str) -> str:
        if "bypass permissions on" in capture:
            return "bypass"
        if "plan mode on" in capture:
            return "plan"
        if "accept edits on" in capture:
            return "accept"
        return "none"

    def mode_toggle(
        self,
        *,
        pane: str = "current",
        status_only: bool = False,
        delay_seconds: float = 0.15,
    ) -> dict:
        """Toggle Claude/Codex Shift+Tab mode using the existing screen oracle."""
        target_pane = self._keybind_target_pane(pane)
        capture = self.adapter.run(
            "capture-pane", "-t", target_pane, "-p", "-S", "-5", allow_failure=True
        )
        before = self.detect_mode_from_capture(capture)
        if status_only:
            return {"pane": target_pane, "mode": before, "presses": 0}
        presses_by_mode = {"plan": 1, "bypass": 3, "accept": 2, "none": 2}
        expected_by_mode = {
            "plan": "bypass",
            "bypass": "plan",
            "accept": "bypass",
            "none": "plan",
        }
        presses = presses_by_mode[before]
        for index in range(presses):
            self.adapter.send_keys(target_pane, "BTab")
            if delay_seconds > 0 and index + 1 < presses:
                time.sleep(delay_seconds)
        return {
            "pane": target_pane,
            "from": before,
            "to": expected_by_mode[before],
            "presses": presses,
        }

    def open_session_doc(self, arg: str = "current") -> dict:
        """Resolve and open a session doc through Token-API's open-by-id endpoint."""
        value = (arg or "current").strip()
        if value.isdigit():
            doc_id = value
            doc = {"id": int(value)}
        else:
            if value == "current":
                doc = self.session_doc_for_pane("current")
            else:
                # Keybind callers commonly know only the physical pane target
                # tmux expanded at dispatch time. Normalize to the stable public
                # role before asking Token-API for the session document.
                pane_label = self.public_pane_id(value)
                doc = fetch_session_doc_for_pane_label(pane_label)
            doc_id = str(doc.get("id") or "")
        if not doc_id.isdigit():
            raise ValueError("no session doc id resolved")
        response = _token_api_json("POST", f"/api/session-docs/{doc_id}/open", None)
        return {
            "opened": True,
            "doc_id": int(doc_id),
            "title": response.get("title") or doc.get("title") or "",
            "pane_label": doc.get("pane_label") or "",
            "result": response,
        }

    def goto_spoken(
        self,
        *,
        db_path: str | None = None,
        max_age_seconds: int = 600,
    ) -> dict:
        """Focus the pane whose instance most recently emitted a TTS event."""
        path = Path(db_path or os.environ.get("TOKEN_API_DB") or Path.home() / ".claude/agents.db")
        if not path.exists():
            self.adapter.run(
                "display-message",
                f"goto-spoken: agents.db missing at {path}",
                allow_failure=True,
            )
            return {"status": "missing_db", "db_path": str(path)}

        events = ("tts_playing", "tts_starting", "tts_queued")
        with closing(sqlite3.connect(path)) as conn:
            conn.row_factory = sqlite3.Row
            placeholders = ",".join("?" * len(events))
            row = conn.execute(
                f"""
                SELECT e.instance_id, e.event_type,
                       CAST(
                         (julianday('now') - julianday(e.created_at)) * 86400 AS INTEGER
                       ) AS age_seconds,
                       ci.tmux_pane, ci.name AS tab_name
                FROM events e
                LEFT JOIN instances ci ON ci.id = e.instance_id
                WHERE e.event_type IN ({placeholders})
                  AND e.instance_id IS NOT NULL
                ORDER BY datetime(e.created_at) DESC, e.id DESC
                LIMIT 1
                """,
                events,
            ).fetchone()

        if row is None:
            self.adapter.run(
                "display-message",
                "goto-spoken: no TTS events recorded",
                allow_failure=True,
            )
            return {"status": "none"}
        age = int(row["age_seconds"] or 0)
        if age > max_age_seconds:
            self.adapter.run(
                "display-message",
                f"goto-spoken: no recent speaker (last was {age // 60}m ago)",
                allow_failure=True,
            )
            return {"status": "stale", "age_seconds": age}
        pane = str(row["tmux_pane"] or "")
        if not pane:
            instance_id = str(row["instance_id"] or "")
            self.adapter.run(
                "display-message",
                f"goto-spoken: speaker has no tmux_pane (instance {instance_id[:8]})",
                allow_failure=True,
            )
            return {"status": "missing_pane", "instance_id": instance_id}

        try:
            pane = resolve_to_physical(self.adapter, pane)
        except Exception:
            # Raw stale DB entries are common; use the stored target as the probe
            # and let tmux fail closed below.
            pass
        probe = self.adapter.run(
            "display-message", "-p", "-t", pane, "#{session_name}:#{window_id}", allow_failure=True
        ).strip()
        if not probe:
            self.adapter.run(
                "display-message",
                "goto-spoken: pane not found (stale registry?)",
                allow_failure=True,
            )
            return {"status": "stale_pane", "instance_id": str(row["instance_id"] or "")}

        from .focus_guard import allow_temporarily

        allow_temporarily(self.adapter, reason="goto-spoken", actor="tmuxctld")
        self.adapter.run("select-window", "-t", probe)
        self.adapter.run("select-pane", "-t", pane)
        role = self._public_or_unresolved(pane)
        label = str(row["tab_name"] or row["instance_id"] or "")[:80]
        self.adapter.run("display-message", f"→ {label} ({age}s ago)", allow_failure=True)
        return {
            "status": "focused",
            "instance_id": str(row["instance_id"] or ""),
            "pane_role": role,
            "window": probe,
            "age_seconds": age,
            "event_type": str(row["event_type"] or ""),
        }

    def pane_rename(self, pane: str, name: str = "") -> dict:
        """Interview-nudge the instance in a pane; explicit rename stays CLI-owned."""
        target_pane = self._keybind_target_pane(pane)
        instance_id = self.adapter.show_pane_option(target_pane, "@INSTANCE_ID").strip()
        if not instance_id:
            raise ValueError(f"pane has no @INSTANCE_ID: {target_pane}")
        if name.strip():
            # The existing explicit rename path shells through instance-name and
            # then sends /rename into the agent. That is not a thin tmuxctl
            # primitive, so the daemon exposes only the safe empty-name interview
            # nudge for now.
            raise NotImplementedError("explicit pane rename is not daemonized")
        response = _token_api_json(
            "POST", "/api/orchestrator/naming_nudge", {"instance_id": instance_id}, timeout=2
        )
        if response.get("success") is False:
            raise ValueError(f"naming nudge failed: {json.dumps(response, sort_keys=True)}")
        self.adapter.run(
            "display-message",
            f"interview: asked {instance_id} to name itself",
            allow_failure=True,
        )
        return {"status": "nudged", "instance_id": instance_id, "result": response}

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

    def instance_rename(self, name: str, *, instance_id: str = "", pane: str = "") -> dict:
        """Own a pane's identity: the ``@PANE_LABEL`` border nametag AND native title.

        Semantic replacement for token-api authoring a raw ``set-option @PANE_LABEL``
        through ``/tmux/run`` — the daemon is the sole writer of pane identity.
        Resolution precedence: an explicit ``pane`` (already live-resolved by the
        caller) wins; otherwise ``instance_id`` resolves to its live pane via the
        wrapper ledger. FAILS CLOSED — an unresolved target means ``{found: False}``
        and zero tmux mutation, never a rename against the wrong (or a dead) pane.
        """
        if pane:
            try:
                resolved = resolve_pane(self.adapter, pane)
            except Exception:  # noqa: BLE001 — a missing/dead pane is a fail-closed no-op
                return {"found": False, "target": pane, "pane_role": "", "name": name}
            target = resolved.pane_id
            pane_role = resolved.pane_role
        else:
            resolved = self.resolve_instance(instance_id)
            if not resolved["found"]:
                return {"found": False, "target": instance_id, "pane_role": "", "name": name}
            target = resolved["pane_id"]
            pane_role = resolved["pane_role"]
        # Border nametag: pane-border-format reads ONLY @PANE_LABEL (zero fork per
        # redraw). adapter.run auto-resolves a canonical role target to its physical
        # %id, so an instance-role and a physical pane target both land correctly.
        self.adapter.run("set-option", "-p", "-t", target, "@PANE_LABEL", name)
        # Native pane title. select-pane -T is the TITLE-ONLY form (not rename-pane):
        # camera-neutral per tmux_adapter._select_pane_title_only, so a rename never
        # snaps the operator's focus onto the renamed pane.
        self.adapter.run("select-pane", "-t", target, "-T", name)
        return {"found": True, "target": target, "pane_role": pane_role, "name": name}

    def _pane_for_wrapper_id(self, wrapper_id: str) -> str:
        """Resolve the live physical pane for a wrapper id via the ledger, or ""."""
        if not wrapper_id:
            return ""
        from .wrapper_ledger import LEDGER

        row = LEDGER.resolve(wrapper_id=wrapper_id)
        if row and row.pane_positional_id:
            try:
                return resolve_pane(self.adapter, row.pane_positional_id).pane_id
            except Exception:  # noqa: BLE001 — a dead/missing pane fails closed
                return ""
        return ""

    def instance_stamp(
        self,
        *,
        instance_id: str,
        pane: str = "",
        wrapper_id: str = "",
        pane_positional_id: str = "",
        persona: str = "",
        engine: str = "",
        working_dir: str = "",
        vacate_pane: str = "",
    ) -> dict:
        """Sole writer of the durable ``@INSTANCE_ID`` pane stamp + ledger binding.

        The semantic replacement for token-api authoring a raw ``set-option
        @INSTANCE_ID`` through ``/tmux/run`` — tmuxctld is the single writer of the
        pane's identity stamp, exactly as :meth:`instance_rename` is for
        ``@PANE_LABEL``. token-api resolves the canonical instance row id at
        SessionStart and hands it here; the daemon owns the write.

        ``@INSTANCE_ID`` is the BOOTSTRAP identity that pane resolution itself
        depends on, so it is stamped onto an EXPLICIT ``pane`` (resolved by the
        caller at SessionStart) — never re-derived by-instance_id (chicken/egg).
        A ``wrapper_id`` fallback resolves the pane through the ledger. FAILS
        CLOSED: an unresolved target means ``{found: False, stamped: False}`` and
        zero tmux mutation, never a stamp against the wrong (or a dead) pane.

        Also binds the wrapper-ledger row's ``instance_id`` (so the reverse oracle
        prefers the ledger over a stamp scan) and, when ``vacate_pane`` is given,
        GUARDED-clears a prior pane's stamp — only when it still carries THIS
        instance's id, so a pane already reused by another agent is never clobbered.
        """
        instance_id = (instance_id or "").strip()
        if not instance_id:
            return {"found": False, "stamped": False, "reason": "no_instance_id",
                    "instance_id": "", "pane": ""}

        target = ""
        pane_role = ""
        if pane:
            try:
                resolved = resolve_pane(self.adapter, pane)
                target = resolved.pane_id
                pane_role = resolved.pane_role
            except Exception:  # noqa: BLE001 — a missing/dead pane is a fail-closed no-op
                target = ""
        if not target and wrapper_id:
            target = self._pane_for_wrapper_id(wrapper_id)
        if not target:
            return {"found": False, "stamped": False, "reason": "unresolved_pane",
                    "instance_id": instance_id, "pane": ""}

        # tmuxctld is the sole writer of the @INSTANCE_ID identity stamp.
        self.adapter.run("set-option", "-p", "-t", target, "@INSTANCE_ID", instance_id)

        # Bind the wrapper-ledger occupancy row so the reverse oracle resolves this
        # pane -> instance_id from the ledger (preferred) and the tmux stamp scan
        # (fallback) agree. Keyed by wrapper_id; codex workers with no wrapper row
        # are served by the stamp alone.
        ledger = None
        label = pane_positional_id or str(
            self.adapter.show_pane_option(target, "@PANE_ID") or ""
        ).strip()
        if wrapper_id:
            from .wrapper_ledger import LEDGER

            ledger = LEDGER.upsert(
                wrapper_id=wrapper_id,
                instance_id=instance_id,
                persona=persona,
                pane_positional_id=label,
                engine=engine,
                working_dir=working_dir,
                state="OPEN",
            ).as_dict()

        # Guarded vacate: an instance that moved panes must not leave its id on the
        # old pane (a stale duplicate the oracle would resolve). Clear ONLY when the
        # old pane still carries this instance's id.
        vacated = ""
        if vacate_pane:
            try:
                old = resolve_pane(self.adapter, vacate_pane).pane_id
            except Exception:  # noqa: BLE001 — old pane gone: nothing to vacate
                old = ""
            if old and old != target:
                current = str(self.adapter.show_pane_option(old, "@INSTANCE_ID") or "").strip()
                if current == instance_id:
                    self.adapter.run("set-option", "-pu", "-t", old, "@INSTANCE_ID")
                    vacated = old

        return {
            "found": True,
            "stamped": True,
            "instance_id": instance_id,
            "pane": target,
            "pane_role": pane_role,
            "pane_positional_id": label,
            "ledger": ledger,
            "vacated": vacated,
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
        from .occupancy import assert_comms_delivery_target_occupied

        resolved = self.resolve_instance(instance_id)
        if not resolved["found"]:
            return {"instance_id": instance_id, "found": False}
        assert_comms_delivery_target_occupied(self.adapter, resolved["pane_id"])
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
