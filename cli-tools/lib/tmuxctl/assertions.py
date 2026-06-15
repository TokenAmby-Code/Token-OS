from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .api import (
    fetch_instance_registry,
    log_event,
    patch_instance,
    stop_instance,
    update_instance_activity,
)
from .custodes import _pane_pid, pane_has_active_agent
from .enums import InstanceStatus
from .resolver import resolve_pane
from .tmux_adapter import TmuxAdapter

DISPATCH_BIN = "dispatch"
CLAUDE_CMD_BIN = "claude-cmd"
PERSONA_LABELS = {
    "legion:custodes",
    "legion:malcador",
    "mechanicus:fabricator-general",
    "mechanicus:admin",
}


@dataclass(frozen=True)
class PersonaSpec:
    pane_label: str
    persona: str
    instance_type: str
    session_doc: str
    engine: str = "claude"
    sync: bool = False
    model: str = ""
    # Explicit launch cwd. Empty → resolve the Imperium-ENV vault at launch time
    # (see _persona_working_dir). A per-persona override can pin a different vault
    # (e.g. koronus civic seats → Civic/Pax-ENV).
    working_dir: str = ""


def _vault_root() -> Path:
    root = os.environ.get("IMPERIUM")
    if root:
        return Path(root) / "Imperium-ENV"
    return Path("/Volumes/Imperium/Imperium-ENV")


def _persona_working_dir() -> str:
    """Imperium-ENV vault as the persona launch cwd, mount-guarded.

    Persona panes (Custodes, Malcador, FG, Admin) must launch from the vault, not
    $HOME. Returns "" when the vault is not mounted so dispatch falls back to its
    own default instead of being handed a nonexistent --dir.
    """
    vault = _vault_root()
    return str(vault) if vault.is_dir() else ""


def _today_daily_note() -> str:
    return str(_vault_root() / f"{date.today().isoformat()}.md")


def _admin_log() -> str:
    return str(_vault_root() / "Mars" / "Logs" / f"administratum-{date.today().isoformat()}.md")


def persona_spec(label: str) -> PersonaSpec:
    if label == "legion:custodes":
        return PersonaSpec(
            label, "custodes", "hook_driven", _today_daily_note(), sync=True, model="opus"
        )
    if label == "legion:malcador":
        return PersonaSpec(
            label,
            "malcador",
            "hook_driven",
            str(_vault_root() / "Terra" / "Sessions" / "malcador.md"),
            model="fable",
        )
    if label == "mechanicus:fabricator-general":
        return PersonaSpec(
            label,
            "fabricator-general",
            "hook_driven",
            str(_vault_root() / "Mars" / "Sessions" / "fabricator-general.md"),
        )
    if label == "mechanicus:admin":
        return PersonaSpec(label, "administratum", "hook_driven", _admin_log(), model="sonnet")
    raise ValueError(f"unknown persona pane: {label}")


def _pane_label(adapter: TmuxAdapter, pane_id: str, resolved_role: str = "") -> str:
    return resolved_role or adapter.show_pane_option(pane_id, "@PANE_ID")


def _pane_type(adapter: TmuxAdapter, pane_id: str) -> str:
    return adapter.show_pane_option(pane_id, "@PANE_TYPE")


def _registry_entries(pane_id: str, pane_label: str, *, include_stopped: bool = False):
    registry = fetch_instance_registry()
    rows = [
        row
        for row in registry.instances
        if (include_stopped or row.status is not InstanceStatus.STOPPED)
        and (row.tmux_pane == pane_id or (pane_label and row.pane_label == pane_label))
    ]
    rows.sort(key=lambda r: r.last_activity, reverse=True)
    return rows


def _runtime_has_instance(adapter: TmuxAdapter, pane_id: str) -> bool:
    return pane_has_active_agent(_pane_pid(adapter, pane_id))


