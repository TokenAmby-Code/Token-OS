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
import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import send_gate, typing_guard_state
from .api import RegistryError
from .send_gate import thread_local_override
from .service import TmuxControlPlane
from .tmux_adapter import (
    TmuxAdapter,
    TmuxError,
    TmuxSendGated,
    normalize_prompt_payload,
    prompt_payload_hash,
)

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
_VOICE_LIST_SEP = "__TMUXCTLD_VOICE_FIELD__"
_VOICE_LOCK_OPTION = "@DISCORD_VOICE_LOCK"
_VOICE_PROCESSING_OPTION = "@DISCORD_VOICE_PROCESSING"


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


def _normalize_bot_name(bot_name: str | None) -> str:
    return (bot_name or "unknown").strip().lower().replace("-", "_")


def _voice_static_target(bot_name: str) -> str | None:
    bot = _normalize_bot_name(bot_name)
    if bot == "custodes":
        return "council:custodes"
    if bot in {"mechanicus", "fabricator_general", "fabricator-general", "fg"}:
        return "mechanicus:fabricator-general"
    return None


@dataclass
class VoiceSession:
    """In-memory Discord voice draft state.

    External callers only know ``voice_session_id`` and the public target role.
    The raw tmux ``%`` id is intentionally not stored here; it can appear only
    inside ``TmuxAdapter`` while executing a request.
    """

    voice_session_id: str
    bot_name: str
    user_id: str
    channel_id: str
    route_epoch: str
    target_role: str
    created_at: float = field(default_factory=time.time)
    utterances: int = 0


