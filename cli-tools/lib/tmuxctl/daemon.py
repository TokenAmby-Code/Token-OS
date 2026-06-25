"""tmuxctld — the standalone HTTP-loopback daemon face of tmuxctl.

This is "what the composite of every tmuxctl command looks like if refactored
into a self-sufficient daemon." It is stdlib-only on purpose: tmuxctl is verified
zero-third-party (the satellite shells it under a bare interpreter; ``bin/tmuxctl``
runs any python3.11+). FastAPI would force a venv; ``http.server`` does not.

Design (locked decisions):

* Framework — ``ThreadingHTTPServer`` + ``BaseHTTPRequestHandler``.
* Concurrency — a FRESH, cheap ``TmuxControlPlane(TmuxAdapter())`` per request.
  The adapter carries non-thread-safe mutable state (``last_send_gate_result``,
  ``focus_mutation_count``, ``_resolving_targets``); one adapter per request
  isolates it under threading. One request == one logical op.
* Envelope — ``{ok:true, result}`` / ``{ok:false, error:{code,message,detail}}``
  at HTTP 200 unless a transport failure (bad JSON body -> 400, unknown route ->
  404). A dead/missing pane is a structured ``found:false``, never a 500.
  ``TmuxSendGated`` -> ``{ok:false, error:{code:"gated", detail:<gate>}}`` at 200
  (zero bytes sent, re-queueable). The daemon gets the send-gate + focus-guard
  for free via ``adapter.run()``.
* ``/health`` is the one un-enveloped surface: it returns the flat
  ``{ok,tmux_reachable,version,sha,port}`` shape (the satellite/watchdog contract).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .api import RegistryError
from .service import TmuxControlPlane
from .tmux_adapter import TmuxAdapter, TmuxError, TmuxSendGated, prompt_payload_hash

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7778

# The daemon is unauthenticated and does powerful tmux ops — it binds loopback
# ONLY. serve() refuses any other --host (fail-closed).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Server-side log. Full exception detail is logged HERE (launchd captures stderr)
# and never leaked into the HTTP JSON response (clients get a generic message).
log = logging.getLogger("tmuxctld")

_RAW_TMUX_ID_RX = re.compile(r"%\d+")
_PUBLIC_PANE_ID_RX = re.compile(r"^[^:%\s]+:[^:%\s]+$")


class PromptSubmitSniffer:
    """In-memory UserPromptSubmit acknowledgement bus for daemon send transactions.

    The daemon is the only process that can know "I issued this prompt send" at
    the exact time the bytes hit tmux. Token-API owns the agent hook receiver.
    The hook handler echoes UserPromptSubmit facts back here; the daemon waits
    on that echo before reporting a prompt send as verified.
    """

    def __init__(self, *, max_events: int = 2048) -> None:
        self._cond = threading.Condition()
        self._events: deque[dict] = deque(maxlen=max_events)

    def record(self, payload: dict) -> dict:
        instance_id = str(
            payload.get("session_id") or payload.get("instance_id") or payload.get("id") or ""
        ).strip()
        prompt_hash = str(payload.get("prompt_hash") or payload.get("payload_hash") or "").strip()
        event = {
            "event": "UserPromptSubmit",
            "instance_id": instance_id,
            "pane": str(payload.get("pane") or "").strip(),
            "prompt_hash": prompt_hash,
            "at": time.monotonic(),
            "wall_time": time.time(),
        }
        with self._cond:
            self._events.append(event)
            self._cond.notify_all()
        return event

    @staticmethod
    def _matches(
        event: dict,
        *,
        instance_id: str,
        payload_hash: str,
        since: float,
    ) -> bool:
        if event.get("at", 0) < since:
            return False
        if instance_id and event.get("instance_id") != instance_id:
            return False
        event_hash = str(event.get("prompt_hash") or "").strip()
        # Prefer exact hash matching when the hook surface supplies it. Some
        # current Claude/Codex UserPromptSubmit payloads do not; in that case,
        # same-instance-after-send is still a real submit acknowledgement.
        if event_hash and payload_hash and event_hash != payload_hash:
            return False
        return True

    def wait(
        self,
        *,
        instance_id: str,
        payload_hash: str,
        since: float,
        timeout: float,
    ) -> dict | None:
        if not instance_id or timeout <= 0:
            return None
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for event in reversed(self._events):
                    if self._matches(
                        event,
                        instance_id=instance_id,
                        payload_hash=payload_hash,
                        since=since,
                    ):
                        return dict(event)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)


_PROMPT_SUBMIT_SNIFFER = PromptSubmitSniffer()


def _safe_public_role(role: str | None) -> str:
    """Redact a pane role to a canonical ``{page}:{id}`` or ``unresolved``.

    Mirrors ``cli.py._safe_public_role`` — a raw tmux ``%NN`` (or any non-public
    shape) is never allowed through to a response. Keeps the daemon's freelist
    canonical-only, matching the CLI and the canonical-id campaign.
    """
    value = (role or "").strip()
    if not value or _RAW_TMUX_ID_RX.search(value):
        return "unresolved"
    return value if _PUBLIC_PANE_ID_RX.fullmatch(value) else "unresolved"


# ---------------------------------------------------------------------------
# Boot-time metadata (version / sha)
# ---------------------------------------------------------------------------


def read_version() -> str:
    """cli-tools package version from pyproject (``unknown`` on failure)."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            match = re.match(r'\s*version\s*=\s*"([^"]+)"', line)
            if match:
                return match.group(1)
    except Exception:
        pass
    return "unknown"


def read_sha() -> str:
    """Short git sha of the checkout the daemon booted from (``unknown`` on failure)."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parents[2]),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        sha = proc.stdout.strip()
        return sha or "unknown"
    except Exception:
        return "unknown"


# The write-only liveness heartbeat (`~/.claude/tmuxctld-heartbeat.json` + its
# 30s writer thread) was RETIRED with the daemon-native persona work: nothing ever
# read it (it was scaffolding for a never-built StartInterval watchdog). /health is
# the live liveness contract; launchd `KeepAlive` owns process supervision. Do NOT
# reintroduce a heartbeat-file poller — prefer /health.


def tmux_reachable(adapter: TmuxAdapter) -> bool:
    """Cheap fail-closed probe: can we list sessions at all?"""
    try:
        adapter.list_sessions()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Param coercion helpers (query params arrive as strings; JSON body keeps types)
# ---------------------------------------------------------------------------


def _s(params: dict, key: str, default: str = "") -> str:
    val = params.get(key, default)
    return default if val is None else str(val)


def _opt(params: dict, key: str) -> str | None:
    val = params.get(key)
    return None if val is None else str(val)


def _b(params: dict, key: str, default: bool = False) -> bool:
    val = params.get(key, default)
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


def _f(params: dict, key: str, default: float) -> float:
    val = params.get(key)
    return default if val in (None, "") else float(val)


def _i(params: dict, key: str, default: int) -> int:
    val = params.get(key)
    return default if val in (None, "") else int(val)


def _window_ref(control: TmuxControlPlane, value: str) -> tuple[str, int]:
    value = value or "current"
    if value == "current":
        session = control.adapter.current_session_name()
        raw = control.adapter.run("display-message", "-p", "#{window_index}").strip()
        return session, int(raw)
    if ":" not in value:
        raise ValueError("window must look like session:index or use 'current'")
    session_name, raw_index = value.split(":", 1)
    return session_name, int(raw_index)


# ---------------------------------------------------------------------------
# Route handlers: (control, params) -> dict | list | str. Wrapped centrally in
# the envelope by the handler's try/except.
# ---------------------------------------------------------------------------


# -- Resolution (GET) -------------------------------------------------------


def _h_resolve_instance(control, params):
    # Resolve an instance UUID to its live pane. CANONICAL-ONLY: the public
    # {page}:{id} role is the sole external pane identity — a raw physical %NN is
    # never returned (the canonical-id invariant; consumers that need to act on
    # the pane use the /instance/* ops, which resolve internally). Fails closed:
    # no live pane -> found:false, pane_id:"".
    instance_id = _s(params, "instance_id")
    resolved = control.resolve_instance(instance_id)
    pane_id = _safe_public_role(resolved["pane_role"]) if resolved["found"] else ""
    return {
        "instance_id": instance_id,
        "found": resolved["found"],
        "pane_id": pane_id,
        "pane_role": pane_id,
    }


def _h_instance_id_for_pane(control, params):
    # Reverse of resolve-instance: read a pane's live @INSTANCE_ID stamp. Fails
    # closed — an unstamped or dead pane yields instance_id:"" / found:false.
    return control.instance_id_for_pane(_s(params, "pane", "current"))


def _h_resolve_pane(control, params):
    target = _s(params, "target")
    fmt = _s(params, "format", "full")
    if fmt == "id":
        return control.public_pane_id(target)
    if fmt == "physical":
        return control.physical_pane_id(target)
    if fmt == "json":
        values: dict[str, str] = {}
        for line in control.resolve_pane(target).splitlines():
            key, value = line.split(": ", 1)
            values[key] = value
        return values
    return control.resolve_pane(target)


def _h_resolve_agent(control, params):
    return control.resolve_agent(
        _s(params, "pane", "current"),
        _s(params, "agent", "auto"),
        default=_s(params, "default", "claude"),
    )


def _h_freelist(control, params):
    # Redact raw physical ids to the canonical role (mirrors the CLI json shape):
    # both pane_id and pane_role carry the public {page}:{id}, never a raw %NN.
    out = []
    for free_pane in control.freelist():
        role = _safe_public_role(free_pane.get("pane_role"))
        out.append({"pane_id": role, "pane_role": role, "window_name": free_pane["window_name"]})
    return out


def _h_session_doc(control, params):
    return control.session_doc_for_pane(_s(params, "pane", "current"))


def _h_translate_ids(control, params):
    text = _s(params, "text")
    return control.translate_ids(text, unresolved=_s(params, "unresolved", "unresolved"))


# -- Inspection (GET) -------------------------------------------------------


def _h_inspect_workspace(control, params):
    return control.inspect_workspace(_s(params, "session", "main"), physical=_b(params, "physical"))


def _h_inspect_window(control, params):
    session_name, window_index = _window_ref(control, _s(params, "window", "current"))
    return control.inspect_window(session_name, window_index, physical=_b(params, "physical"))


def _h_inspect_pane(control, params):
    return control.inspect_pane(_s(params, "pane"), physical=_b(params, "physical"))


def _h_inspect_restart_plan(control, params):
    return control.inspect_restart_plan(_s(params, "session", "main"))


def _h_doctor(control, params):
    return control.doctor(_s(params, "session", "main"))


def _h_instance_show_option(control, params):
    return control.instance_show_option(_s(params, "instance_id"), _s(params, "option"))


# -- Send + act (POST) ------------------------------------------------------


def _h_send_keys(control, params):
    pane = _s(params, "pane")
    command = _s(params, "command")
    if _b(params, "no_escape"):
        control.adapter.run("send-keys", "-t", pane, "-l", command)
    else:
        control.adapter.send_keys(pane, command)
    # adapter.run() suppresses a gated send SILENTLY (sets last_send_gate_result,
    # returns ""), so — like send_text_then_submit — surface the structured gate
    # instead of falsely reporting sent:True. Dispatch turns this into the
    # {ok:false, error:{code:"gated"}} envelope (zero bytes written, re-queueable).
    gate = getattr(control.adapter, "last_send_gate_result", None)
    if gate:
        raise TmuxSendGated(gate)
    return {"pane": pane, "sent": True}


def _h_send_text(control, params):
    pane = _s(params, "pane")
    text = _s(params, "text")
    submit = _b(params, "submit", True)
    clear_prompt = _b(params, "clear_prompt")
    verify = _b(params, "verify", submit)
    verify_timeout = _f(params, "verify_timeout", 5.0)
    submit_settle_seconds = _f(params, "submit_settle_seconds", 1.0)
    ack_submit_retries = _i(params, "ack_submit_retries", 2)
    pre_submit_raw = params.get("pre_submit_keys", ())
    if isinstance(pre_submit_raw, str):
        pre_submit_keys = tuple(k for k in pre_submit_raw.split(",") if k)
    elif isinstance(pre_submit_raw, list | tuple):
        pre_submit_keys = tuple(str(k) for k in pre_submit_raw if str(k))
    else:
        pre_submit_keys = ()

    if not submit:
        return control.send_text(pane, text, clear_prompt=clear_prompt, submit=False)

    payload_hash = prompt_payload_hash(text)
    dispatch_id = str(uuid.uuid4())
    instance_id = ""
    try:
        instance_id = str(control.instance_id_for_pane(pane).get("instance_id") or "").strip()
    except Exception:
        instance_id = ""

    started = time.monotonic()
    if hasattr(control.adapter, "send_text_then_submit"):
        control.adapter.send_text_then_submit(
            pane,
            text,
            clear_prompt=clear_prompt,
            pre_submit_keys=pre_submit_keys,
            submit_settle_seconds=submit_settle_seconds,
        )
    else:
        normalized = re.sub(r"[\r\n]+", " ", text).rstrip()
        if not normalized.strip():
            raise ValueError("prompt payload is empty after normalization")
        if clear_prompt:
            control.adapter.send_keys(pane, "C-u")
        control.adapter.run("send-keys", "-t", pane, "-l", normalized)
        gate = getattr(control.adapter, "last_send_gate_result", None)
        if gate and gate.get("suppressed"):
            raise TmuxSendGated(gate)
        # Test-adapter fallback. Real daemon sends use TmuxAdapter's canonical
        # method above; callers must not assemble send-keys outside tmuxctld.
        if submit_settle_seconds > 0:
            time.sleep(submit_settle_seconds)
        for key in pre_submit_keys:
            control.adapter.send_keys(pane, key)
        if pre_submit_keys and submit_settle_seconds > 0:
            time.sleep(submit_settle_seconds)
        control.adapter.send_keys(pane, "C-m")
        if submit_settle_seconds > 0:
            time.sleep(submit_settle_seconds)
        control.adapter.send_keys(pane, "C-m")

    def _send_submit_key() -> None:
        if hasattr(control.adapter, "send_keys"):
            control.adapter.send_keys(pane, "C-m")
        else:
            control.adapter.run("send-keys", "-t", pane, "C-m")

    ack = None
    if verify:
        for attempt in range(max(0, ack_submit_retries) + 1):
            ack = _PROMPT_SUBMIT_SNIFFER.wait(
                instance_id=instance_id,
                payload_hash=payload_hash,
                since=started,
                timeout=verify_timeout,
            )
            if ack or attempt >= max(0, ack_submit_retries):
                break
            # Handshake recovery: if the TUI swallowed the prior submit as a
            # prompt newline, a later standalone carriage return is the proven
            # white-whale recovery key. The daemon owns this retry; callers do
            # not pile on their own raw send-keys.
            _send_submit_key()
            if submit_settle_seconds > 0:
                time.sleep(submit_settle_seconds)
    verification_status = "submitted" if ack else ("unverified" if verify else "not_requested")
    return {
        "status": "submitted" if ack else "unverified",
        "pane": pane,
        "instance_id": instance_id,
        "dispatch_id": dispatch_id,
        "payload_hash": payload_hash,
        "verification_status": verification_status,
        "verified_by": "UserPromptSubmit" if ack else None,
        "ack": ack,
    }


def _h_insert_text(control, params):
    control.insert_text(_s(params, "pane"), _s(params, "text"))
    return {"pane": _s(params, "pane"), "status": "inserted"}


def _h_prompt_start(control, params):
    control.move_to_prompt_start(_s(params, "pane"), page_ups=_i(params, "page_ups", 50))
    return {"pane": _s(params, "pane"), "status": "prompt-start"}


def _h_prompt_end(control, params):
    control.move_to_prompt_end(_s(params, "pane"), page_downs=_i(params, "page_downs", 50))
    return {"pane": _s(params, "pane"), "status": "prompt-end"}


def _h_invoke_skill(control, params):
    instance_id = _s(params, "instance_id")
    pane = _s(params, "pane", "current")
    if instance_id:
        resolved = control.resolve_instance(instance_id)
        if not resolved["found"]:
            return {"instance_id": instance_id, "found": False}
        pane = resolved["pane_id"]
    skill = _s(params, "skill")
    agent = _s(params, "agent", "auto")
    arguments = _s(params, "arguments") or None
    if _b(params, "submit"):
        rendered = control.send_skill(
            pane, skill, agent=agent, arguments=arguments, clear_prompt=_b(params, "clear_prompt")
        )
        return {"pane": pane, "submitted": True, "rendered": rendered}
    rendered = control.invoke_skill(pane, skill, agent=agent, arguments=arguments)
    return {"pane": pane, "submitted": False, "rendered": rendered}


def _h_assert_instance(control, params):
    return control.assert_instance(_s(params, "pane"))


def _h_reconcile(control, params):
    # The detached daemon has no ambient tmux session; the fleet lives in `main`.
    return {"results": control.reconcile_personas(session=_s(params, "session", "main"))}


def _h_event(control, params):
    return control.handle_event(
        _s(params, "event"), pane=_s(params, "pane"), session=_s(params, "session", "main")
    )


def _h_hook_user_prompt_submit(_control, params):
    return _PROMPT_SUBMIT_SNIFFER.record(params)


def _h_clear_runtime(control, params):
    return control.clear_runtime(_s(params, "pane"))


def _h_close_pane(control, params):
    return control.close_pane(_s(params, "pane"), timeout=_f(params, "timeout", 3.0))


def _h_close(control, params):
    return control.close_instance(
        _s(params, "instance_id"),
        lifecycle=_s(params, "lifecycle", "retire"),
        mode=_s(params, "mode", "now"),
        pane=_s(params, "pane"),
        timeout=_f(params, "timeout", 3.0),
    )


# -- Workspace + stack (POST) ----------------------------------------------


def _h_stack_add(control, params):
    return control.stack_add(
        _s(params, "base"),
        cwd=_opt(params, "cwd"),
        session=_s(params, "session", "main"),
        focus=_b(params, "focus", True),
    )


def _h_stack_dispatch(control, params):
    return control.stack_dispatch(
        _s(params, "base"),
        _s(params, "command"),
        cwd=_opt(params, "cwd"),
        session=_s(params, "session", "main"),
        focus=_b(params, "focus", True),
        settle_seconds=_f(params, "settle", 0.5),
    )


def _h_stack_adopt(control, params):
    return control.stack_adopt(
        _s(params, "base"),
        _s(params, "pane"),
        cwd=_opt(params, "cwd"),
        session=_s(params, "session", "main"),
        focus=_b(params, "focus", True),
    )


def _h_stack_enforce(control, params):
    return control.stack_enforce(
        pane=_s(params, "pane", "current"),
        window=_s(params, "window"),
        focus=_b(params, "focus"),
        admit=_b(params, "admit"),
        kill_pending_clear=_b(params, "kill_pending_clear"),
    )


def _h_stack_sweep(control, params):
    return control.stack_sweep(
        session=_s(params, "session", "main"),
        kill_pending_clear=_b(params, "kill_pending_clear", True),
    )


def _h_mechanicus_focus_selected(control, params):
    return control.mechanicus_focus_selected(_s(params, "pane", "current"))


def _h_mechanicus_enforce(control, params):
    return control.mechanicus_enforce(_s(params, "pane", "current"))


def _h_normalize(control, params):
    session_name, window_index = _window_ref(control, _s(params, "window", "current"))
    return control.normalize(session_name=session_name, window_index=window_index)


def _h_focus(control, params):
    session_name, window_index = _window_ref(control, _s(params, "window", "current"))
    return control.focus(
        session_name=session_name, window_index=window_index, mode=_s(params, "mode", "toggle")
    )


def _h_pane_select(control, params):
    return control.pane_select(
        mode=_s(params, "mode"), direction=_s(params, "direction"), client=_s(params, "client")
    )


def _h_create(control, params):
    return control.create_workspace(_s(params, "session", "main"))


def _h_rebuild_window(control, params):
    session_name, window_index = _window_ref(control, _s(params, "window", "current"))
    return control.rebuild_window(session_name=session_name, window_index=window_index)


def _h_restart(control, params):
    if _b(params, "dry_run", True):
        return {"output": control.dry_run_restart(_s(params, "session", "main"))}
    output, ok = control.execute_restart(_s(params, "session", "main"))
    return {"output": output, "ok": ok}


def _h_metal_observe(control, params):
    return control.metal_observe(_s(params, "session"))


def _h_metal_restart(control, params):
    return control.metal_restart(_s(params, "session"), dry_run=_b(params, "dry_run"))


# -- Focus-guard (POST) -----------------------------------------------------


def _h_mechanicus_focus_guard(control, params):
    return control.mechanicus_focus_guard(
        pane=_s(params, "pane"),
        client=_s(params, "client"),
        surface=_s(params, "surface", "after-select"),
    )


def _h_allow_mechanicus_focus(control, params):
    return {
        "until": control.allow_mechanicus_focus(
            seconds=_f(params, "seconds", 4.0), reason=_s(params, "reason", "explicit")
        )
    }


def _h_allow_human_mechanicus_focus(control, params):
    return control.allow_human_mechanicus_focus(
        client=_s(params, "client"),
        reason=_s(params, "reason", "explicit-human-navigation"),
    )


# -- Tombstone + audience (POST) -------------------------------------------


def _h_tombstone_jump(control, params):
    pane = control._resolve_current(_s(params, "pane", "current"))
    return control.tombstone_jump(pane, client=_s(params, "client"))


def _h_tombstone_install(control, params):
    return control.tombstone_install(
        _s(params, "slot_pane"), _s(params, "source_role"), _s(params, "target_pane")
    )


def _h_audience_toggle(control, params):
    pane = control._resolve_current(_s(params, "pane", "current"))
    return control.audience_toggle(pane, client=_s(params, "client"))


def _h_audience_return(control, params):
    pane = control._resolve_current(_s(params, "pane", "current"))
    return control.audience_return(pane, client=_s(params, "client"))


# -- Instance-id ops --------------------------------------------------------


def _h_instance_set_option(control, params):
    return control.instance_set_option(
        _s(params, "instance_id"), _s(params, "option"), _s(params, "value")
    )


def _h_instance_unset_option(control, params):
    return control.instance_unset_option(_s(params, "instance_id"), _s(params, "option"))


def _h_instance_send_text(control, params):
    instance_id = _s(params, "instance_id")
    if not _b(params, "submit", True):
        return control.instance_send_text(
            instance_id,
            _s(params, "text"),
            clear_prompt=_b(params, "clear_prompt"),
            submit=False,
        )
    resolved = control.resolve_instance(instance_id)
    if not resolved["found"]:
        return {"instance_id": instance_id, "found": False}
    result = _h_send_text(
        control,
        {
            **params,
            "pane": resolved["pane_id"],
            "verify": _b(params, "verify", True),
        },
    )
    return {**result, "instance_id": instance_id, "found": True}


def _h_instance_tint(control, params):
    return control.instance_tint(_s(params, "instance_id"), _s(params, "color"))


def _h_instance_clear_tint(control, params):
    return control.instance_clear_tint(_s(params, "instance_id"))


def _h_instance_focus(control, params):
    return control.instance_focus(
        _s(params, "instance_id"), allow=_b(params, "allow"), client=_s(params, "client")
    )


RouteHandler = Callable[["TmuxControlPlane", dict], object]

ROUTES: dict[tuple[str, str], RouteHandler] = {
    # Resolution (GET)
    ("GET", "/tmux/resolve-instance"): _h_resolve_instance,
    ("GET", "/tmux/instance-id-for-pane"): _h_instance_id_for_pane,
    ("GET", "/resolve-pane"): _h_resolve_pane,
    ("GET", "/resolve-agent"): _h_resolve_agent,
    ("GET", "/freelist"): _h_freelist,
    ("GET", "/session-doc"): _h_session_doc,
    ("GET", "/translate-ids"): _h_translate_ids,
    ("POST", "/translate-ids"): _h_translate_ids,
    # Inspection (GET)
    ("GET", "/inspect/workspace"): _h_inspect_workspace,
    ("GET", "/inspect/window"): _h_inspect_window,
    ("GET", "/inspect/pane"): _h_inspect_pane,
    ("GET", "/inspect/restart-plan"): _h_inspect_restart_plan,
    ("GET", "/doctor"): _h_doctor,
    ("GET", "/instance/show-option"): _h_instance_show_option,
    # Send + act (POST)
    ("POST", "/tmux/send-keys"): _h_send_keys,
    ("POST", "/send-text"): _h_send_text,
    ("POST", "/insert-text"): _h_insert_text,
    ("POST", "/prompt-start"): _h_prompt_start,
    ("POST", "/prompt-end"): _h_prompt_end,
    ("POST", "/invoke-skill"): _h_invoke_skill,
    ("POST", "/assert-instance"): _h_assert_instance,
    ("POST", "/hooks/user-prompt-submit"): _h_hook_user_prompt_submit,
    # Event-driven persona reconcile (replaces the retired 2-min assert-personas
    # poll). /reconcile re-seats all must-fill seats; /event ingests a single tmux
    # lifecycle event (a persona pane-died self-heal). Nothing polls these.
    ("POST", "/reconcile"): _h_reconcile,
    ("POST", "/event"): _h_event,
    ("POST", "/clear-runtime"): _h_clear_runtime,
    ("POST", "/close-pane"): _h_close_pane,
    ("POST", "/close"): _h_close,
    # Workspace + stack (POST)
    ("POST", "/stack/add"): _h_stack_add,
    ("POST", "/stack/dispatch"): _h_stack_dispatch,
    ("POST", "/stack/adopt"): _h_stack_adopt,
    ("POST", "/stack/enforce"): _h_stack_enforce,
    ("POST", "/stack/sweep"): _h_stack_sweep,
    ("POST", "/mechanicus/focus-selected"): _h_mechanicus_focus_selected,
    ("POST", "/mechanicus/enforce"): _h_mechanicus_enforce,
    ("POST", "/normalize"): _h_normalize,
    ("POST", "/focus"): _h_focus,
    ("POST", "/pane-select"): _h_pane_select,
    ("POST", "/create"): _h_create,
    ("POST", "/rebuild-window"): _h_rebuild_window,
    ("POST", "/restart"): _h_restart,
    ("POST", "/metal-observe"): _h_metal_observe,
    ("POST", "/metal-restart"): _h_metal_restart,
    # Focus-guard (POST)
    ("POST", "/mechanicus-focus-guard"): _h_mechanicus_focus_guard,
    ("POST", "/allow-mechanicus-focus"): _h_allow_mechanicus_focus,
    ("POST", "/allow-human-mechanicus-focus"): _h_allow_human_mechanicus_focus,
    # Tombstone + audience (POST)
    ("POST", "/tombstone/jump"): _h_tombstone_jump,
    ("POST", "/tombstone/install"): _h_tombstone_install,
    ("POST", "/audience/toggle"): _h_audience_toggle,
    ("POST", "/audience/return"): _h_audience_return,
    # Instance-id ops (POST)
    ("POST", "/instance/set-option"): _h_instance_set_option,
    ("POST", "/instance/unset-option"): _h_instance_unset_option,
    ("POST", "/instance/send-text"): _h_instance_send_text,
    ("POST", "/instance/tint"): _h_instance_tint,
    ("POST", "/instance/clear-tint"): _h_instance_clear_tint,
    ("POST", "/instance/focus"): _h_instance_focus,
}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class TmuxctldServer(ThreadingHTTPServer):
    """Threading HTTP server for the tmuxctld loopback control plane.

    Carries the per-process metadata (version/sha/advertised port) and the
    adapter factory the request handler uses to build a fresh control plane per
    request. :attr:`ready` is set the instant the accept loop owns the (already
    bound + listening) socket, so startup and tests gate on the real ready event
    instead of a sleep-based race.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        adapter_factory: Callable[[], TmuxAdapter] | None = None,
        version: str = "",
        sha: str = "",
        advertised_port: int | None = None,
    ) -> None:
        super().__init__(server_address, TmuxctldHandler)
        self.adapter_factory: Callable[[], TmuxAdapter] = adapter_factory or (lambda: TmuxAdapter())
        self.version = version
        self.sha = sha
        self.advertised_port = advertised_port or server_address[1]
        self.ready = threading.Event()

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        """Run the accept loop, signalling :attr:`ready` once we own the socket.

        ``server_bind()`` + ``server_activate()`` already ran in ``__init__``, so
        the socket is bound and listening by the time this is called; we set the
        ready event before entering the (blocking) accept loop so a waiter can
        connect the moment it returns.
        """
        self.ready.set()
        super().serve_forever(poll_interval)


class TmuxctldHandler(BaseHTTPRequestHandler):
    """HTTP request handler — a faithful transport over the control plane.

    It parses the request, builds a fresh ``TmuxControlPlane`` per request,
    dispatches to the matching route handler, and wraps the return in the
    ``{ok, result}`` / ``{ok:false, error}`` envelope. ALL command logic lives on
    ``TmuxControlPlane`` / the free route functions — never here.
    """

    server: TmuxctldServer  # narrow BaseServer for the typed attribute access below
    server_version = "tmuxctld/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        params: dict = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._write(400, self._error("bad_request", "invalid Content-Length"))
                return
            if length < 0:
                self._write(400, self._error("bad_request", "invalid Content-Length"))
                return
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    self._write(400, self._error("bad_request", "invalid JSON body"))
                    return
                if isinstance(body, dict):
                    params = {**params, **body}

        # /health is the one un-enveloped surface (satellite/watchdog contract).
        if method == "GET" and path == "/health":
            self._write(200, self._health_payload())
            return

        handler = ROUTES.get((method, path))
        if handler is None:
            self._write(404, self._error("not_found", f"no route for {method} {path}"))
            return

        try:
            control = TmuxControlPlane(self.server.adapter_factory())
            result = handler(control, params)
            self._write(200, {"ok": True, "result": result})
        except TmuxSendGated as exc:
            # Zero bytes were written; the structured gate result rides in detail
            # so the caller can re-queue cleanly.
            self._write(200, self._error("gated", str(exc), detail=exc.gate))
        except (TmuxError, RegistryError, ValueError, KeyError) as exc:
            # Expected, structured domain errors: the message is a deliberate,
            # author-controlled part of the API contract — safe to surface.
            self._write(200, self._error(type(exc).__name__, str(exc)))
        except Exception:  # never a 500 for a logic failure
            # Unexpected internal error: log the full exception (with traceback)
            # SERVER-SIDE and return a generic client message — raw exception
            # detail must never leak into the HTTP JSON response.
            log.exception("unhandled error dispatching %s %s", method, path)
            self._write(200, self._error("internal", "internal error"))

    def _health_payload(self) -> dict:
        return {
            "ok": True,
            "tmux_reachable": tmux_reachable(self.server.adapter_factory()),
            "version": self.server.version,
            "sha": self.server.sha,
            "port": self.server.advertised_port,
        }

    @staticmethod
    def _error(code: str, message: str, *, detail=None) -> dict:
        return {"ok": False, "error": {"code": code, "message": message, "detail": detail or ""}}

    def _write(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def log_message(self, *_args):
        """Silence stdlib access logging — launchd captures stderr already."""
        return


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the ``tmuxctld`` CLI parser (``--host`` / ``--port``)."""
    parser = argparse.ArgumentParser(
        prog="tmuxctld",
        description="Standalone HTTP-loopback daemon face of tmuxctl (stdlib only).",
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help="loopback bind host (default 127.0.0.1)"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port (default 7778)")
    return parser


def serve(host: str, port: int) -> int:
    """Run the daemon on ``host:port`` until SIGTERM/Ctrl-C; returns the exit code.

    Installs a SIGTERM handler that raises ``SystemExit`` for a clean
    ``server_close()`` and blocks in ``serve_forever()`` (which sets the server
    ready event once listening).

    The daemon performs powerful, UNAUTHENTICATED tmux operations, so it must
    only ever bind loopback. A non-loopback ``--host`` is refused (fail-closed)
    rather than silently exposing the control plane on a routable interface.
    """
    if host not in _LOOPBACK_HOSTS:
        print(
            f"tmuxctld refuses to bind non-loopback host {host!r} "
            f"(allowed: {', '.join(sorted(_LOOPBACK_HOSTS))})",
            file=sys.stderr,
            flush=True,
        )
        return 2
    server = TmuxctldServer(
        (host, port),
        version=read_version(),
        sha=read_sha(),
        advertised_port=port,
    )

    def _shutdown(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    print(f"tmuxctld listening on http://{host}:{port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse argv and run :func:`serve`; the module's process entry point."""
    args = build_parser().parse_args(argv)
    return serve(str(args.host), int(args.port))


if __name__ == "__main__":
    raise SystemExit(main())