def _dispatch_args(
    pane_id: str, upsert: dict[str, Any], prompt_file: Path | None = None
) -> list[str]:
    engine = str(upsert.get("engine") or "claude")
    args = [DISPATCH_BIN, "--engine", engine, "--pane", pane_id]
    if persona := upsert.get("persona"):
        args += ["--persona", str(persona)]
    if session_doc := upsert.get("session_doc"):
        args += ["--session-doc", str(session_doc)]
    if model := upsert.get("model"):
        args += ["--model", str(model)]
    if work_dir := upsert.get("dir") or upsert.get("working_dir"):
        args += ["--dir", str(work_dir)]
    if upsert.get("instance_type"):
        args += ["--instance-type", str(upsert["instance_type"])]
    if prompt_file is not None:
        args += ["--prompt-file", str(prompt_file)]
    elif prompt := upsert.get("prompt"):
        args += ["--prompt", str(prompt)]
    if upsert.get("sync"):
        args.append("--sync")
    elif upsert.get("no_gt", True):
        args.append("--no-gt")
    return args


def _launch(pane_id: str, upsert: dict[str, Any], prompt: str = "") -> tuple[bool, str]:
    prompt_file = None
    try:
        if prompt:
            fd, path = tempfile.mkstemp(prefix="tmuxctl-assert-", suffix=".md")
            os.close(fd)
            prompt_file = Path(path)
            prompt_file.write_text(prompt)
        proc = subprocess.run(
            _dispatch_args(pane_id, upsert, prompt_file),
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if proc.returncode != 0:
            return False, f"dispatch rc={proc.returncode}: {proc.stderr.strip()[:240]}"
        return True, "launched"
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    finally:
        if prompt_file:
            prompt_file.unlink(missing_ok=True)


def _upsert_prompt(pane_id: str, prompt: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [CLAUDE_CMD_BIN, "--pane", pane_id, prompt],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, f"claude-cmd rc={proc.returncode}: {proc.stderr.strip()[:200]}"
    return True, "upserted_existing_pane"


def _send_persona_command(adapter: TmuxAdapter, pane_id: str, persona: str) -> tuple[bool, str]:
    try:
        adapter.send_text_then_submit(pane_id, f"/persona {persona}", clear_prompt=True)
        return True, "persona_command_sent"
    except Exception as exc:
        return False, str(exc)


# ── Persona assertion guardrail ──────────────────────────────────────────────
# The assertion loop is stateless: every tick is an independent process, so a
# persistently-failing predicate would re-inject `/persona <name>` on every tick
# forever — the sisyphus loop that rotted FG's context (~60+ identical sends in
# one window). The guard records, on the pane itself, the persona + a hash of the
# observed registry row at the last send. A resend is suppressed when the
# observed row is byte-for-byte unchanged (re-sending cannot change a verdict
# that already failed against this exact input) until either the row mutates or
# the backoff window elapses. State lives in a tmux pane option to survive across
# the independent per-tick invocations, matching the @CC_STATE/@TTS_STATE idiom.
PERSONA_GUARD_OPTION = "@PERSONA_ASSERT_GUARD"
PERSONA_GUARD_BACKOFF_SECONDS = 300.0
# Fail-open threshold. The persona correction (`/persona`) is SECONDARY; payload
# delivery is PRIMARY. A correction that cannot change its own input (the registry
# row is byte-for-byte unchanged) is held — suppressed — for a few bounded ticks to
# give a legitimately-mid-transition pane time to settle. Once the attempt count
# reaches this threshold the loop STOPS suppressing the payload: it flips to the
# `persona_correction_failopen` action and emits a LOUD diagnostic so the send path
# delivers the byte-bearing payload anyway, never dropping it forever. The value is
# pinned at the attempt count A4 observed stuck (`attempts=4`) so the live custodes
# enforcement channel fails open instead of suppressing on that exact evidence.
PERSONA_FAILOPEN_ATTEMPTS = 4
PANE_CLOSE_TRANSIENT_OPTIONS = (
    "@INSTANCE_ID",
    "@PANE_LABEL",
    "@CC_STATE",
    "@TTS_STATE",
    "@CONTEXT_INFO",
    "@STACK_PENDING",
    "@ACTIVE_TITLE",
    "@PROGRESS_TITLE",
    "@PANE_PROGRESS",
    "@GT_FIRE",
    "@PLANNING_STATE",
    "@PLANNING_AGENT",
    "@DISCORD_VOICE_LOCK",
    "@DISCORD_VOICE_PROCESSING",
)


def _observed_row_hash(row, spec: PersonaSpec) -> str:
    """Fingerprint exactly the columns `_row_matches_persona` consults.

    If this hash is unchanged between two ticks, the predicate's verdict cannot
    have changed either, so a resend is provably useless.
    """
    fields = {
        "persona": spec.persona,
        "instance_id": getattr(row, "instance_id", "") if row is not None else "",
        "pane_label": getattr(row, "pane_label", "") if row is not None else "",
        # Canonical identity the predicate now keys on first — must be in the
        # fingerprint so a slug change (e.g. "" -> "custodes") counts as new input
        # and the guard re-evaluates instead of suppressing on stale equivalence.
        "persona_slug": (
            (getattr(row, "persona_slug", "") or "").strip().lower() if row is not None else ""
        ),
        "legion": getattr(row, "legion", "") if row is not None else "",
        "tab_name": (getattr(row, "tab_name", "") or "") if row is not None else "",
        "instance_type": getattr(row, "instance_type", "") if row is not None else "",
        "primarch": (getattr(row, "primarch", "") or "") if row is not None else "",
    }
    return hashlib.sha1(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def _read_persona_guard(adapter: TmuxAdapter, pane_id: str) -> dict[str, Any]:
    raw = adapter.show_pane_option(pane_id, PERSONA_GUARD_OPTION)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _write_persona_guard(adapter: TmuxAdapter, pane_id: str, payload: dict[str, Any]) -> None:
    adapter.run(
        "set-option",
        "-p",
        "-t",
        pane_id,
        PERSONA_GUARD_OPTION,
        json.dumps(payload, sort_keys=True),
        allow_failure=True,
    )


def _clear_persona_guard(adapter: TmuxAdapter, pane_id: str) -> None:
    adapter.run("set-option", "-pu", "-t", pane_id, PERSONA_GUARD_OPTION, allow_failure=True)


def _guarded_send_persona_command(
    adapter: TmuxAdapter, pane_id: str, spec: PersonaSpec, row
) -> tuple[bool, str, str]:
    """Send `/persona` at most once per unchanged observed row.

    Returns ``(sent, reason, action)``. When the observed row is identical to the
    one recorded at the previous send and the backoff window has not elapsed, the
    send is suppressed, a ``persona_assertion_stuck`` event is emitted (so the
    underlying predicate bug surfaces for the next FG dispatch instead of silently
    spamming), and ``action`` is ``persona_correction_suppressed``.
    """
    row_hash = _observed_row_hash(row, spec)
    guard = _read_persona_guard(adapter, pane_id)
    now = time.time()
    same_input = guard.get("persona") == spec.persona and guard.get("row_hash") == row_hash

    if same_input:
        attempts = int(guard.get("attempts", 1) or 1) + 1
        elapsed = now - float(guard.get("ts", 0) or 0)
        if elapsed < PERSONA_GUARD_BACKOFF_SECONDS:
            guard["attempts"] = attempts
            _write_persona_guard(adapter, pane_id, guard)
            observed_row = {
                "instance_id": getattr(row, "instance_id", "") if row is not None else "",
                "legion": getattr(row, "legion", "") if row is not None else "",
                "tab_name": (getattr(row, "tab_name", "") or "") if row is not None else "",
                "instance_type": getattr(row, "instance_type", "") if row is not None else "",
            }
            if attempts >= PERSONA_FAILOPEN_ATTEMPTS:
                # FAIL OPEN. The correction is provably stuck — re-sending `/persona`
                # cannot change a verdict that has already failed against this exact
                # registry row N times. We reach this branch only when the live
                # runtime IS present (runtime_ok=True, row is not None), so the pane
                # is deliverable: stop suppressing the payload, emit a LOUD diagnostic,
                # and signal the send path to deliver. Suppress-on-stuck was the
                # anti-pattern that silently dropped every enforcement send to %25.
                log_event(
                    "persona_assert_failopen",
                    instance_id=getattr(row, "instance_id", "") if row is not None else "",
                    details={
                        "pane": pane_id,
                        "persona": spec.persona,
                        "pane_label": spec.pane_label,
                        "predicate": "_row_matches_persona",
                        "attempts": attempts,
                        "threshold": PERSONA_FAILOPEN_ATTEMPTS,
                        "resolution": (
                            "persona correction is stuck against an unchanged registry "
                            "row; failing open — the live runtime is present so the "
                            "payload is delivered and the persona mismatch is left for "
                            "the next FG dispatch / restart to reconcile"
                        ),
                        "observed_row": observed_row,
                    },
                )
                return (
                    False,
                    f"persona_assert_failopen attempts={attempts}",
                    "persona_correction_failopen",
                )
            log_event(
                "persona_assertion_stuck",
                instance_id=getattr(row, "instance_id", "") if row is not None else "",
                details={
                    "pane": pane_id,
                    "persona": spec.persona,
                    "pane_label": spec.pane_label,
                    "predicate": "_row_matches_persona",
                    "attempts": attempts,
                    "backoff_seconds": PERSONA_GUARD_BACKOFF_SECONDS,
                    "elapsed_seconds": round(elapsed, 1),
                    "failopen_at": PERSONA_FAILOPEN_ATTEMPTS,
                    "observed_row": observed_row,
                },
            )
            return (
                False,
                f"persona_assert_suppressed_stuck attempts={attempts}",
                "persona_correction_suppressed",
            )
        # Backoff elapsed — the pane may have recovered in a way we cannot observe
        # (e.g. live runtime healthy but registry write lagging). Allow one more
        # attempt, preserving the escalating attempt count.

    sent, reason = _send_persona_command(adapter, pane_id, spec.persona)
    if sent:
        _write_persona_guard(
            adapter,
            pane_id,
            {
                "persona": spec.persona,
                "row_hash": row_hash,
                "ts": now,
                "attempts": (int(guard.get("attempts", 0) or 0) + 1) if same_input else 1,
            },
        )
    return sent, reason, ("persona_correction_sent" if sent else "persona_correction_failed")


def _guarded_note_unregistered(
    adapter: TmuxAdapter, pane_id: str, spec: PersonaSpec
) -> tuple[bool, str, str]:
    """Surface a live persona pane that has NO registry row at all — without spamming.

    Injecting ``/persona`` here is a proven no-op: for a singleton pane the persona
    skill verifies-and-reports rather than self-PATCHing (by design — registration
    is an infrastructure invariant, not the agent's job). The only component that
    can correctly create the row is the agent's own SessionStart, which holds the
    session_id and the full identity derivation and fires on (re)start;
    ``instances-clear`` now preserves persona rows so the watchdog reactivates them
    in place thereafter. So we do NOT inject — re-injecting ``/persona`` every tick
    only burned the persona's model (Opus, on the Administratum pane) forever
    without ever creating the row. Instead emit a distinct, actionable diagnostic
    once per backoff window and let the operator / a restart register the row.

    Returns ``(False, reason, action)`` — never "sent"; the action is
    ``persona_unregistered_noted`` (fresh) or ``persona_unregistered_suppressed``
    (within backoff).
    """
    row_hash = _observed_row_hash(None, spec)
    guard = _read_persona_guard(adapter, pane_id)
    now = time.time()
    same_input = guard.get("persona") == spec.persona and guard.get("row_hash") == row_hash

    if same_input and (now - float(guard.get("ts", 0) or 0)) < PERSONA_GUARD_BACKOFF_SECONDS:
        attempts = int(guard.get("attempts", 1) or 1) + 1
        guard["attempts"] = attempts
        _write_persona_guard(adapter, pane_id, guard)
        return (
            False,
            f"persona_unregistered_suppressed attempts={attempts}",
            "persona_unregistered_suppressed",
        )

    log_event(
        "persona_unregistered_live_runtime",
        details={
            "pane": pane_id,
            "pane_label": spec.pane_label,
            "expected_persona": spec.persona,
            "remedy": (
                f"restart this pane so SessionStart registers the row "
                f"(primarch={spec.persona}); /persona is a no-op for singleton panes "
                f"and instances-clear now preserves the row for reactivation"
            ),
        },
    )
    _write_persona_guard(
        adapter,
        pane_id,
        {
            "persona": spec.persona,
            "row_hash": row_hash,
            "ts": now,
            "attempts": (int(guard.get("attempts", 0) or 0) + 1) if same_input else 1,
        },
    )
    return False, "persona_unregistered_live_runtime", "persona_unregistered_noted"


def _stop_rows(rows, *, pane_id: str, pane_label: str, reason: str) -> None:
    for row in rows:
        try:
            stop_instance(row.instance_id)
            log_event(
                "assert_instance_repaired",
                instance_id=row.instance_id,
                details={
                    "pane": pane_id,
                    "pane_label": pane_label,
                    "repair": "stopped",
                    "reason": reason,
                },
            )
        except Exception as exc:
            log_event(
                "assert_instance_mismatch",
                instance_id=row.instance_id,
                details={
                    "pane": pane_id,
                    "pane_label": pane_label,
                    "reason": reason,
                    "stop_error": str(exc),
                },
            )


def _assert_persona_color(adapter: TmuxAdapter, pane_id: str, spec: PersonaSpec) -> None:
    current = adapter.run("display-message", "-p", "#{pane_id}", allow_failure=True).strip()
    if current != pane_id:
        return
    voice_locked = adapter.run(
        "show-options",
        "-pqv",
        "-t",
        pane_id,
        "@DISCORD_VOICE_LOCK",
        allow_failure=True,
    ).strip()
    if voice_locked == "1":
        return
    if spec.persona == "custodes":
        adapter.run("select-pane", "-t", pane_id, "-P", "bg=#302800", allow_failure=True)
    elif spec.persona == "fabricator-general":
        adapter.run("select-pane", "-t", pane_id, "-P", "bg=#300808", allow_failure=True)


def _clear_pane_overlay(adapter: TmuxAdapter, pane_id: str) -> None:
    """Clear close-time pane chrome/state without touching durable pane identity."""
    current = adapter.run("display-message", "-p", "#{pane_id}", allow_failure=True).strip()
    pane_label = adapter.show_pane_option(pane_id, "@PANE_ID")
    if current == pane_id:
        adapter.run("select-pane", "-t", pane_id, "-P", "bg=default", allow_failure=True)
    adapter.run("select-pane", "-t", pane_id, "-T", "", allow_failure=True)
    adapter.run(
        "set-option", "-p", "-t", pane_id, "@PANE_TITLE_SUPPRESS", "true", allow_failure=True
    )
    for option in PANE_CLOSE_TRANSIENT_OPTIONS:
        adapter.run("set-option", "-pu", "-t", pane_id, option, allow_failure=True)
    if pane_label not in PERSONA_LABELS:
        adapter.run("set-option", "-pu", "-t", pane_id, PERSONA_GUARD_OPTION, allow_failure=True)


def _row_matches_persona(row, spec: PersonaSpec) -> bool:
    if row is None:
        return False
    tab = (getattr(row, "tab_name", "") or "").lower()
    # CANONICAL identity first. Post sync-decouple, /api/instances exposes the
    # instances.persona_id JOIN as persona.slug (carried here as `persona_slug`)
    # — the same identity personas.resolve_live_persona_instance resolves on
    # (persona slug + rank != 'retired'; rank is already filtered to non-retired
    # upstream by the registry's active-set selection). When the slug is present it
    # is authoritative: it identifies the singleton directly, so we never fall
    # through to the legacy legion/primarch/instance_type columns the API dropped.
    # This is the Symptom-2 fix: the old custodes branch required
    # `instance_type in {sync, hook_driven}` — a sync MODE, not identity — so a
    # correctly-registered custodes (slug=custodes, legion column gone) failed the
    # predicate and re-armed the `/persona custodes` injection loop every tick.
    slug = (getattr(row, "persona_slug", "") or "").strip().lower()
    if slug:
        return slug == spec.persona
    # LEGACY fallbacks for rows/sources predating the persona_slug surface.
    if spec.persona == "custodes":
        # Identity is the custodes legion (persona slug), never sync mode.
        return row.legion == "custodes" or spec.persona in tab
    if spec.persona == "malcador":
        # Malcador is a singleton primarch sharing the `astartes` legion with the
        # regiment workers, so legion cannot identify it — its load-bearing key is
        # `primarch='malcador'` (the same column the registry seeds and dispatch
        # resolves on), mirroring Administratum. Keying on primarch decouples the
        # match from agent self-naming: a freshly registered row has
        # tab_name='needs-name' yet IS Malcador, so requiring the persona substring
        # in tab_name would re-arm the correction loop. tab_name stays a fallback
        # for rows predating the primarch column.
        return row.pane_label == spec.pane_label and (
            getattr(row, "primarch", "") == "malcador" or spec.persona in tab
        )
    if spec.persona == "fabricator-general":
        # FG owns a dedicated legion (`fabricator`, see ALLOWED_LEGIONS in the
        # token-api). Prefer that DB-level identity column; tab_name reflects
        # current *work* (e.g. "fg-observed-agents-cutoff"), not persona identity,
        # so requiring the persona substring there falsely fails and drives the
        # sisyphus resend loop. tab_name stays a fallback for rows that have not
        # yet written their legion.
        return row.pane_label == spec.pane_label and (
            row.legion == "fabricator" or spec.persona in tab
        )
    if spec.persona == "administratum":
        # Administratum shares the `mechanicus` legion with worker panes, so legion
        # cannot identify it — its load-bearing key is `primarch='administratum'`
        # (the same column the token-api `_resolve_administratum_instance`
        # dispatcher resolves on). Keying on primarch decouples the match from the
        # agent self-naming: a freshly SessionStart-registered row has
        # tab_name='needs-name' yet IS the recorder, so requiring the persona
        # substring in tab_name re-armed the correction loop until the agent ran
        # `instance-name`. tab_name stays a fallback for rows predating the
        # primarch column.
        return row.pane_label == spec.pane_label and (
            getattr(row, "primarch", "") == "administratum" or spec.persona in tab
        )
    # Fallback for any other persona pane: stable pane_label plus persona-derived
    # tab name.
    return row.pane_label == spec.pane_label and spec.persona in tab


def _base_result(pane_id: str, pane_label: str, pane_type: str, row) -> dict[str, Any]:
    return {
        "ok": False,
        "pane": pane_id,
        "pane_label": pane_label,
        "pane_type": pane_type,
        "instance_id": row.instance_id if row else "",
        "action": "none",
        "reason": "",
    }


def assert_instance(
    adapter: TmuxAdapter,
    target: str,
    *,
    upsert: dict[str, Any] | None = None,
    prune: bool = False,
) -> dict[str, Any]:
    from .focus_guard import preserve_focus

    with preserve_focus(adapter, source="tmuxctl assert-instance", attempted_target=target):
        return _assert_instance_impl(adapter, target, upsert=upsert, prune=prune)


def _assert_instance_impl(
    adapter: TmuxAdapter,
    target: str,
    *,
    upsert: dict[str, Any] | None = None,
    prune: bool = False,
) -> dict[str, Any]:
    # upsert/prune are accepted only for internal compatibility; public CLI no longer exposes them.
    resolved = resolve_pane(adapter, target)
    pane_id = resolved.pane_id
    pane_label = _pane_label(adapter, pane_id, resolved.pane_role)
    pane_type = _pane_type(adapter, pane_id)
    runtime_ok = _runtime_has_instance(adapter, pane_id)
    rows = _registry_entries(pane_id, pane_label)
    row = rows[0] if rows else None
    result = _base_result(pane_id, pane_label, pane_type, row)

    def finish(result: dict[str, Any], *, clear_failed: bool = True) -> dict[str, Any]:
        if clear_failed and not result.get("ok"):
            _clear_pane_overlay(adapter, pane_id)
        return result

    if pane_label in PERSONA_LABELS:
        spec = persona_spec(pane_label)
        if not runtime_ok:
            if rows:
                _stop_rows(
                    rows, pane_id=pane_id, pane_label=pane_label, reason="persona_runtime_dead"
                )
            launch_upsert = {
                "engine": spec.engine,
                "persona": spec.persona,
                "instance_type": spec.instance_type,
                "session_doc": spec.session_doc,
                "sync": spec.sync,
                "model": spec.model,
                "no_gt": not spec.sync,
                # Launch the persona from the vault, not $HOME. Empty resolves the
                # Imperium-ENV vault (mount-guarded); _dispatch_args omits --dir when blank.
                "dir": spec.working_dir or _persona_working_dir(),
            }
            ok, reason = _launch(pane_id, launch_upsert, str((upsert or {}).get("prompt") or ""))
            result.update(
                {"ok": ok, "action": "launched" if ok else "launch_failed", "reason": reason}
            )
            return finish(result, clear_failed=False)
        if row is not None and not _row_matches_persona(row, spec):
            sent, reason, action = _guarded_send_persona_command(adapter, pane_id, spec, row)
            log_event(
                "assert_instance_mismatch",
                instance_id=row.instance_id,
                details={
                    "pane": pane_id,
                    "pane_label": pane_label,
                    "expected_persona": spec.persona,
                    "actual_legion": row.legion,
                    "actual_tab_name": getattr(row, "tab_name", ""),
                    "action": action,
                },
            )
            result.update({"ok": False, "action": action, "reason": reason})
            # The persona correction is stuck past its bounded attempts but the live
            # runtime IS present (we only reach this branch with runtime_ok=True and a
            # real row). Mark the pane deliverable so the send path fails open and
            # delivers the payload instead of treating the stuck correction as a dead
            # pane — payload delivery is primary, the correction is secondary.
            if action == "persona_correction_failopen":
                result["deliverable"] = True
            return finish(result, clear_failed=False)
        if row is None:
            stopped_rows = _registry_entries(pane_id, pane_label, include_stopped=True)
            stopped_match = next(
                (
                    candidate
                    for candidate in stopped_rows
                    if candidate.status is InstanceStatus.STOPPED
                    and _row_matches_persona(candidate, spec)
                ),
                None,
            )
            if stopped_match is not None:
                try:
                    update_instance_activity(stopped_match.instance_id, "prompt_submit")
                    if spec.persona == "custodes":
                        # Plan-mode exits can mark the row stopped and synced=0 while
                        # the live Custodes runtime remains in-pane. Reactivation must
                        # restore synced=true too; color/state-hook predicates depend on it.
                        patch_instance(stopped_match.instance_id, "synced", {"synced": True})
                        patch_instance(stopped_match.instance_id, "legion", {"legion": "custodes"})
                    _assert_persona_color(adapter, pane_id, spec)
                    _clear_persona_guard(adapter, pane_id)
                    result.update(
                        {
                            "ok": True,
                            "instance_id": stopped_match.instance_id,
                            "action": "registry_reactivated",
                            "reason": "live_runtime_stopped_registry_row_reactivated",
                        }
                    )
                    return finish(result, clear_failed=False)
                except Exception as exc:
                    log_event(
                        "assert_instance_mismatch",
                        instance_id=stopped_match.instance_id,
                        details={
                            "pane": pane_id,
                            "pane_label": pane_label,
                            "reason": "reactivate_stopped_registry_failed",
                            "error": str(exc),
                        },
                    )
                    result.update(
                        {
                            "ok": False,
                            "instance_id": stopped_match.instance_id,
                            "action": "registry_reactivation_failed",
                            "reason": "reactivate_stopped_registry_failed",
                        }
                    )
                    return finish(result, clear_failed=False)
            # Live runtime, no registry row at all (not even a stopped one to
            # reactivate). Do NOT inject `/persona` — it is a no-op for singleton
            # panes and re-firing it every tick burned the persona's model forever.
            # Surface the anomaly loudly + back off; SessionStart on restart creates
            # the row, and instances-clear now preserves it for later reactivation.
            noted, reason, action = _guarded_note_unregistered(adapter, pane_id, spec)
            result.update({"ok": False, "action": action, "reason": reason})
            return finish(result, clear_failed=False)
        _assert_persona_color(adapter, pane_id, spec)
        _clear_persona_guard(adapter, pane_id)
        result.update({"ok": True, "reason": "live"})
        return finish(result, clear_failed=False)

    if pane_type == "stack-worker":
        if not runtime_ok:
            if rows:
                _stop_rows(
                    rows, pane_id=pane_id, pane_label=pane_label, reason="stack_worker_runtime_dead"
                )
            adapter.run("set-option", "-pu", "-t", pane_id, "@PANE_ID", allow_failure=True)
            adapter.run("set-option", "-pu", "-t", pane_id, "@PANE_TYPE", allow_failure=True)
            adapter.run("kill-pane", "-t", pane_id, allow_failure=True)
            result.update({"ok": False, "action": "pruned", "reason": "stack_worker_runtime_dead"})
            return finish(result)
        ok = row is not None
        result.update({"ok": ok, "reason": "live" if ok else "no_registry_instance"})
        return finish(result)

    if not runtime_ok and rows:
        _stop_rows(rows, pane_id=pane_id, pane_label=pane_label, reason="structured_runtime_dead")
    ok = runtime_ok and row is not None
    result.update(
        {
            "ok": ok,
            "reason": "live"
            if ok
            else ("no_runtime_instance" if not runtime_ok else "no_registry_instance"),
        }
    )
    return finish(result)


def assert_persona(
    adapter: TmuxAdapter, pane_label: str, *, prompt: str = "", session: str = "main"
) -> dict[str, Any]:
    # Compatibility helper for in-process callers; public CLI surface is assert-instance.
    persona_spec(pane_label)
    try:
        pane_id = resolve_pane(adapter, pane_label).pane_id
    except ValueError:
        from .stack import add_orchestrator_stack_pane

        base = pane_label.split(":", 1)[0]
        add_orchestrator_stack_pane(adapter, session, base)
        pane_id = resolve_pane(adapter, pane_label).pane_id
    result = assert_instance(adapter, pane_id)
    if result.get("ok") and prompt:
        ok, reason = _upsert_prompt(pane_id, prompt)
        result.update(
            {
                "ok": ok,
                "dispatched": ok,
                "action": "prompt_sent" if ok else "prompt_failed",
                "reason": reason,
            }
        )
    else:
        result["dispatched"] = False
    return result