class VoiceSessionStore:
    """Thread-safe process-local Discord voice draft registry."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, VoiceSession] = {}

    def put(self, session: VoiceSession) -> None:
        with self._lock:
            # One active voice draft per bot/user. Starting a new one replaces
            # old in-memory state; the handler clears old pane options first.
            for sid, existing in list(self._sessions.items()):
                if existing.bot_name == session.bot_name and existing.user_id == session.user_id:
                    del self._sessions[sid]
            self._sessions[session.voice_session_id] = session

    def get(self, voice_session_id: str) -> VoiceSession | None:
        with self._lock:
            return self._sessions.get(voice_session_id)

    def update(self, session: VoiceSession) -> None:
        with self._lock:
            if session.voice_session_id not in self._sessions:
                raise KeyError("voice session not found")
            self._sessions[session.voice_session_id] = session

    def matching(
        self,
        *,
        voice_session_id: str = "",
        bot_name: str = "",
        user_id: str = "",
    ) -> list[VoiceSession]:
        bot = _normalize_bot_name(bot_name) if bot_name else ""
        uid = str(user_id) if user_id else ""
        with self._lock:
            if voice_session_id:
                item = self._sessions.get(voice_session_id)
                return [item] if item else []
            return [
                item
                for item in self._sessions.values()
                if (not bot or item.bot_name == bot) and (not uid or item.user_id == uid)
            ]

    def remove(self, voice_session_id: str) -> VoiceSession | None:
        with self._lock:
            return self._sessions.pop(voice_session_id, None)

    def list(self) -> list[VoiceSession]:
        with self._lock:
            return list(self._sessions.values())


VOICE_SESSIONS = VoiceSessionStore()


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


def _refuse_send_into_human_lock(control, pane: str) -> str:
    """Resolve ``pane`` and fail closed on a live HUMAN keystroke/pending lock.

    The send gate honors a process-global ``TMUX_SEND_GATE_ALLOW`` sanctioned
    override and yields it back to a human lock for ONLY the daemon's own two
    thread-local holder reasons (``tmuxctld-send-holder`` /
    ``tmuxctl-submit-transaction``). Every OTHER override reason — including the
    process-global env override an enforce-action sets (e.g. token-api's
    ``custodes_enforcement_deferred_timeout`` deferred-timeout Custodes nag) —
    sails past the typing guard and pierces the Emperor's live keystroke lock at
    this send-path chokepoint.

    The daemon treats a human ON/PENDING lock as inviolable: no ambient override
    may clobber active typing. This check reads ONLY the keystroke/pending hold
    (``send_gate._pane_human_locked`` excludes the daemon's own green AGENT hold),
    so the daemon's legitimate self-pierce of its AGENT marker is unaffected and
    an OFF pane still sends. Raises :class:`TmuxSendGated` (zero bytes written,
    re-queueable) when locked; returns the resolved physical pane id otherwise.

    The human lock is keyed on the PHYSICAL ``%NN`` (the tmux any-key binding
    stamps ``@TYPING_LOCK_UNTIL`` per physical pane). A canonical caller id
    (``council:custodes``, ``mechanicus:N``, …) must therefore be resolved before
    the lock read, or the read keys off a non-physical target tmux does not
    understand, the lock reads as unset, and the send pierces. So resolution is
    split: a missing resolver (``AttributeError`` — a fail-open test/shim adapter)
    falls back to the caller id (a raw ``%NN`` is already physical), but a GENUINE
    resolution failure fails closed — we will not gamble a pierce on an unresolved
    canonical id.
    """
    try:
        phys = control.adapter._resolve_pane_target_arg(pane)
    except AttributeError:
        # Adapter has no resolver (fail-open shim / test double); the caller id is
        # used as-is. A real daemon's TmuxAdapter always provides the resolver, and
        # a raw %NN is already physical, so this branch never masks a canonical id.
        phys = pane
    except Exception as exc:
        # Resolution genuinely failed: we cannot key the lock read on the physical
        # %NN, and falling back to the canonical id would silently miss the lock and
        # pierce. Fail closed (zero bytes, re-queueable) rather than risk clobbering
        # active typing. Log the raw cause SERVER-SIDE only; the gate payload rides
        # back to the caller, so it must not leak resolver internals — gate
        # ``pane_unresolved`` is enough to distinguish the fail-closed case.
        log.warning("tmuxctld: pane resolution failed for %s; failing closed: %s", pane, exc)
        raise TmuxSendGated(
            {
                "suppressed": True,
                "reason": "typing_guard",
                "gate": "pane_unresolved",
                "policy": "cancel",
                "target": pane,
                "deferred": True,
            }
        ) from exc
    if send_gate._pane_human_locked(phys):
        raise TmuxSendGated(
            {
                "suppressed": True,
                "reason": "typing_guard",
                "gate": "human_lock",
                "policy": "cancel",
                "target": phys,
                "deferred": True,
            }
        )
    return phys


def _h_send_keys(control, params):
    pane = _s(params, "pane")
    command = _s(params, "command")
    from .occupancy import assert_dispatch_target_available, looks_like_dispatch_launcher_payload

    # Inviolable human-lock fail-closed before any byte-bearing send: an ambient
    # TMUX_SEND_GATE_ALLOW override (enforce-action / quiet-hours pierce) must
    # never clobber active typing at this chokepoint.
    _refuse_send_into_human_lock(control, pane)
    if looks_like_dispatch_launcher_payload(command):
        assert_dispatch_target_available(control.adapter, pane)
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


# The captured composer slice we fingerprint for the white-whale "submit
# swallowed as a prompt newline" failure. A short head of the payload is enough
# to confirm the bytes landed in the composer.
_SWALLOW_NEEDLE_LEN = 48


def _detect_swallowed_submit(capture: str, payload: str) -> bool:
    """Heuristic: did the TUI swallow the Enter into the prompt body?

    The white-whale failure (live Codex/Claude repro) is: the literal payload
    bytes land in the composer, but the C-m that should submit is ingested as a
    newline *inside* the draft instead. The capture signature is therefore (a) a
    representative head of the submitted payload still sitting in the composer
    AND (b) the captured composer region ending in a trailing newline (the
    swallowed Enter left the cursor on a fresh line rather than submitting).

    A clean submit leaves an empty composer (needle absent) — returns False, so
    the recovery C-m is fired only when there is real evidence of a stuck draft.
    Tuning the exact composer fingerprint against each TUI is a follow-on; the
    behavioral contract this guards is: detect → still recover → surface loudly.
    """
    if not capture or not payload:
        return False
    needle = normalize_prompt_payload(payload).strip()[:_SWALLOW_NEEDLE_LEN]
    if not needle or needle not in capture:
        return False
    return capture.endswith("\n")


def _notify_swallowed_submit(*, pane_public: str, instance_id: str, payload_hash: str) -> None:
    """Surface a swallowed-submit recovery on the human notify path (best-effort).

    The loud surfacing of record is the ``logger.warning`` at the call site; this
    additionally routes a notice to token-api's ``/api/notify`` (TTS/Discord
    router) so the recovery is reported, not silently eaten (per the
    no-error-suppressing-debounce / surface-don't-suppress discipline). Failure
    to notify must never break the send path, so all errors are swallowed here
    AFTER the warning has already been logged.
    """
    base = os.environ.get("TOKEN_API_URL", "http://localhost:7777").rstrip("/")
    target = pane_public if pane_public and pane_public != "unresolved" else "a pane"
    message = (
        f"tmuxctld recovered a swallowed submit on {target}"
        f"{f' (instance {instance_id})' if instance_id else ''} — "
        "Enter was ingested as a prompt newline; recovery C-m fired."
    )
    body = json.dumps(
        {
            "message": message,
            "vibe": "alert",
            "instance_id": instance_id or None,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/api/notify",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=2.0):
            pass
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning(
            "tmuxctld: swallowed-submit notify failed hash=%s (surfaced via log only): %s",
            payload_hash,
            exc,
        )


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

    # Resolve the physical pane id once for all guard + capture ops. tmux pane
    # options (@TYPING_*_UNTIL) and capture-pane key off the real %NN; the
    # caller-supplied id may be a canonical page:id. _resolve is a no-op on a raw
    # %NN, so it is safe either way and tolerant of resolver failure. The same
    # resolution feeds the inviolable human-lock fail-closed: a live keystroke /
    # pending lock gates the send NOW (zero bytes), immune to any ambient
    # TMUX_SEND_GATE_ALLOW enforce-action override, and before we acquire our own
    # AGENT hold over a pane the Emperor is typing into.
    phys_pane = _refuse_send_into_human_lock(control, pane)

    # Fail closed before ANY byte-bearing send, including insert-only calls. If a
    # human/pending/other agent guard is already live, do not enter send_gate's
    # default delay path: that holds the HTTP request until caller timeouts and
    # can later release a stale send onto active typing. Surface a structured
    # gated result instead; Token-API can queue/retry, but tmuxctld issues zero
    # bytes now.
    normalized_payload = normalize_prompt_payload(text)
    from .occupancy import assert_dispatch_target_available, looks_like_dispatch_launcher_payload

    if looks_like_dispatch_launcher_payload(normalized_payload):
        assert_dispatch_target_available(control.adapter, phys_pane)
    pre_gate = send_gate.evaluate(("send-keys", "-t", phys_pane, "-l", normalized_payload))
    if pre_gate is not None and pre_gate.get("suppressed"):
        raise TmuxSendGated({**pre_gate, "policy": "cancel", "deferred": True})

    if not submit:
        return control.send_text(pane, text, clear_prompt=clear_prompt, submit=False)

    # Hash the NORMALIZED payload that is actually injected (newlines collapsed,
    # rstripped) — not the raw text. The UserPromptSubmit ack hashes the prompt
    # the agent received (post-normalization; cf. agent-cmd's payload_hash), so
    # hashing raw multiline text here would never match and force a false
    # `unverified` + needless recovery.
    payload_hash = prompt_payload_hash(normalized_payload)
    dispatch_id = str(uuid.uuid4())
    instance_id = ""
    try:
        instance_id = str(control.instance_id_for_pane(pane).get("instance_id") or "").strip()
    except Exception:
        instance_id = ""

    # Hold the typing guard (green ⌨ AGENT state) for the handshake window so
    # concurrent sends to this pane delay behind it (state-blind, the existing
    # gate path). Budget the hold to outlast the worst-case send+verify+retry.
    hold_seconds = max(
        8,
        int(verify_timeout * (max(0, ack_submit_retries) + 1) + submit_settle_seconds * 4) + 2,
    )
    held = False
    try:
        held = bool(
            typing_guard_state.hold(
                typing_guard_state.Tmux(typing_guard_state.tmux_binary()),
                phys_pane,
                seconds=hold_seconds,
                now=typing_guard_state.now_epoch(),
            )
        )
    except Exception as exc:  # tmux unreachable / no live server (e.g. unit tests)
        log.debug("tmuxctld: agent-guard hold skipped pane=%s: %s", phys_pane, exc)
        held = False

    # When we hold, pierce our OWN agent lock so the send we just guarded is not
    # delayed by the green state we set — thread-locally, so a concurrent send to
    # another pane on another worker thread is never granted the pierce (and so
    # never stomps a human lock there). When the hold was DENIED (a live human
    # on/pending lock), we do NOT pierce: the send routes through the normal gate
    # and delays behind the human, never stomping the Emperor's keystrokes.
    override_ctx = (
        thread_local_override("tmuxctld-send-holder") if held else contextlib.nullcontext()
    )

    def _send_submit_key() -> None:
        if hasattr(control.adapter, "send_keys"):
            control.adapter.send_keys(pane, "C-m")
        else:
            control.adapter.run("send-keys", "-t", pane, "C-m")

    started = time.monotonic()
    ack = None
    swallowed_submit_detected = False
    recovery_attempts = 0
    failures: list[dict] = []
    try:
        with override_ctx:
            if hasattr(control.adapter, "send_text_then_submit"):
                control.adapter.send_text_then_submit(
                    pane,
                    text,
                    clear_prompt=clear_prompt,
                    pre_submit_keys=pre_submit_keys,
                    submit_settle_seconds=submit_settle_seconds,
                )
            else:
                normalized = normalized_payload
                if clear_prompt:
                    control.adapter.send_keys(pane, "C-u")
                control.adapter.run("send-keys", "-t", pane, "-l", normalized)
                gate = getattr(control.adapter, "last_send_gate_result", None)
                if gate and gate.get("suppressed"):
                    raise TmuxSendGated(gate)
                # Test-adapter fallback. Real daemon sends use TmuxAdapter's
                # canonical method above; callers must not assemble send-keys
                # outside tmuxctld.
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
                    # Handshake recovery: the prior submit was not acknowledged.
                    # Pane-sniff FIRST — if the composer still holds the draft
                    # with a swallowed Enter (trailing newline), that is the
                    # white-whale failure: surface it loudly instead of silently
                    # eating it, but STILL fire the recovery C-m to sink the
                    # stuck draft. The daemon owns this retry; callers do not pile
                    # on their own raw send-keys.
                    capture = ""
                    try:
                        capture = control.adapter.capture_pane(phys_pane, lines=12)
                    except Exception as exc:
                        log.debug("tmuxctld: capture-pane failed pane=%s: %s", phys_pane, exc)
                    if _detect_swallowed_submit(capture, text):
                        swallowed_submit_detected = True
                        pane_public = _safe_public_role(pane)
                        log.warning(
                            "tmuxctld send: SWALLOWED SUBMIT pane=%s instance=%s hash=%s — "
                            "draft present in composer with trailing newline; firing recovery "
                            "C-m and surfacing (not eaten)",
                            pane_public,
                            instance_id,
                            payload_hash,
                        )
                        failures.append(
                            {
                                "type": "swallowed_submit",
                                "attempt": attempt + 1,
                                "detail": "Enter ingested as prompt newline; recovery C-m fired",
                            }
                        )
                        _notify_swallowed_submit(
                            pane_public=pane_public,
                            instance_id=instance_id,
                            payload_hash=payload_hash,
                        )
                    _send_submit_key()
                    recovery_attempts += 1
                    if submit_settle_seconds > 0:
                        time.sleep(submit_settle_seconds)
    finally:
        # The guard must never leak green past the handshake. Release only what
        # we acquired (release clears @TYPING_AGENT_UNTIL only and re-projects a
        # human lock that may have arrived mid-hold via expire_pane semantics).
        if held:
            try:
                typing_guard_state.release(
                    typing_guard_state.Tmux(typing_guard_state.tmux_binary()),
                    phys_pane,
                    now=typing_guard_state.now_epoch(),
                )
            except Exception as exc:
                log.warning("tmuxctld: agent-guard release failed pane=%s: %s", phys_pane, exc)

    verification_status = "submitted" if ack else ("unverified" if verify else "not_requested")
    # When verification was never requested, a completed send is "submitted", not
    # "unverified" — only a requested-but-unacked send is genuinely unverified.
    status = "submitted" if ack or not verify else "unverified"
    return {
        "status": status,
        "pane": pane,
        "instance_id": instance_id,
        "dispatch_id": dispatch_id,
        "payload_hash": payload_hash,
        "verification_status": verification_status,
        "verified_by": "UserPromptSubmit" if ack else None,
        "ack": ack,
        "guard_held": held,
        "swallowed_submit_detected": swallowed_submit_detected,
        "recovery_attempts": recovery_attempts,
        "failures": failures,
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


def _h_insert_invocation(control, params):
    """Engine-agnostic, kind-aware skill/command insert at a pane's prompt start.

    The warm-daemon replacement for the Shift+Tab menu's 3 cold ``tmuxctl`` spawns
    (resolve/prompt-start, insert-text, prompt-end). One loopback round-trip does
    the whole prompt-start -> insert -> Codex-sink -> prompt-end, with the leader
    policy (``$skill`` vs ``/skill`` vs universal ``/command``) resolved here.
    """
    pane = _s(params, "pane")
    name = _s(params, "name") or _s(params, "skill")
    agent = _s(params, "agent", "auto")
    kind = _s(params, "kind", "skill")
    arguments = _s(params, "arguments") or None
    result = control.insert_invocation(pane, name, agent=agent, kind=kind, arguments=arguments)
    return {"status": "inserted", **result}


def _h_assert_instance(control, params):
    return control.assert_instance(_s(params, "pane"))


def _h_reconcile(control, params):
    # The detached daemon has no ambient tmux session; the fleet lives in `main`.
    return {"results": control.reconcile_personas(session=_s(params, "session", "main"))}


def _h_event(control, params):
    return control.handle_event(
        _s(params, "event"), pane=_s(params, "pane"), session=_s(params, "session", "main")
    )


def _h_persona_engine(control, params):
    return control.rotate_persona_engine(
        _s(params, "pane"),
        engine=(_s(params, "engine") or None),
        toggle=_b(params, "toggle"),
        session=(_s(params, "session") or None),
    )


def _h_hook_user_prompt_submit(_control, params):
    return _PROMPT_SUBMIT_SNIFFER.record(params)


def _h_clear_runtime(control, params):
    return control.clear_runtime(_s(params, "pane"))


_WRAPPEREND_LIST_SEP = "__TMUXCTLD_WRAPPEREND_FIELD__"


def _adapter_show_pane_option(control: TmuxControlPlane, pane: str, option: str) -> str:
    if hasattr(control.adapter, "show_pane_option"):
        return str(control.adapter.show_pane_option(pane, option) or "").strip()
    return str(
        control.adapter.run("show-options", "-pv", "-t", pane, option, allow_failure=True) or ""
    ).strip()


def _pane_exists_for_wrapperend(control: TmuxControlPlane, pane: str) -> bool:
    if not pane:
        return False
    return bool(
        control.adapter.run(
            "display-message", "-t", pane, "-p", "#{pane_id}", allow_failure=True
        ).strip()
    )


def _find_pane_by_wrapper_id(control: TmuxControlPlane, wrapper_launch_id: str) -> str:
    if not wrapper_launch_id:
        return ""
    raw = control.adapter.run(
        "list-panes",
        "-a",
        "-F",
        _WRAPPEREND_LIST_SEP.join(["#{pane_id}", "#{@TOKEN_API_WRAPPER_LAUNCH_ID}"]),
        allow_failure=True,
    )
    for line in raw.splitlines():
        if not line:
            continue
        pane_id, owner = (line.split(_WRAPPEREND_LIST_SEP, 1) + [""])[:2]
        if owner.strip() == wrapper_launch_id and pane_id.strip():
            return pane_id.strip()
    return ""


def _h_hook_wrapperend(control, params):
    """Authoritative wrapper-owned visual/runtime cleanup for tmux panes.

    Token-API owns process/session lifecycle. tmuxctld owns pane-local visual
    state, so WrapperEnd clears only the pane whose @TOKEN_API_WRAPPER_LAUNCH_ID
    matches the exiting wrapper. Missing/already-cleared panes are successful
    no-ops; a pane owned by a different wrapper is surfaced as an error.
    """
    env = params.get("env") if isinstance(params.get("env"), dict) else {}
    wrapper_launch_id = _s(params, "wrapper_launch_id") or _s(env, "TOKEN_API_WRAPPER_LAUNCH_ID")
    pane = _s(params, "tmux_pane") or _s(env, "TMUX_PANE")
    if not wrapper_launch_id:
        log.error("tmuxctld wrapperend missing wrapper_launch_id pane=%s", pane)
        raise ValueError("wrapper_launch_id required")

    if pane and not _pane_exists_for_wrapperend(control, pane):
        pane = ""
    if not pane:
        pane = _find_pane_by_wrapper_id(control, wrapper_launch_id)
    if not pane:
        return {
            "status": "already_missing",
            "wrapper_launch_id": wrapper_launch_id,
            "pane": "",
        }

    owner = _adapter_show_pane_option(control, pane, "@TOKEN_API_WRAPPER_LAUNCH_ID")
    if owner and owner != wrapper_launch_id:
        log.error(
            "tmuxctld wrapperend ownership mismatch pane=%s payload_wrapper=%s pane_wrapper=%s",
            pane,
            wrapper_launch_id,
            owner,
        )
        raise ValueError("wrapperend pane is owned by another wrapper")
    if not owner:
        return {
            "status": "already_cleared",
            "wrapper_launch_id": wrapper_launch_id,
            "pane": pane,
        }

    result = control.clear_runtime(pane)
    return {
        "status": "cleared",
        "wrapper_launch_id": wrapper_launch_id,
        "pane": result.get("pane", pane) if isinstance(result, dict) else pane,
    }


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


def _voice_session_payload(session: VoiceSession) -> dict:
    return {
        "voice_session_id": session.voice_session_id,
        "bot_name": session.bot_name,
        "user_id": session.user_id,
        "channel_id": session.channel_id,
        "route_epoch": session.route_epoch,
        "target_role": session.target_role,
        "created_at": session.created_at,
        "utterances": session.utterances,
    }


def _voice_public_target_for_client(control: TmuxControlPlane, client_name: str) -> str:
    raw = control.adapter.run(
        "display-message",
        "-c",
        client_name,
        "-p",
        _VOICE_LIST_SEP.join(
            [
                "#{pane_id}",
                "#{session_name}",
                "#{pane_current_command}",
                "#{pane_current_path}",
                "#{@PANE_ID}",
            ]
        ),
        allow_failure=True,
    ).strip()
    parts = raw.split(_VOICE_LIST_SEP)
    if len(parts) != 5:
        return ""
    pane_id, session_name, _command, current_path, pane_role = parts
    if not pane_id.startswith("%"):
        return ""
    if session_name == "discord-daemon" or session_name.startswith("tx_test_"):
        return ""
    if current_path.endswith("/runtimes/token-os/live/discord-daemon"):
        return ""
    role = _safe_public_role(pane_role)
    if role == "unresolved":
        return ""
    # Force a full public resolution through tmuxctld. This proves the public
    # role is routable now, but returns only the semantic role.
    try:
        resolved = _safe_public_role(control.public_pane_id(role))
    except Exception:
        return ""
    return "" if resolved == "unresolved" else resolved


def _voice_resolve_imperial_guard_target(control: TmuxControlPlane) -> str:
    raw = control.adapter.run(
        "list-clients",
        "-F",
        _VOICE_LIST_SEP.join(["#{client_activity}", "#{client_name}", "#{session_name}"]),
        allow_failure=True,
    )
    clients: list[tuple[int, str]] = []
    for line in raw.splitlines():
        if not line:
            continue
        parts = line.split(_VOICE_LIST_SEP)
        if len(parts) != 3:
            continue
        raw_activity, client_name, session_name = parts
        if not client_name or session_name == "discord-daemon":
            continue
        try:
            activity = int(raw_activity or "0")
        except ValueError:
            continue
        clients.append((activity, client_name))
    clients.sort(reverse=True)
    for _activity, client_name in clients:
        role = _voice_public_target_for_client(control, client_name)
        if role:
            return role
    raise ValueError("no routable attached operator client target")


def _voice_resolve_target(control: TmuxControlPlane, bot_name: str) -> str:
    bot = _normalize_bot_name(bot_name)
    if bot == "imperial_guard":
        return _voice_resolve_imperial_guard_target(control)
    target = _voice_static_target(bot)
    if not target:
        raise ValueError(f"no voice target policy for bot {bot!r}")
    role = _safe_public_role(control.public_pane_id(target))
    if role == "unresolved":
        raise ValueError(f"voice target is not routable: {target}")
    return role


def _voice_set_option_best_effort(
    control: TmuxControlPlane, target_role: str, option: str, value: str
) -> None:
    try:
        control.adapter.run(
            "set-option", "-p", "-t", target_role, option, value, allow_failure=True
        )
    except Exception:
        pass


def _voice_clear_options(control: TmuxControlPlane, target_role: str) -> None:
    _voice_set_option_best_effort(control, target_role, _VOICE_PROCESSING_OPTION, "0")
    _voice_set_option_best_effort(control, target_role, _VOICE_LOCK_OPTION, "0")


def _voice_clear_sessions(
    control: TmuxControlPlane,
    *,
    voice_session_id: str = "",
    bot_name: str = "",
    user_id: str = "",
) -> list[dict]:
    cleared: list[dict] = []
    for session in VOICE_SESSIONS.matching(
        voice_session_id=voice_session_id, bot_name=bot_name, user_id=user_id
    ):
        removed = VOICE_SESSIONS.remove(session.voice_session_id)
        if not removed:
            continue
        _voice_clear_options(control, removed.target_role)
        cleared.append(_voice_session_payload(removed))
    return cleared


def _h_voice_start(control, params):
    bot_name = _normalize_bot_name(_s(params, "bot_name", "voice"))
    user_id = _s(params, "user_id")
    if not user_id:
        raise ValueError("user_id required")
    channel_id = _s(params, "channel_id")
    route_epoch = _s(params, "route_epoch")
    # Replace any existing draft for this bot/user and release its lock before
    # acquiring the new route.
    _voice_clear_sessions(control, bot_name=bot_name, user_id=user_id)
    target_role = _voice_resolve_target(control, bot_name)
    session = VoiceSession(
        voice_session_id=uuid.uuid4().hex,
        bot_name=bot_name,
        user_id=user_id,
        channel_id=channel_id,
        route_epoch=route_epoch,
        target_role=target_role,
    )
    VOICE_SESSIONS.put(session)
    _voice_set_option_best_effort(control, target_role, _VOICE_LOCK_OPTION, "1")
    _voice_set_option_best_effort(control, target_role, _VOICE_PROCESSING_OPTION, "0")
    return {
        "voice_session_id": session.voice_session_id,
        "target_role": session.target_role,
    }


def _require_voice_session(params) -> VoiceSession:
    voice_session_id = _s(params, "voice_session_id")
    if not voice_session_id:
        raise ValueError("voice_session_id required")
    session = VOICE_SESSIONS.get(voice_session_id)
    if not session:
        raise KeyError("voice session not found")
    return session


def _h_voice_append(control, params):
    session = _require_voice_session(params)
    text = _s(params, "text").strip()
    if not text:
        return {"inserted": False, "reason": "empty", "target_role": session.target_role}
    segment = f" {text}" if session.utterances else text
    try:
        _voice_set_option_best_effort(control, session.target_role, _VOICE_PROCESSING_OPTION, "1")
        control.send_text(session.target_role, segment, submit=False)
    finally:
        _voice_set_option_best_effort(control, session.target_role, _VOICE_PROCESSING_OPTION, "0")
    session.utterances += 1
    VOICE_SESSIONS.update(session)
    return {"inserted": True, "target_role": session.target_role, "utterances": session.utterances}


def _h_voice_ship(control, params):
    session = _require_voice_session(params)
    text = _s(params, "text").strip()
    if text:
        _h_voice_append(control, {"voice_session_id": session.voice_session_id, "text": text})
        session = _require_voice_session({"voice_session_id": session.voice_session_id})
    try:
        _voice_set_option_best_effort(control, session.target_role, _VOICE_PROCESSING_OPTION, "1")
        control.adapter.send_keys(session.target_role, "Enter")
    finally:
        _voice_set_option_best_effort(control, session.target_role, _VOICE_PROCESSING_OPTION, "0")
        removed = VOICE_SESSIONS.remove(session.voice_session_id)
        if removed:
            _voice_clear_options(control, removed.target_role)
    return {"shipped": True, "target_role": session.target_role}


def _h_voice_scratch(control, params):
    session = _require_voice_session(params)
    try:
        control.adapter.send_keys(session.target_role, "C-c")
    finally:
        removed = VOICE_SESSIONS.remove(session.voice_session_id)
        if removed:
            _voice_clear_options(control, removed.target_role)
    return {"scratched": True, "target_role": session.target_role}


def _h_voice_clear(control, params):
    bot_name = _s(params, "bot_name")
    cleared = _voice_clear_sessions(
        control,
        voice_session_id=_s(params, "voice_session_id"),
        bot_name=bot_name,
        user_id=_s(params, "user_id"),
    )
    cleared_options = False
    # Startup/leave cleanup must also clear stale pane status left by a prior
    # Discord/tmuxctld process. If there is no in-memory session but a bot owner
    # was supplied, resolve that bot's semantic target now and clear the public
    # role's voice options. No DB, no physical id, no fallback target.
    if not cleared and bot_name:
        try:
            target_role = _voice_resolve_target(control, bot_name)
            _voice_clear_options(control, target_role)
            cleared_options = True
        except Exception:
            cleared_options = False
    return {"cleared": len(cleared), "sessions": cleared, "cleared_options": cleared_options}


def _h_voice_status(control, params):
    return {
        "sessions": [
            _voice_session_payload(session)
            for session in VOICE_SESSIONS.matching(
                voice_session_id=_s(params, "voice_session_id"),
                bot_name=_s(params, "bot_name"),
                user_id=_s(params, "user_id"),
            )
        ]
    }


def _h_voice_target(control, params):
    bot_name = _normalize_bot_name(_s(params, "bot_name", "voice"))
    target_role = _voice_resolve_target(control, bot_name)
    return {"bot_name": bot_name, "target_role": target_role}


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
    ("POST", "/insert-invocation"): _h_insert_invocation,
    ("POST", "/assert-instance"): _h_assert_instance,
    ("POST", "/hooks/user-prompt-submit"): _h_hook_user_prompt_submit,
    ("POST", "/hooks/wrapperend"): _h_hook_wrapperend,
    # Event-driven persona reconcile (replaces the retired 2-min assert-personas
    # poll). /reconcile re-seats all must-fill seats; /event ingests a single tmux
    # lifecycle event (a persona pane-died self-heal). Nothing polls these.
    ("POST", "/reconcile"): _h_reconcile,
    ("POST", "/event"): _h_event,
    ("POST", "/persona-engine"): _h_persona_engine,
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
    # Discord voice session API (semantic boundary; no raw physical panes)
    ("POST", "/voice/session/start"): _h_voice_start,
    ("POST", "/voice/session/append"): _h_voice_append,
    ("POST", "/voice/session/ship"): _h_voice_ship,
    ("POST", "/voice/session/scratch"): _h_voice_scratch,
    ("POST", "/voice/session/clear"): _h_voice_clear,
    ("GET", "/voice/status"): _h_voice_status,
    ("GET", "/voice/target"): _h_voice_target,
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
