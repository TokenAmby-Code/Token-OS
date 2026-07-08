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
import errno
import hashlib
import io
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
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
from .resolver import resolve_to_physical
from .send_gate import thread_local_override
from .service import TmuxControlPlane
from .skill_invoke import (
    ethereal_invocation_text,
    invocation_sink_keys,
    invocation_text,
    normalize_invocation_kind,
    resolve_agent_for_pane,
)
from .tmux_adapter import (
    TmuxAdapter,
    TmuxError,
    TmuxSendGated,
    normalize_prompt_payload,
    prompt_payload_hash,
    tmux_binary,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7778
CALLBACKS_VERSION = 1
DEFAULT_CALLBACKS_PATH = Path("~/.claude/tmuxctld-callbacks.json").expanduser()
DEFAULT_CALLBACK_TTL_SECONDS = 30 * 60
DEFERRED_SENDS_VERSION = 1
DEFAULT_DEFERRED_SENDS_PATH = Path("~/.claude/tmuxctld-deferred-sends.json").expanduser()

# tmux owns pane-death detection. tmuxctld owns persona re-seating. The global
# hook is therefore daemon-critical: if tmux.conf was not sourced (or hooks were
# cleared during a live reload), must-fill persona panes can remain forever at
# "Pane is dead" now that the old correctness poll is retired.
_PANE_DIED_HOOK = (
    'run-shell -b "tmuxctld-ping POST /event event=pane-died pane=#{pane_id} '
    ">/dev/null || env IMPERIUM_TMUX_RAW=1 tmux display-message "
    'tmuxctld-ping-/event-failed"'
)

# The global pane-died hook can vanish under the daemon: a live ``tmux source-file``
# or a hook-clear during a workspace reload drops it, and then a dying must-fill
# seat or one-off slot strands at "Pane is dead" with nothing to reassert it (the
# witnessed regression). The hook is therefore re-asserted PERIODICALLY, not only
# at daemon boot. The re-assertion rides the /health heartbeat the watchdog already
# polls (no new poller — the daemon's reconcile-poll ban stands) and is throttled
# to this interval so a healthy daemon re-installs the idempotent hook cheaply and
# self-heals within one interval of any clear.
_HOOK_REASSERT_INTERVAL_SECONDS = 60.0

# When a re-assertion FAILS (``set-hook`` timed out against a wedged/slow tmux —
# the 2026-07-07 outage condition), holding the full interval strands the global
# pane-died hook uninstalled for a whole minute: nothing self-heals in that window
# (twice-bitten). A failed attempt therefore pulls the throttle deadline in to this
# short retry window so the very next /health heartbeat retries and re-installs the
# instant tmux recovers. The retry cadence is still bounded by the heartbeat itself
# (no new poller) and each attempt is one 5s-capped ``set-hook`` — matching the
# install timeout, so this never busy-loops faster than the daemon can act.
_HOOK_REASSERT_RETRY_SECONDS = 5.0

# The permanent typing-guard PENDING-branch root keys (Enter/C-m/BSpace/C-h/C-c) are
# bound once at tmux-server start and — unlike ``Any`` — have no focus/rehydrate
# re-source path, so a deploy that advances the daemon SHA leaves the LIVE key-table
# on the OLD (flashing) form until an explicit ``source-file``.  This is the CD
# deploy-coherence gap.  ``typing_guard_state.reconcile_pending_bindings`` closes it
# by re-sourcing only those keys when the live form drifts; it rides the /health
# heartbeat on this cadence (idempotent, throttled, non-fatal — same doctrine as the
# pane-died hook re-assertion) so a running server self-heals within one interval of
# any deploy WITHOUT a new poller or a destructive restart/kickstart.
_BINDING_RECONCILE_INTERVAL_SECONDS = 60.0

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

_CLIENT_DISCONNECT_ERRNOS = frozenset(
    errno_value
    for errno_value in (
        getattr(errno, "ECONNRESET", None),
        getattr(errno, "EPIPE", None),
        getattr(errno, "ECONNABORTED", None),
        getattr(errno, "ESHUTDOWN", None),
    )
    if errno_value is not None
)


def _operation_degraded_seconds_from_env() -> float:
    raw = os.environ.get("TMUXCTLD_DEGRADED_OPERATION_SECONDS")
    try:
        value = float(raw) if raw is not None else 30.0
    except (TypeError, ValueError):
        value = 30.0
    return max(0.001, value)


@dataclass(frozen=True)
class _ActiveOperation:
    op_id: str
    method: str
    path: str
    started: float
    thread_name: str


class OperationMonitor:
    """In-process request monitor for detecting wedged non-health operation paths.

    The daemon is intentionally stateless for tmux work: every request gets a
    fresh control plane/adapter. If operations progressively ossify while
    ``/health`` still answers, the watchdog needs a cheap heartbeat-visible
    signal that a data route is stuck. This monitor stores only bounded timing
    metadata and removes active records in a ``finally`` block, so completed
    requests leave no residue.
    """

    def __init__(self, *, max_recent_slow: int = 20) -> None:
        self._lock = threading.RLock()
        self._active: dict[str, _ActiveOperation] = {}
        self._recent_slow: deque[dict] = deque(maxlen=max_recent_slow)
        self._completed = 0
        self._failed = 0

    def begin(self, method: str, path: str) -> str:
        op_id = str(uuid.uuid4())
        record = _ActiveOperation(
            op_id=op_id,
            method=method.upper(),
            path=path,
            started=time.monotonic(),
            thread_name=threading.current_thread().name,
        )
        with self._lock:
            self._active[op_id] = record
        return op_id

    def finish(self, op_id: str, *, ok: bool) -> None:
        now = time.monotonic()
        threshold = _operation_degraded_seconds_from_env()
        with self._lock:
            record = self._active.pop(op_id, None)
            if record is None:
                return
            self._completed += 1
            if not ok:
                self._failed += 1
            duration = max(0.0, now - record.started)
            if duration >= threshold:
                self._recent_slow.append(
                    {
                        "method": record.method,
                        "path": record.path,
                        "duration_seconds": round(duration, 3),
                        "ok": bool(ok),
                        "finished_epoch": time.time(),
                    }
                )

    def snapshot(self) -> dict:
        now = time.monotonic()
        threshold = _operation_degraded_seconds_from_env()
        with self._lock:
            active = list(self._active.values())
            recent_slow = list(self._recent_slow)
            completed = self._completed
            failed = self._failed
        active_payload = [
            {
                "method": op.method,
                "path": op.path,
                "age_seconds": round(max(0.0, now - op.started), 3),
                "thread": op.thread_name,
            }
            for op in active
        ]
        stuck = [op for op in active_payload if op["age_seconds"] >= threshold]
        return {
            "operation_degraded": bool(stuck),
            "operation_slow_threshold_seconds": threshold,
            "active_operations": len(active_payload),
            "stuck_operations": stuck,
            "recent_slow_operations": recent_slow,
            "completed_operations": completed,
            "failed_operations": failed,
        }


def _is_client_disconnect(exc: BaseException) -> bool:
    """Return True for normal HTTP client disconnects during request/response IO."""

    if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
        return True
    return isinstance(exc, OSError) and getattr(exc, "errno", None) in _CLIENT_DISCONNECT_ERRNOS


def ensure_tmux_lifecycle_hooks() -> dict:
    """Best-effort install of the tmux hooks that feed daemon reconciliation.

    The canonical config also declares these, but daemon startup is the correct
    backstop because tmuxctld is the consumer that needs the event stream. This
    is deliberately non-fatal: tmux may be unavailable during launchd startup, in
    which case the normal tmux config still installs hooks when the workspace is
    created/sourced.
    """
    commands = [
        ("set-option", "-g", "remain-on-exit", "on"),
        ("set-hook", "-g", "pane-died[90]", _PANE_DIED_HOOK),
    ]
    results = []
    ok = True
    for command in commands:
        try:
            proc = subprocess.run(
                (tmux_binary(), *command),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            entry = {
                "command": command[0],
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stderr": (proc.stderr or "").strip()[:300],
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            entry = {
                "command": command[0],
                "ok": False,
                "returncode": None,
                "stderr": str(exc)[:300],
            }
        results.append(entry)
        ok = ok and bool(entry["ok"])
    if not ok:
        log.warning("tmux lifecycle hook install incomplete: %s", results)
    return {"ok": ok, "results": results}


class TmuxctldNotImplementedAnchor(RuntimeError):
    """Loud daemon-native placeholder for a route that is named but not built.

    A 501 anchor is the forward twin of a 410 CLI tombstone: deliberate, stable,
    and never a surface for feature logic. The route exists so callers can bind
    to the daemon URL first; until the real handler replaces the anchor it fails
    loudly with HTTP 501.
    """

    def __init__(self, method: str, path: str, *, detail: str = "") -> None:
        self.method = method.upper()
        self.path = path
        self.detail = detail
        super().__init__(f"{self.method} {self.path} is not implemented")


def not_implemented_anchor(method: str, path: str, *, detail: str = "") -> object:
    """Raise a loud 501 anchor for an intentionally unimplemented daemon route."""

    raise TmuxctldNotImplementedAnchor(method, path, detail=detail)


def _callback_path_from_env() -> Path:
    return Path(os.environ.get("TMUXCTLD_CALLBACKS_PATH") or DEFAULT_CALLBACKS_PATH).expanduser()


def _callback_ttl_from_env() -> float:
    raw = os.environ.get("TMUXCTLD_CALLBACK_TTL_SECONDS")
    try:
        ttl = float(raw) if raw is not None else DEFAULT_CALLBACK_TTL_SECONDS
    except ValueError:
        ttl = DEFAULT_CALLBACK_TTL_SECONDS
    return max(1.0, ttl)


def _deferred_sends_path_from_env() -> Path:
    return Path(
        os.environ.get("TMUXCTLD_DEFERRED_SENDS_PATH") or DEFAULT_DEFERRED_SENDS_PATH
    ).expanduser()


class PromptSubmitSniffer:
    """UserPromptSubmit acknowledgement bus for daemon send transactions.

    The daemon is the only process that can know "I issued this prompt send" at
    the exact time the bytes hit tmux. Token-API owns the agent hook receiver.
    The hook handler echoes UserPromptSubmit facts back here; the daemon waits
    on that echo before reporting a prompt send as verified.

    Recent pending callbacks are persisted so a daemon restart does not erase
    the caller's level-2 hook echo. The event deque remains intentionally
    volatile: only "send happened, echo caller when target submits" callbacks
    are durable.
    """

    def __init__(
        self,
        *,
        max_events: int = 2048,
        callbacks_path: str | Path | None = None,
        callback_ttl_seconds: float | None = None,
    ) -> None:
        self._cond = threading.Condition()
        self._events: deque[dict] = deque(maxlen=max_events)
        self._callbacks: dict[str, dict] = {}
        self._callbacks_path = Path(callbacks_path).expanduser() if callbacks_path else None
        self._callback_ttl_seconds = callback_ttl_seconds

    @property
    def callbacks_path(self) -> Path:
        return self._callbacks_path or _callback_path_from_env()

    @property
    def callback_ttl_seconds(self) -> float:
        if self._callback_ttl_seconds is not None:
            return max(1.0, float(self._callback_ttl_seconds))
        return _callback_ttl_from_env()

    def _is_stale(self, callback: dict, *, now: float | None = None) -> bool:
        try:
            registered_at = float(callback.get("registered_at") or 0)
        except (TypeError, ValueError):
            return True
        if registered_at <= 0:
            return True
        now = time.monotonic() if now is None else now
        # time.monotonic() is system-boot relative on macOS, so it survives a
        # daemon restart. If the host rebooted and the stored value is now in the
        # future, the callback is from a prior boot and cannot be safely matched.
        age = now - registered_at
        return age < 0 or age > self.callback_ttl_seconds

    def _prune_stale_locked(self, *, now: float | None = None) -> int:
        stale = [
            correlation_id
            for correlation_id, callback in self._callbacks.items()
            if self._is_stale(callback, now=now)
        ]
        for correlation_id in stale:
            self._callbacks.pop(correlation_id, None)
        return len(stale)

    def _write_locked(self) -> None:
        path = self.callbacks_path
        path.parent.mkdir(parents=True, exist_ok=True)
        callbacks = [
            dict(callback)
            for _, callback in sorted(self._callbacks.items(), key=lambda item: str(item[0]))
        ]
        payload = {
            "version": CALLBACKS_VERSION,
            "updated_epoch": time.time(),
            "callbacks": callbacks,
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, sort_keys=True, separators=(",", ":"))
                fh.write("\n")
            os.replace(tmp_name, path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _callback_from_mapping(item: object) -> dict | None:
        if not isinstance(item, dict):
            return None
        correlation_id = str(item.get("correlation_id") or "").strip()
        caller_pane = str(item.get("caller_pane") or "").strip()
        if not correlation_id or not caller_pane:
            return None
        try:
            since = float(item.get("since") or 0)
            registered_at = float(item.get("registered_at") or 0)
        except (TypeError, ValueError):
            return None
        callback = {
            "correlation_id": correlation_id,
            "caller_pane": caller_pane,
            "target_pane": str(item.get("target_pane") or "").strip(),
            "target_label": str(item.get("target_label") or "").strip(),
            "instance_id": str(item.get("instance_id") or "").strip(),
            "payload_hash": str(item.get("payload_hash") or "").strip(),
            "since": since,
            "registered_at": registered_at,
            "fired": bool(item.get("fired")),
        }
        return callback

    def load_callbacks(self, *, force: bool = False) -> dict:
        """Load persisted pending callbacks, dropping entries past the turn TTL."""
        path = self.callbacks_path
        with self._cond:
            if self._callbacks and not force:
                return {"loaded": False, "path": str(path), "callbacks": len(self._callbacks)}
            loaded = 0
            dropped = 0
            callbacks: dict[str, dict] = {}
            try:
                with path.open("r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except FileNotFoundError:
                self._callbacks = {}
                return {"loaded": True, "path": str(path), "callbacks": 0, "dropped": 0}
            raw_callbacks = payload.get("callbacks") if isinstance(payload, dict) else None
            if not isinstance(raw_callbacks, list):
                raw_callbacks = []
            now = time.monotonic()
            for item in raw_callbacks:
                callback = self._callback_from_mapping(item)
                if callback is None or callback.get("fired") or self._is_stale(callback, now=now):
                    dropped += 1
                    continue
                callbacks[str(callback["correlation_id"])] = callback
                loaded += 1
            self._callbacks = callbacks
            if dropped:
                self._write_locked()
            return {
                "loaded": True,
                "path": str(path),
                "callbacks": loaded,
                "dropped": dropped,
            }

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
        pane: str = "",
        payload_hash: str,
        since: float,
    ) -> bool:
        if event.get("at", 0) < since:
            return False
        event_instance_id = str(event.get("instance_id") or "").strip()
        if instance_id:
            if event_instance_id:
                if event_instance_id != instance_id:
                    return False
            elif pane and event.get("pane") != pane:
                # Some UserPromptSubmit hooks are pane+hash-only. When the send
                # callback has an authoritative ledger instance_id but the hook
                # omitted it, allow an exact target pane match to carry the
                # event through to the payload-hash check below.
                return False
            elif not pane:
                return False
        elif pane and event.get("pane") != pane:
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
                        pane="",
                        payload_hash=payload_hash,
                        since=since,
                    ):
                        return dict(event)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)

    def register_callback(
        self,
        *,
        correlation_id: str,
        caller_pane: str,
        target_pane: str,
        instance_id: str,
        payload_hash: str,
        since: float,
        target_label: str = "",
    ) -> dict | None:
        correlation_id = str(correlation_id or "").strip()
        caller_pane = str(caller_pane or "").strip()
        if not correlation_id or not caller_pane:
            return None
        callback = {
            "correlation_id": correlation_id,
            "caller_pane": caller_pane,
            "target_pane": str(target_pane or "").strip(),
            "target_label": str(target_label or "").strip(),
            "instance_id": str(instance_id or "").strip(),
            "payload_hash": str(payload_hash or "").strip(),
            "since": since,
            "registered_at": time.monotonic(),
            "fired": False,
        }
        with self._cond:
            self._prune_stale_locked()
            # Idempotent operation replays must not create duplicate late echoes.
            self._callbacks.setdefault(correlation_id, callback)
            self._write_locked()
            return dict(self._callbacks[correlation_id])

    def pop_matching_callbacks(self, event: dict) -> list[dict]:
        matched: list[dict] = []
        with self._cond:
            changed = bool(self._prune_stale_locked())
            for correlation_id, callback in list(self._callbacks.items()):
                if callback.get("fired"):
                    continue
                if self._is_stale(callback):
                    self._callbacks.pop(correlation_id, None)
                    changed = True
                    continue
                if not self._matches(
                    event,
                    instance_id=str(callback.get("instance_id") or ""),
                    pane=str(callback.get("target_pane") or ""),
                    payload_hash=str(callback.get("payload_hash") or ""),
                    since=float(callback.get("since") or 0),
                ):
                    continue
                callback["fired"] = True
                matched.append(dict(callback))
                self._callbacks.pop(correlation_id, None)
                changed = True
            if changed:
                self._write_locked()
        return matched


_PROMPT_SUBMIT_SNIFFER = PromptSubmitSniffer()


class DeferredSendQueue:
    """Durable FIFO queue for typing-guard-held directed sends.

    Queue entries are persisted before the HTTP caller receives a queued
    receipt.  Drain removes an entry before replaying it: this favors the
    no-duplicate-delivery invariant over retrying an ambiguous in-flight send
    after a daemon crash.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path).expanduser() if path else None
        self._lock = threading.RLock()
        self._items: list[dict] = []
        self._seq = 0

    @property
    def path(self) -> Path:
        return self._path or _deferred_sends_path_from_env()

    def _write_locked(self) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": DEFERRED_SENDS_VERSION,
            "updated_epoch": time.time(),
            "items": self._items,
            "seq": self._seq,
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, sort_keys=True, separators=(",", ":"))
                fh.write("\n")
            os.replace(tmp_name, path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _item_from_mapping(item: object) -> dict | None:
        if not isinstance(item, dict):
            return None
        item_id = str(item.get("id") or "").strip()
        route = str(item.get("route") or "").strip()
        pane = str(item.get("pane") or "").strip()
        phys_pane = str(item.get("phys_pane") or "").strip()
        params = item.get("params")
        if not item_id or not route or not phys_pane or not isinstance(params, dict):
            return None
        try:
            seq = int(item.get("seq") or 0)
            queued_at = float(item.get("queued_at") or 0)
        except (TypeError, ValueError):
            return None
        ttl_raw = item.get("ttl_seconds")
        ttl_seconds = None
        if ttl_raw not in (None, ""):
            try:
                ttl_seconds = float(ttl_raw)
            except (TypeError, ValueError):
                ttl_seconds = None
        return {
            "id": item_id,
            "seq": seq,
            "route": route,
            "pane": pane,
            "phys_pane": phys_pane,
            "params": dict(params),
            "queued_at": queued_at,
            "ttl_seconds": ttl_seconds,
            "gate": dict(item.get("gate") or {}),
        }

    def load(self, *, force: bool = False) -> dict:
        with self._lock:
            if self._items and not force:
                return {"loaded": False, "path": str(self.path), "queued": len(self._items)}
            try:
                with self.path.open("r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except FileNotFoundError:
                self._items = []
                self._seq = 0
                return {"loaded": True, "path": str(self.path), "queued": 0, "dropped": 0}
            raw_items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(raw_items, list):
                raw_items = []
            loaded: list[dict] = []
            dropped = 0
            now = time.time()
            for raw in raw_items:
                item = self._item_from_mapping(raw)
                if item is None or self._is_expired(item, now=now):
                    dropped += 1
                    continue
                loaded.append(item)
            loaded.sort(key=lambda it: (int(it.get("seq") or 0), str(it.get("id") or "")))
            self._items = loaded
            try:
                stored_seq = int(payload.get("seq") or 0) if isinstance(payload, dict) else 0
            except (TypeError, ValueError):
                stored_seq = 0
            self._seq = max(stored_seq, *(int(it["seq"]) for it in loaded), 0)
            if dropped:
                self._write_locked()
            return {
                "loaded": True,
                "path": str(self.path),
                "queued": len(self._items),
                "dropped": dropped,
            }

    @staticmethod
    def _is_expired(item: dict, *, now: float | None = None) -> bool:
        ttl = item.get("ttl_seconds")
        if ttl is None:
            return False
        try:
            ttl_f = float(ttl)
            queued_at = float(item.get("queued_at") or 0)
        except (TypeError, ValueError):
            return True
        return ttl_f > 0 and (now if now is not None else time.time()) - queued_at > ttl_f

    def enqueue(
        self,
        *,
        route: str,
        params: dict,
        pane: str,
        phys_pane: str,
        gate: dict,
        ttl_seconds: float | None = None,
    ) -> dict:
        with self._lock:
            self._seq += 1
            item_id = str(uuid.uuid4())
            stored_params = {
                str(k): v
                for k, v in params.items()
                if not str(k).startswith("_typing_guard_deferred_")
            }
            item = {
                "id": item_id,
                "seq": self._seq,
                "route": route,
                "pane": pane,
                "phys_pane": phys_pane,
                "params": stored_params,
                "queued_at": time.time(),
                "ttl_seconds": ttl_seconds,
                "gate": dict(gate),
            }
            self._items.append(item)
            self._write_locked()
            return dict(item)

    def pop_ready_for_pane(self, phys_pane: str) -> dict | None:
        with self._lock:
            now = time.time()
            changed = False
            kept: list[dict] = []
            expired = 0
            for item in self._items:
                if self._is_expired(item, now=now):
                    expired += 1
                    changed = True
                    continue
                kept.append(item)
            self._items = kept
            for idx, item in enumerate(self._items):
                if str(item.get("phys_pane") or "") == phys_pane:
                    popped = self._items.pop(idx)
                    self._write_locked()
                    return popped
            if changed:
                log.warning("tmuxctld: dropped %d expired deferred send(s)", expired)
                self._write_locked()
            return None

    def requeue_front(self, item: dict) -> None:
        with self._lock:
            self._items = [dict(item)] + [
                it for it in self._items if it.get("id") != item.get("id")
            ]
            self._write_locked()

    def by_pane(self) -> dict[str, int]:
        with self._lock:
            counts: dict[str, int] = {}
            for item in self._items:
                pane = str(item.get("phys_pane") or "")
                counts[pane] = counts.get(pane, 0) + 1
            return counts

    def size(self) -> int:
        with self._lock:
            return len(self._items)


_DEFERRED_SEND_QUEUE = DeferredSendQueue()
_DEFERRED_DRAINING: set[str] = set()
_DEFERRED_DRAIN_LOCK = threading.Lock()


def _typing_guard_policy(params: dict) -> tuple[str, str | None]:
    policy = str(params.get("typing_guard_policy") or params.get("send_gate_policy") or "enqueue")
    policy = policy.strip().lower()
    if policy in {"", "defer", "queue", "queued"}:
        policy = "enqueue"
    reason = str(
        params.get("typing_guard_drop_reason") or params.get("send_gate_drop_reason") or ""
    ).strip()
    if policy not in {"enqueue", "drop"}:
        raise ValueError("typing_guard_policy must be enqueue or drop")
    if policy == "drop" and not reason:
        raise ValueError("typing_guard_policy=drop requires typing_guard_drop_reason")
    return policy, reason or None


def _typing_guard_ttl(params: dict) -> float | None:
    raw = params.get("typing_guard_ttl_seconds") or params.get("defer_ttl_seconds")
    if raw in (None, ""):
        return None
    try:
        ttl = float(raw)
    except (TypeError, ValueError):
        return None
    return ttl if ttl > 0 else None


def _typing_gate_detail(phys_pane: str, *, gate: dict | None = None) -> dict:
    detail = {
        "suppressed": True,
        "reason": "typing_guard",
        "gate": "human_lock",
        "policy": "enqueue",
        "target": phys_pane,
        "deferred": True,
    }
    if gate:
        detail.update(gate)
        detail["reason"] = "typing_guard"
        detail["target"] = gate.get("target") or phys_pane
    return detail


def _deferred_receipt(item: dict, *, gate: dict) -> dict:
    return {
        "status": "queued",
        "queued": True,
        "deferred": True,
        "delivered": False,
        "submitted": False,
        "queue_id": item["id"],
        "queue_seq": item["seq"],
        "pane": item.get("pane") or item.get("phys_pane"),
        "physical_pane": item.get("phys_pane"),
        "target": item.get("phys_pane"),
        "reason": "typing_guard",
        "gate": gate,
        "queue_path": str(_DEFERRED_SEND_QUEUE.path),
    }


def _drop_receipt(*, pane: str, phys_pane: str, reason: str, gate: dict) -> dict:
    return {
        "status": "dropped",
        "dropped": True,
        "queued": False,
        "deferred": False,
        "delivered": False,
        "submitted": False,
        "pane": pane,
        "physical_pane": phys_pane,
        "reason": "typing_guard",
        "drop_reason": reason,
        "gate": {**gate, "policy": "drop", "drop_reason": reason},
    }


def _defer_or_drop_typing_guard(
    *,
    route: str,
    params: dict,
    pane: str,
    phys_pane: str,
    gate: dict | None = None,
) -> dict | None:
    """Return a queued/dropped receipt when the typing guard blocks this send."""

    active_gate = gate if gate and gate.get("reason") == "typing_guard" else None
    if active_gate is None and send_gate._pane_human_locked(phys_pane):
        active_gate = _typing_gate_detail(phys_pane)
    if active_gate is None or not active_gate.get("suppressed", True):
        return None

    policy, drop_reason = _typing_guard_policy(params)
    active_gate = _typing_gate_detail(phys_pane, gate=active_gate)
    if policy == "drop":
        return _drop_receipt(
            pane=pane,
            phys_pane=phys_pane,
            reason=drop_reason or "unspecified",
            gate=active_gate,
        )
    if _b(params, "_typing_guard_deferred_drain"):
        raise TmuxSendGated({**active_gate, "drain_reblocked": True})
    item = _DEFERRED_SEND_QUEUE.enqueue(
        route=route,
        params=params,
        pane=pane,
        phys_pane=phys_pane,
        gate=active_gate,
        ttl_seconds=_typing_guard_ttl(params),
    )
    _schedule_deferred_drain(phys_pane)
    log.warning(
        "tmuxctld: queued deferred send route=%s pane=%s queue_id=%s",
        route,
        _safe_public_role(pane),
        item["id"],
    )
    return _deferred_receipt(item, gate=active_gate)


def _execute_deferred_send(item: dict) -> dict:
    route = str(item.get("route") or "")
    handler = _DEFERRED_ROUTE_HANDLERS.get(route)
    if handler is None:
        raise ValueError(f"deferred send route is not replayable: {route}")
    params = dict(item.get("params") or {})
    params["_typing_guard_deferred_drain"] = True
    params["_typing_guard_deferred_id"] = item.get("id")
    if not str(params.get("operation_id") or "").strip():
        params["operation_id"] = f"deferred:{item.get('id')}"
    control = TmuxControlPlane(TmuxAdapter())
    return handler(control, params)


def _drain_deferred_sends_for_pane(phys_pane: str) -> dict:
    drained = 0
    reblocked = False
    failures: list[dict] = []
    while not send_gate._pane_human_locked(phys_pane):
        item = _DEFERRED_SEND_QUEUE.pop_ready_for_pane(phys_pane)
        if item is None:
            break
        try:
            _execute_deferred_send(item)
            drained += 1
        except TmuxSendGated as exc:
            if exc.gate.get("reason") == "typing_guard":
                _DEFERRED_SEND_QUEUE.requeue_front(item)
                reblocked = True
                break
            failures.append({"queue_id": item.get("id"), "error": "gated", "detail": exc.gate})
        except Exception as exc:  # noqa: BLE001 - preserve daemon loop, log server-side.
            log.exception("tmuxctld: deferred send failed queue_id=%s", item.get("id"))
            failures.append({"queue_id": item.get("id"), "error": type(exc).__name__})
    return {"pane": phys_pane, "drained": drained, "reblocked": reblocked, "failures": failures}


def _schedule_deferred_drain(phys_pane: str) -> None:
    if not phys_pane:
        return
    with _DEFERRED_DRAIN_LOCK:
        if phys_pane in _DEFERRED_DRAINING:
            return
        _DEFERRED_DRAINING.add(phys_pane)

    def _worker() -> None:
        try:
            while send_gate._pane_human_locked(phys_pane):
                time.sleep(send_gate._typing_delay_sleep(phys_pane))
            _drain_deferred_sends_for_pane(phys_pane)
        finally:
            with _DEFERRED_DRAIN_LOCK:
                _DEFERRED_DRAINING.discard(phys_pane)

    threading.Thread(target=_worker, name=f"typing-guard-drain-{phys_pane}", daemon=True).start()


def _schedule_all_deferred_drains() -> None:
    for phys_pane in _DEFERRED_SEND_QUEUE.by_pane():
        _schedule_deferred_drain(phys_pane)


def _listen_fd_from_env() -> int | None:
    """Return the first systemd-style inherited listen fd, if LISTEN_FDS says one exists."""
    raw = os.environ.get("LISTEN_FDS")
    if not raw:
        return None
    try:
        listen_fds = int(raw)
    except ValueError:
        return None
    if listen_fds <= 0:
        return None
    listen_pid = os.environ.get("LISTEN_PID")
    if listen_pid:
        try:
            if int(listen_pid) != os.getpid():
                return None
        except ValueError:
            return None
    return 3


def _activated_listen_fd() -> tuple[int, str] | None:
    """Return an inherited listening fd plus source label, or None for self-bind."""
    fd = _listen_fd_from_env()
    if fd is not None:
        return fd, "LISTEN_FDS"
    try:
        from .launchd_socket import activated_fd

        fd = activated_fd("Listeners")
    except Exception:  # noqa: BLE001
        log.exception("tmuxctld launchd socket activation check failed")
        fd = None
    if fd is not None:
        return fd, "launchd"
    return None


def _emit_prompt_submit_callback(control, callback: dict, event: dict) -> dict:
    """Best-effort late level-2 echo to the original caller pane.

    The callback is deliberately a one-shot side effect keyed by correlation id.
    It is not a delivery verdict; level-1 was already returned to the caller when
    bytes reached the target pane.
    """

    correlation_id = str(callback.get("correlation_id") or "").strip()
    caller_pane = str(callback.get("caller_pane") or "").strip()
    target = _safe_public_role(str(callback.get("target_label") or "")) or _safe_public_role(
        str(callback.get("target_pane") or "")
    )
    text = (
        "[tmuxctld:hook-echo] "
        f"correlation_id={correlation_id} delivered=1 turn=submitted target={target}"
    )
    try:
        if hasattr(control.adapter, "send_text_then_submit"):
            control.adapter.send_text_then_submit(
                caller_pane,
                text,
                clear_prompt=False,
                pre_submit_keys=(),
                submit_settle_seconds=0,
            )
        else:
            control.adapter.run("send-keys", "-t", caller_pane, "-l", text)
            if hasattr(control.adapter, "send_keys"):
                control.adapter.send_keys(caller_pane, "C-m")
            else:
                control.adapter.run("send-keys", "-t", caller_pane, "C-m")
        return {
            "correlation_id": correlation_id,
            "caller_pane": caller_pane,
            "target_pane": callback.get("target_pane"),
            "target_label": callback.get("target_label"),
            "status": "sent",
            "event": event,
        }
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "tmuxctld: prompt-submit hook echo failed correlation_id=%s caller=%s: %s",
            correlation_id,
            _safe_public_role(caller_pane),
            exc,
        )
        return {
            "correlation_id": correlation_id,
            "caller_pane": caller_pane,
            "target_pane": callback.get("target_pane"),
            "target_label": callback.get("target_label"),
            "status": "failed",
            "error": str(exc),
            "event": event,
        }


class SendOperationIdempotency:
    """Per-operation send cache; never dedups without an explicit operation id."""

    def __init__(self, *, max_entries: int = 2048) -> None:
        self._cond = threading.Condition()
        self._max_entries = max_entries
        self._entries: dict[str, dict] = {}

    @staticmethod
    def _matches(entry: dict, *, pane: str, payload_hash: str) -> bool:
        return entry.get("pane") == pane and entry.get("payload_hash") == payload_hash

    def begin(self, operation_id: str, *, pane: str, payload_hash: str) -> dict | None:
        if not operation_id:
            return None
        with self._cond:
            while True:
                entry = self._entries.get(operation_id)
                if entry is None:
                    self._entries[operation_id] = {
                        "state": "inflight",
                        "pane": pane,
                        "payload_hash": payload_hash,
                        "started_at": time.monotonic(),
                    }
                    return None
                if not self._matches(entry, pane=pane, payload_hash=payload_hash):
                    raise ValueError("operation_id reused for different pane/payload")
                if entry.get("state") == "done":
                    result = dict(entry.get("result") or {})
                    result["idempotent_replay"] = True
                    return result
                self._cond.wait(timeout=5.0)

    def abort(self, operation_id: str) -> None:
        if not operation_id:
            return
        with self._cond:
            entry = self._entries.get(operation_id)
            if entry and entry.get("state") == "inflight":
                self._entries.pop(operation_id, None)
            self._cond.notify_all()

    def finish(self, operation_id: str, *, pane: str, payload_hash: str, result: dict) -> None:
        if not operation_id:
            return
        with self._cond:
            self._entries[operation_id] = {
                "state": "done",
                "pane": pane,
                "payload_hash": payload_hash,
                "result": dict(result),
                "finished_at": time.monotonic(),
            }
            while len(self._entries) > self._max_entries:
                done_keys = [
                    key for key, entry in self._entries.items() if entry.get("state") == "done"
                ]
                if not done_keys:
                    break
                oldest = min(
                    done_keys,
                    key=lambda key: float(self._entries[key].get("finished_at") or 0),
                )
                self._entries.pop(oldest, None)
            self._cond.notify_all()


_SEND_IDEMPOTENCY = SendOperationIdempotency()


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


def tmux_socket_path() -> Path:
    """Return the tmux control socket path this daemon depends on."""
    configured = os.environ.get("TMUXCTLD_TMUX_SOCKET_PATH", "").strip()
    if configured:
        return Path(configured)
    return Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}" / "default"


def tmux_socket_connectable(path: Path | None = None) -> bool:
    """Fail-closed AF_UNIX connect probe for the tmux socket file."""
    socket_path = path or tmux_socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(float(os.environ.get("TMUXCTLD_SOCKET_PROBE_SECONDS", "0.25")))
            sock.connect(str(socket_path))
        return True
    except Exception:
        return False


def _live_fleet_tmux_server_pids(session: str = "main") -> list[int]:
    """Find live fleet tmux server processes without starting tmux."""
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        return []
    pids: list[int] = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_s, _, command = stripped.partition(" ")
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if pid == os.getpid() or "tmux" not in command or "new-session" not in command:
            continue
        if f"-s {session}" in command or f"-s{session}" in command:
            pids.append(pid)
    return pids


def recover_missing_tmux_socket(session: str = "main") -> dict:
    """Name and heal socket-file loss by asking the live server to re-bind.

    Never boots tmux. If the socket is absent/unconnectable and exactly one live
    fleet server process is present, send SIGUSR1; tmux recreates its socket.
    """
    path = tmux_socket_path()
    before = tmux_socket_connectable(path)
    state = "reachable" if before else ("socket_loss" if path.exists() else "socket_missing")
    result = {
        "state": state,
        "socket_path": str(path),
        "recovery": "none",
        "server_pids": [],
    }
    if before:
        return result
    pids = _live_fleet_tmux_server_pids(session=session)
    result["server_pids"] = pids
    if len(pids) == 1:
        os.kill(pids[0], signal.SIGUSR1)
        result["recovery"] = "sigusr1_rebind"
    elif len(pids) > 1:
        result["recovery"] = "ambiguous_live_servers_noop"
    else:
        result["recovery"] = "no_live_server_noop"
    return result


def tmux_reachable(adapter: TmuxAdapter) -> bool:
    """Fail-closed probe: socket connect must work, then tmux must answer."""
    if not tmux_socket_connectable():
        return False
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
    ledger_row = control.ledger_resolve(
        target,
        wrapper_id=_s(params, "wrapper_id"),
        instance_id=_s(params, "instance_id"),
        pane_positional_id=_s(params, "pane_positional_id") or _s(params, "pane_label"),
    )
    if ledger_row.get("found"):
        row = ledger_row["row"]
        if _s(params, "format", "full") == "json":
            return {
                "requested": target
                or _s(params, "wrapper_id")
                or _s(params, "instance_id")
                or _s(params, "pane_positional_id")
                or _s(params, "pane_label"),
                "pane_id": row["pane_positional_id"],
                "role": row["pane_positional_id"],
                "kind": "ledger",
                "agent": row["engine"] or "auto",
                "live_agent": bool(row["engine"]),
                "ledger": row,
            }
        if _s(params, "format", "full") == "id":
            return row["pane_positional_id"]
        if _s(params, "format", "full") == "physical":
            return control.physical_pane_id(row["pane_positional_id"])
        return "\n".join(
            [
                f"requested: {target or row['wrapper_id']}",
                f"pane_id: {row['pane_positional_id'] or '(unset)'}",
                f"role: {row['pane_positional_id'] or '(unset)'}",
                "kind: ledger",
                f"agent: {row['engine'] or 'auto'}",
                f"live_agent: {str(bool(row['engine'])).lower()}",
                f"wrapper_id: {row['wrapper_id']}",
                f"instance_id: {row['instance_id']}",
            ]
        )
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


_TOKEN_API_TMUX_RUN_COMMANDS = frozenset(
    {
        "capture-pane",
        "display-message",
        "list-clients",
        "list-panes",
        "pipe-pane",
        "resize-pane",
        "select-pane",
        "set-option",
        "show-options",
    }
)


def _h_tmux_run(control, params):
    """Small allowlisted tmux adapter bridge for legacy Token-API reads.

    This is not a shell escape hatch: callers supply argv tokens, ``send-keys``
    is deliberately excluded in favour of the existing send-text/send-keys
    daemon APIs, and execution goes through ``TmuxAdapter.run`` so target
    resolution, send/focus guards, and pane runtime invariants remain daemon
    owned.
    """

    raw_args = params.get("args")
    if not isinstance(raw_args, list) or not raw_args:
        raise ValueError("args list required")
    args = tuple(str(arg) for arg in raw_args)
    command = args[0]
    if command not in _TOKEN_API_TMUX_RUN_COMMANDS:
        raise ValueError(f"tmux command not allowed through /tmux/run: {command}")
    return {"stdout": control.adapter.run(*args, allow_failure=False), "args": list(args)}


def _h_instance_show_option(control, params):
    return control.instance_show_option(_s(params, "instance_id"), _s(params, "option"))


# -- Send + act (POST) ------------------------------------------------------


def _resolve_physical_pane_or_gate(control, pane: str) -> str:
    """Resolve ``pane`` to the physical tmux ``%NN`` or fail closed."""
    if pane.startswith("%"):
        return pane
    try:
        return resolve_to_physical(control.adapter, pane)
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


def _raise_if_human_locked(phys: str) -> None:
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


def _refuse_send_into_human_lock(control, pane: str) -> str:
    """Resolve ``pane`` and fail closed on a live HUMAN keystroke/pending lock.

    The send gate honors a process-global ``TMUX_SEND_GATE_ALLOW`` sanctioned
    override and yields it back to a human lock for ONLY the daemon's own
    thread-local transaction reasons (``tmuxctld-send-holder`` /
    ``tmuxctl-submit-transaction`` / ``tmuxctld-direct-user``). Every OTHER
    override reason — including the process-global env override an enforce-action
    sets (e.g. token-api's
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
    reads daemon-owned JSON guard state per physical pane). A canonical caller id
    (``council:custodes``, ``mechanicus:N``, …) must therefore be resolved before
    the lock read, or the read keys off a non-physical target tmux does not
    understand, the lock reads as unset, and the send pierces. So resolution is
    split: a missing resolver (``AttributeError`` — a fail-open test/shim adapter)
    falls back to the caller id (a raw ``%NN`` is already physical), but a GENUINE
    resolution failure fails closed — we will not gamble a pierce on an unresolved
    canonical id.
    """
    phys = _resolve_physical_pane_or_gate(control, pane)
    _raise_if_human_locked(phys)
    return phys


def _h_send_keys(control, params):
    pane = _s(params, "pane")
    command = _opt(params, "command") or _opt(params, "key") or ""
    if not command:
        raw_keys = params.get("keys")
        if isinstance(raw_keys, list) and len(raw_keys) == 1:
            key = raw_keys[0]
            command = "" if key is None else str(key)
    if not command:
        raise ValueError("command/key required")
    from .occupancy import assert_dispatch_target_available, looks_like_dispatch_launcher_payload

    # Inviolable human-lock fail-closed before any byte-bearing send: an ambient
    # TMUX_SEND_GATE_ALLOW override (enforce-action / quiet-hours pierce) must
    # never clobber active typing at this chokepoint.
    phys_pane = _resolve_physical_pane_or_gate(control, pane)
    deferred = _defer_or_drop_typing_guard(
        route="/send-keys", params=params, pane=pane, phys_pane=phys_pane
    )
    if deferred is not None:
        return {**deferred, "command": command, "sent": False}
    if looks_like_dispatch_launcher_payload(command):
        assert_dispatch_target_available(control.adapter, phys_pane)
    if _b(params, "no_escape"):
        control.adapter.run("send-keys", "-t", phys_pane, "-l", command)
    else:
        control.adapter.send_keys(phys_pane, command)
    # adapter.run() suppresses a gated send SILENTLY (sets last_send_gate_result,
    # returns ""), so — like send_text_then_submit — surface the structured gate
    # instead of falsely reporting sent:True. Dispatch turns this into the
    # {ok:false, error:{code:"gated"}} envelope (zero bytes written, re-queueable).
    gate = getattr(control.adapter, "last_send_gate_result", None)
    if gate:
        raise TmuxSendGated(gate)
    return {"pane": pane, "physical_pane": phys_pane, "command": command, "sent": True}


# The captured composer slice we fingerprint for the white-whale "submit
# swallowed as a prompt newline" failure. A short head of the payload is enough
# to confirm the bytes landed in the composer.
_SWALLOW_NEEDLE_LEN = 48

# Characters that may legitimately sit BELOW a stuck draft without meaning the
# input was consumed: blank padding and TUI box-drawing borders (the composer's
# own frame). A line made of only these is composer chrome, not fresh output.
_COMPOSER_CHROME_CHARS = frozenset(" \t │┃┆┇┊┋─━┄┅┈┉╌╍╭╮╰╯┌┐└┘├┤┬┴┼╔╗╚╝╠╣╦╩╬║═▏▎▍▌▋▊▉▐▔▕░▒▓")


def _is_composer_chrome_line(line: str) -> bool:
    """True when a line is only blank padding / box-drawing (composer border)."""
    return all(ch in _COMPOSER_CHROME_CHARS for ch in line)


def _detect_swallowed_submit(capture: str, payload: str) -> bool:
    """Heuristic: did the TUI swallow the Enter into the prompt body?

    The white-whale failure (live Codex/Claude repro) is: the literal payload
    bytes land in the composer, but the C-m that should submit is ingested as a
    newline *inside* the draft instead. The capture signature is (a) a
    representative head of the submitted payload still sitting in the composer,
    (b) the captured region ending in a trailing newline (the swallowed Enter
    left the cursor on a fresh line rather than submitting), AND (c) the draft
    is still at the BOTTOM of the pane — only composer chrome (blank lines,
    box-drawing borders) sits below it.

    Condition (c) is what distinguishes a genuinely stuck draft from an
    ALREADY-DELIVERED send whose payload merely appears in scrollback: a
    bare-zsh command echoes itself, then prints output and a fresh shell prompt
    BELOW the echo; an agent-TUI submit scrolls the prompt up into the
    transcript above an emptied composer. In both, substantive (non-chrome)
    text follows the needle, so the send landed and this returns False. Without
    (c), the bare-shell echo tripped both legacy signals and false-failed every
    `:new` dispatch onto a parked pre-alloc pane — the bug this guards against.

    A clean submit leaves an empty composer (needle absent) — returns False, so
    the recovery C-m is fired only when there is real evidence of a stuck draft.
    """
    if not capture or not payload:
        return False
    needle = normalize_prompt_payload(payload).strip()[:_SWALLOW_NEEDLE_LEN]
    if not needle or needle not in capture:
        return False
    if not capture.endswith("\n"):
        return False
    # The needle has no newlines (normalize collapses them), so every occurrence
    # lies within a single splitlines() segment. If any line after the LAST such
    # occurrence carries substantive text, the input was consumed → delivered.
    lines = capture.splitlines()
    last_idx = max(i for i, line in enumerate(lines) if needle in line)
    for trailing in lines[last_idx + 1 :]:
        if not _is_composer_chrome_line(trailing):
            return False
    return True


@contextlib.contextmanager
def _agent_guard_transaction(control, pane: str, *, seconds: int = 8):
    """Own a multi-part daemon pane-write transaction with a JSON AGENT guard."""
    phys_pane = _resolve_physical_pane_or_gate(control, pane)
    _raise_if_human_locked(phys_pane)
    owner = None
    try:
        owner = typing_guard_state.hold(
            typing_guard_state.Tmux(typing_guard_state.tmux_binary()),
            phys_pane,
            seconds=seconds,
            now=typing_guard_state.now_epoch(),
        )
    except Exception as exc:
        log.debug("tmuxctld: agent-guard transaction hold skipped pane=%s: %s", phys_pane, exc)
        owner = None
    ctx = (
        thread_local_override("tmuxctld-send-holder", owner=owner)
        if owner
        else contextlib.nullcontext()
    )
    try:
        with ctx:
            yield owner
    finally:
        if owner:
            try:
                typing_guard_state.release(
                    typing_guard_state.Tmux(typing_guard_state.tmux_binary()),
                    phys_pane,
                    now=typing_guard_state.now_epoch(),
                    owner=owner,
                )
            except Exception as exc:
                log.warning(
                    "tmuxctld: agent-guard transaction release failed pane=%s: %s", phys_pane, exc
                )


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
    # `/api/notify` (NotifyRequest) declares `vibe: int | None` — the phone/Pavlok
    # tactile intensity. The prior string "alert" was rejected 422, so recoveries
    # never reached the human router. 30 matches the fleet's standard attention
    # vibe (see token-api dispatch_notify call sites).
    body = json.dumps(
        {
            "message": message,
            "tts": True,
            "vibe": 30,
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


def _parse_pre_submit_keys(raw) -> tuple[str, ...]:
    if isinstance(raw, str):
        return tuple(k for k in raw.split(",") if k)
    if isinstance(raw, list | tuple):
        return tuple(str(k) for k in raw if str(k))
    return ()


def _send_operation_fingerprint(
    kind: str,
    *,
    text: str = "",
    effects: dict | None = None,
) -> str:
    """Hash the full local-send effect for operation-id reuse checks.

    ``payload_hash`` in API results intentionally remains the prompt/text hash
    that callers already know. The idempotency cache needs a stricter identity:
    same operation id + same visible payload but different effects (submit vs
    insert, clear prompt, sink keys, keypress count, etc.) is a different
    operation and must be rejected instead of replayed.
    """

    return hashlib.sha256(
        json.dumps(
            {"kind": kind, "text": text, "effects": effects or {}},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _capture_pane_text(control, phys_pane: str, *, lines: int = 20) -> str:
    if hasattr(control.adapter, "capture_pane"):
        return control.adapter.capture_pane(phys_pane, lines=lines)
    return control.adapter.run(
        "capture-pane", "-t", phys_pane, "-p", "-S", str(-lines), allow_failure=True
    )


def _wait_for_insert_confirmation(
    control,
    *,
    phys_pane: str,
    text: str,
    timeout: float,
) -> tuple[bool, str]:
    """Read back the visible draft and confirm that inserted bytes landed.

    This is the insert-only "belt". It is deliberately a read-back of the pane,
    not a debounce or after-the-fact duplicate suppressor. A missing read-back is
    surfaced as unverified, but still cached under an explicit operation id so a
    retry cannot blindly duplicate bytes that may already be in the live draft.
    """

    needle = normalize_prompt_payload(text).strip()
    if not needle:
        return True, ""
    deadline = time.monotonic() + max(0.0, timeout)
    last_capture = ""
    while True:
        try:
            last_capture = _capture_pane_text(control, phys_pane, lines=20)
        except Exception as exc:
            log.debug("tmuxctld: insert confirmation capture failed pane=%s: %s", phys_pane, exc)
            last_capture = ""
        # The needle is newline-collapsed (normalize_prompt_payload turns
        # [\r\n]+ into a single space), but call sites send the raw, un-collapsed
        # bytes, so capture-pane returns embedded newlines for any multi-line
        # payload (e.g. forwarded Discord messages). Collapse the read-back the
        # SAME way before matching so a genuine multi-line insert is not
        # spuriously reported "unverified" — which would push callers to retry
        # and risk duplicate inserts.
        haystack = re.sub(r"[\r\n]+", " ", last_capture)
        if needle in haystack:
            return True, last_capture
        if time.monotonic() >= deadline:
            return False, last_capture
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _detect_codex_user_message(capture: str, payload: str) -> bool:
    """Return True when capture shows Codex accepted ``payload`` as a user turn.

    Codex renders submitted user messages in the transcript with a leading ``›``
    marker. The live composer can also contain the payload, but it sits inside
    bordered chrome (for example ``│ › draft``), so require the marker at the
    start of a captured line after whitespace. This is deliberately narrower than
    "payload appears anywhere"; absence of this signal is unverified, not success.
    """

    if not capture or not payload:
        return False
    needle = normalize_prompt_payload(payload).strip()[:_SWALLOW_NEEDLE_LEN]
    if not needle:
        return False
    for line in capture.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("›"):
            continue
        haystack = re.sub(r"[\r\n]+", " ", stripped)
        if needle in haystack:
            return True
    return False


def _classify_submit_delivery(
    control,
    *,
    phys_pane: str,
    text: str,
    ack: dict | None,
) -> tuple[str, str, str, str | None]:
    """Advisory pane-scrape for a SUBMITTED prompt that carries no ack.

    #516's insert belt (``_wait_for_insert_confirmation``) is a PRE-submit
    read-back: it confirms the draft LANDED in the composer. This is its
    POST-submit sibling. A prompt was submitted but no ``UserPromptSubmit`` ack
    came back — either the send was fired with ``verify=false`` (dispatch's
    launch path), or the ack timed out because the message is queued behind a
    running tool call in the target agent (a wide grep, or ``pr-step`` running
    for minutes). Fast-failing there is the bug: the send genuinely succeeded,
    it is just not confirmed yet.

    Pane-scrape is unreliable for precise matching, so this NEVER hard-asserts
    delivery — it returns an ADVISORY the caller can weigh, not a verdict.

    Returns ``(delivery, advisory, capture_excerpt, verified_by)`` where
    ``delivery`` is:
      * ``"confirmed"`` — an ack is present (no scrape needed);
      * ``"failed"``    — the draft is still stuck in the composer with a
        swallowed Enter (the submit did NOT clear the input);
      * ``"likely"``    — an engine-specific capture signal shows the target TUI
        accepted the prompt even though no hook ack arrived;
      * ``"unverified"`` — bytes may have been issued, but neither ack nor
        engine-specific ingestion proof appeared.
    """
    if ack:
        return "confirmed", "", "", "UserPromptSubmit"
    capture = ""
    try:
        capture = _capture_pane_text(control, phys_pane, lines=20)
    except Exception as exc:
        log.debug("tmuxctld: submit-advisory capture failed pane=%s: %s", phys_pane, exc)
        capture = ""
    agent = "auto"
    try:
        agent = resolve_agent_for_pane(control.adapter, phys_pane, "auto", default="auto")
    except Exception as exc:
        log.debug("tmuxctld: submit-advisory agent resolution failed pane=%s: %s", phys_pane, exc)
        agent = "auto"

    if agent == "codex" and _detect_codex_user_message(capture, text):
        return (
            "likely",
            "codex prompt appears in the transcript as an accepted user message, "
            "but no UserPromptSubmit ack arrived",
            capture[-500:],
            "capture-pane:codex-user-message",
        )

    if _detect_swallowed_submit(capture, text):
        return (
            "failed",
            "submit did not clear the composer — the draft is still present with a "
            "swallowed Enter; the message was NOT delivered",
            capture[-500:],
            None,
        )
    return (
        "unverified",
        "send issued but not confirmed — no UserPromptSubmit ack or engine-specific "
        "ingestion signal was observed; do not blind-retry",
        capture[-500:],
        None,
    )


def _hold_agent_guard(phys_pane: str, *, seconds: int) -> str | None:
    """Acquire an AGENT guard and return the owner token (or ``None``).

    The owner token MUST be threaded through ``thread_local_override(owner=...)``
    and back into ``_release_agent_guard(owner=...)`` so ``send_gate.evaluate``'s
    agent-kind pierce branch (which requires ``thread_owner == pane_owner``) can
    pierce the guard we just installed, and so ``release`` can actually clear the
    AGENT record it wrote. Dropping the token silently breaks self-pierce and
    leaves the guard lingering until TTL expiry.
    """
    try:
        return typing_guard_state.hold(
            typing_guard_state.Tmux(typing_guard_state.tmux_binary()),
            phys_pane,
            seconds=seconds,
            now=typing_guard_state.now_epoch(),
        )
    except Exception as exc:  # tmux unreachable / no live server (e.g. unit tests)
        log.debug("tmuxctld: agent-guard hold skipped pane=%s: %s", phys_pane, exc)
        return None


def _release_agent_guard(phys_pane: str, *, owner: str | None = None) -> None:
    try:
        typing_guard_state.release(
            typing_guard_state.Tmux(typing_guard_state.tmux_binary()),
            phys_pane,
            now=typing_guard_state.now_epoch(),
            owner=owner,
        )
    except Exception as exc:
        log.warning("tmuxctld: agent-guard release failed pane=%s: %s", phys_pane, exc)


def _insert_without_submit_pipeline(
    control,
    *,
    pane: str,
    text: str,
    action: Callable[[], dict | None],
    route: str = "/insert-text",
    request_params: dict | None = None,
    operation_id: str = "",
    verify_timeout: float = 1.0,
    direct_user: bool = False,
    effects: dict | None = None,
    occupancy_checked: bool = False,
) -> dict:
    """Shared insert-only send primitive with gate, idempotency, and read-back."""

    phys_pane = _resolve_physical_pane_or_gate(control, pane)
    normalized_payload = normalize_prompt_payload(text)
    from .occupancy import (
        assert_comms_delivery_target_occupied,
        assert_dispatch_target_available,
        looks_like_dispatch_launcher_payload,
    )

    # Occupancy gate is independent of the typing gate and idempotency cache:
    # every byte-bearing insert must re-prove the target is either a free
    # dispatch allocation pane or an occupied managed-agent comms target.
    if not occupancy_checked:
        if looks_like_dispatch_launcher_payload(normalized_payload):
            assert_dispatch_target_available(control.adapter, phys_pane)
        else:
            assert_comms_delivery_target_occupied(control.adapter, phys_pane)
    visible_payload_hash = prompt_payload_hash(normalized_payload)
    idempotency_hash = _send_operation_fingerprint(
        "insert-without-submit",
        text=normalized_payload,
        effects={**(effects or {}), "direct_user": bool(direct_user)},
    )
    idempotent = _SEND_IDEMPOTENCY.begin(
        operation_id, pane=phys_pane, payload_hash=idempotency_hash
    )
    if idempotent is not None:
        return idempotent

    queue_params = dict(
        request_params or {"pane": pane, "text": text, "operation_id": operation_id}
    )
    deferred = _defer_or_drop_typing_guard(
        route=route, params=queue_params, pane=pane, phys_pane=phys_pane
    )
    if deferred is not None:
        _SEND_IDEMPOTENCY.abort(operation_id)
        return deferred
    if not direct_user:
        pre_gate = send_gate.evaluate(
            ("send-keys", "-t", phys_pane, "-l", normalized_payload),
            drop_reason=None,
        )
        if pre_gate is not None and pre_gate.get("suppressed"):
            deferred = _defer_or_drop_typing_guard(
                route=route,
                params=queue_params,
                pane=pane,
                phys_pane=phys_pane,
                gate=pre_gate,
            )
            _SEND_IDEMPOTENCY.abort(operation_id)
            if deferred is not None:
                return deferred
            raise TmuxSendGated({**pre_gate, "policy": "cancel", "deferred": True})

    dispatch_id = str(uuid.uuid4())
    instance_id = ""
    try:
        instance_id = str(control.instance_id_for_pane(pane).get("instance_id") or "").strip()
    except Exception:
        instance_id = ""

    hold_seconds = max(8, int(max(0.0, verify_timeout)) + 3)
    owner_token = _hold_agent_guard(phys_pane, seconds=hold_seconds)
    held = bool(owner_token)
    override_reason = "tmuxctld-direct-user" if direct_user else "tmuxctld-send-holder"
    # Thread the owner token so send_gate can pierce our OWN AGENT guard. Without
    # owner=, sanctioned_agent_owner() never matches the pane owner installed by
    # hold(), and every literal send inside action() would be suppressed.
    override_ctx = (
        thread_local_override(override_reason, owner=owner_token)
        if held
        else contextlib.nullcontext()
    )
    action_result: dict = {}
    send_exception: BaseException | None = None
    try:
        with override_ctx:
            maybe_result = action()
            if isinstance(maybe_result, dict):
                action_result = dict(maybe_result)
        gate = getattr(control.adapter, "last_send_gate_result", None)
        if gate and gate.get("suppressed"):
            raise TmuxSendGated(gate)
    except BaseException as exc:
        send_exception = exc
        raise
    finally:
        if held:
            _release_agent_guard(phys_pane, owner=owner_token)
        if send_exception is not None:
            _SEND_IDEMPOTENCY.abort(operation_id)

    confirmed, capture = _wait_for_insert_confirmation(
        control, phys_pane=phys_pane, text=normalized_payload, timeout=verify_timeout
    )
    failures = []
    if not confirmed:
        failures.append(
            {
                "type": "insert_unverified",
                "detail": "inserted bytes were not found in capture-pane read-back",
            }
        )
    result = {
        **action_result,
        "status": "inserted" if confirmed else "unverified",
        "pane": pane,
        "instance_id": action_result.get("instance_id") or instance_id,
        "dispatch_id": dispatch_id,
        "operation_id": operation_id or None,
        "payload_hash": visible_payload_hash,
        "verification_status": "inserted" if confirmed else "unverified",
        "verified_by": "capture-pane" if confirmed else None,
        "idempotent_replay": False,
        "guard_held": held,
        "submitted": False,
        "insert_confirmed": confirmed,
        "capture_excerpt": capture[-500:] if capture and not confirmed else "",
        "failures": failures,
    }
    _SEND_IDEMPOTENCY.finish(
        operation_id, pane=phys_pane, payload_hash=idempotency_hash, result=result
    )
    return result


def _send_text_pipeline(
    control,
    *,
    pane: str,
    text: str,
    route: str = "/send-text",
    request_params: dict | None = None,
    submit: bool = True,
    clear_prompt: bool = False,
    verify: bool | None = None,
    verify_timeout: float = 5.0,
    submit_settle_seconds: float = 1.0,
    ack_submit_retries: int = 2,
    operation_id: str = "",
    pre_submit_keys: tuple[str, ...] = (),
    post_submit_actions: tuple[dict, ...] = (),
    hook_echo_pane: str = "",
    correlation_id: str = "",
) -> dict:
    if verify is None:
        verify = submit
    # Resolve the physical pane id once for all guard + capture ops. tmux pane
    # options (@TYPING_*_UNTIL) and capture-pane key off the real %NN; the
    # caller-supplied id may be a canonical page:id. _resolve is a no-op on a raw
    # %NN, so it is safe either way and tolerant of resolver failure. The same
    # resolution feeds the inviolable human-lock fail-closed: a live keystroke /
    # pending lock gates the send NOW (zero bytes), immune to any ambient
    # TMUX_SEND_GATE_ALLOW enforce-action override, and before we acquire our own
    # AGENT hold over a pane the Emperor is typing into.
    phys_pane = _resolve_physical_pane_or_gate(control, pane)

    # Fail closed before ANY byte-bearing send, including insert-only calls. If a
    # human/pending/other agent guard is already live, do not enter send_gate's
    # default delay path: that holds the HTTP request until caller timeouts and
    # can later release a stale send onto active typing. Surface a structured
    # gated result instead; Token-API can queue/retry, but tmuxctld issues zero
    # bytes now.
    normalized_payload = normalize_prompt_payload(text)
    from .occupancy import (
        assert_comms_delivery_target_occupied,
        assert_dispatch_target_available,
        looks_like_dispatch_launcher_payload,
    )

    # Ledger-first occupancy gate before idempotency, queues, typing guard, or
    # any byte-bearing send. Dispatch launcher bytes require a ledger-free pane;
    # all other comms require an occupied managed-agent pane. In both cases the
    # selected pane gets exactly one live-process sniff and disagreement is P0.
    if looks_like_dispatch_launcher_payload(normalized_payload):
        assert_dispatch_target_available(control.adapter, phys_pane)
    else:
        assert_comms_delivery_target_occupied(control.adapter, phys_pane)

    if not submit:
        return _insert_without_submit_pipeline(
            control,
            pane=pane,
            text=normalized_payload,
            action=lambda: (
                control.insert_text(phys_pane, normalized_payload)
                or {"pane": pane, "physical_pane": phys_pane}
            ),
            route=route,
            request_params=request_params,
            operation_id=operation_id,
            verify_timeout=verify_timeout,
            effects={"route": "send-text", "submit": False, "clear_prompt": bool(clear_prompt)},
            occupancy_checked=True,
        )

    # Hash the NORMALIZED payload that is actually injected (newlines collapsed,
    # rstripped) — not the raw text. The UserPromptSubmit ack hashes the prompt
    # the agent received (post-normalization; cf. agent-cmd's payload_hash), so
    # hashing raw multiline text here would never match and force a false
    # `unverified` + needless recovery. Check explicit operation-id replay before
    # mutable gates so a safe retry reports the prior result instead of being
    # reclassified by current human-lock state.
    payload_hash = prompt_payload_hash(normalized_payload)
    idempotency_hash = _send_operation_fingerprint(
        "send-text",
        text=normalized_payload,
        effects={
            "submit": True,
            "clear_prompt": bool(clear_prompt),
            "pre_submit_keys": list(pre_submit_keys),
            "post_submit_actions": list(post_submit_actions),
        },
    )
    idempotent = _SEND_IDEMPOTENCY.begin(
        operation_id, pane=phys_pane, payload_hash=idempotency_hash
    )
    if idempotent is not None:
        return idempotent

    queue_params = dict(
        request_params or {"pane": pane, "text": text, "operation_id": operation_id}
    )
    deferred = _defer_or_drop_typing_guard(
        route=route, params=queue_params, pane=pane, phys_pane=phys_pane
    )
    if deferred is not None:
        _SEND_IDEMPOTENCY.abort(operation_id)
        return deferred
    pre_gate = send_gate.evaluate(("send-keys", "-t", phys_pane, "-l", normalized_payload))
    if pre_gate is not None and pre_gate.get("suppressed"):
        _SEND_IDEMPOTENCY.abort(operation_id)
        deferred = _defer_or_drop_typing_guard(
            route=route, params=queue_params, pane=pane, phys_pane=phys_pane, gate=pre_gate
        )
        if deferred is not None:
            return deferred
        raise TmuxSendGated({**pre_gate, "policy": "cancel", "deferred": True})
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
    guard_owner = None
    try:
        guard_owner = typing_guard_state.hold(
            typing_guard_state.Tmux(typing_guard_state.tmux_binary()),
            phys_pane,
            seconds=hold_seconds,
            now=typing_guard_state.now_epoch(),
        )
        held = bool(guard_owner)
    except Exception as exc:  # tmux unreachable / no live server (e.g. unit tests)
        log.debug("tmuxctld: agent-guard hold skipped pane=%s: %s", phys_pane, exc)
        held = False
        guard_owner = None

    # When we hold, pierce our OWN agent lock so the send we just guarded is not
    # delayed by the green state we set — thread-locally, so a concurrent send to
    # another pane on another worker thread is never granted the pierce (and so
    # never stomps a human lock there). When the hold was DENIED (a live human
    # on/pending lock), we do NOT pierce: the send routes through the normal gate
    # and delays behind the human, never stomping the Emperor's keystrokes.
    override_ctx = (
        thread_local_override("tmuxctld-send-holder", owner=guard_owner)
        if held
        else contextlib.nullcontext()
    )

    def _send_submit_key() -> None:
        if hasattr(control.adapter, "send_keys"):
            control.adapter.send_keys(phys_pane, "C-m")
        else:
            control.adapter.run("send-keys", "-t", phys_pane, "C-m")

    def _send_literal(value: str) -> None:
        control.adapter.run("send-keys", "-t", phys_pane, "-l", value)

    def _send_key(value: str) -> None:
        if hasattr(control.adapter, "send_keys"):
            control.adapter.send_keys(phys_pane, value)
        else:
            control.adapter.run("send-keys", "-t", phys_pane, value)

    def _run_post_submit_actions() -> None:
        for action in post_submit_actions:
            kind = str(action.get("type") or "").strip()
            if kind == "key":
                _send_key(str(action.get("key") or ""))
            elif kind == "literal_submit":
                _send_literal(str(action.get("text") or ""))
                _send_submit_key()
            else:
                raise ValueError(f"unknown post-submit action: {kind!r}")
            settle = float(action.get("settle_seconds", submit_settle_seconds) or 0)
            if settle > 0:
                time.sleep(settle)

    started = time.monotonic()
    ack = None
    swallowed_submit_detected = False
    recovery_attempts = 0
    failures: list[dict] = []
    recovery_submit_credited = False
    send_exception: BaseException | None = None
    try:
        with override_ctx:
            if hasattr(control.adapter, "send_text_then_submit"):
                control.adapter.send_text_then_submit(
                    phys_pane,
                    text,
                    clear_prompt=clear_prompt,
                    pre_submit_keys=pre_submit_keys,
                    submit_settle_seconds=submit_settle_seconds,
                )
            else:
                normalized = normalized_payload
                if clear_prompt:
                    control.adapter.send_keys(phys_pane, "C-u")
                control.adapter.run("send-keys", "-t", phys_pane, "-l", normalized)
                gate = getattr(control.adapter, "last_send_gate_result", None)
                if gate and gate.get("suppressed"):
                    raise TmuxSendGated(gate)
                # Test-adapter fallback. Real daemon sends use TmuxAdapter's
                # canonical method above; callers must not assemble send-keys
                # outside tmuxctld.
                if submit_settle_seconds > 0:
                    time.sleep(submit_settle_seconds)
                for key in pre_submit_keys:
                    control.adapter.send_keys(phys_pane, key)
                if pre_submit_keys and submit_settle_seconds > 0:
                    time.sleep(submit_settle_seconds)
                control.adapter.send_keys(phys_pane, "C-m")
                if submit_settle_seconds > 0:
                    time.sleep(submit_settle_seconds)
                control.adapter.send_keys(phys_pane, "C-m")

            if post_submit_actions:
                _run_post_submit_actions()

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
                    if swallowed_submit_detected:
                        try:
                            post_recovery_capture = control.adapter.capture_pane(
                                phys_pane, lines=12
                            )
                        except Exception as exc:
                            log.debug(
                                "tmuxctld: post-recovery capture-pane failed pane=%s: %s",
                                phys_pane,
                                exc,
                            )
                            post_recovery_capture = capture
                        if post_recovery_capture.strip() and not _detect_swallowed_submit(
                            post_recovery_capture, text
                        ):
                            ack = {
                                "event": "UserPromptSubmit",
                                "instance_id": instance_id,
                                "pane": phys_pane,
                                "prompt_hash": payload_hash,
                                "at": time.monotonic(),
                                "recovered": True,
                            }
                            recovery_submit_credited = True
                            break
    except BaseException as exc:
        send_exception = exc
        raise
    finally:
        # The guard must never leak green past the handshake. Release only the
        # owner token this request acquired; never clear a concurrent guard.
        if held:
            try:
                typing_guard_state.release(
                    typing_guard_state.Tmux(typing_guard_state.tmux_binary()),
                    phys_pane,
                    now=typing_guard_state.now_epoch(),
                    owner=guard_owner,
                )
            except Exception as exc:
                log.warning("tmuxctld: agent-guard release failed pane=%s: %s", phys_pane, exc)
        if send_exception is not None:
            _SEND_IDEMPOTENCY.abort(operation_id)

    # Level-1 delivery contract: if tmuxctld reached this point without a gate or
    # send exception, bytes were issued to the target pane. That is delivery
    # success for the send call. UserPromptSubmit is only the level-2 "the target
    # took a turn" fact; lack of that hook is pending, not non-delivery.
    verification_status = "submitted" if ack else ("pending" if verify else "not_requested")
    status = "submitted" if ack else "delivered"
    turn = "submitted" if ack else ("pending" if verify else "not_requested")
    # Advisory submit classification for the no-ack case. The classifier may
    # find a stuck composer or engine-specific ingestion signal, but it must not
    # demote level-1 byte delivery to failure.
    submit_delivery, advisory, capture_excerpt, delivery_verified_by = _classify_submit_delivery(
        control, phys_pane=phys_pane, text=text, ack=ack
    )
    if submit_delivery == "failed":
        failures.append({"type": "submit_not_cleared", "detail": advisory})
    hook_echo = None
    if verify and not ack:
        callback_pane = str(hook_echo_pane or "").strip()
        if callback_pane:
            try:
                callback_pane = _resolve_physical_pane_or_gate(control, callback_pane)
            except Exception as exc:  # noqa: BLE001
                log.debug("tmuxctld: hook-echo caller resolution skipped: %s", exc)
        hook_echo = _PROMPT_SUBMIT_SNIFFER.register_callback(
            correlation_id=correlation_id or operation_id or dispatch_id,
            caller_pane=callback_pane,
            target_pane=phys_pane,
            target_label=pane,
            instance_id=instance_id,
            payload_hash=payload_hash,
            since=started,
        )
    result = {
        "status": status,
        "pane": pane,
        "instance_id": instance_id,
        "dispatch_id": dispatch_id,
        "operation_id": operation_id or None,
        "payload_hash": payload_hash,
        "verification_status": verification_status,
        "verified_by": "UserPromptSubmit" if ack else delivery_verified_by,
        "ack": ack,
        "delivered": True,
        "submitted": bool(ack),
        "turn": turn,
        "delivery": "confirmed" if ack else "delivered",
        "submit_delivery": submit_delivery,
        "advisory": advisory or None,
        "capture_excerpt": capture_excerpt,
        "correlation_id": correlation_id or operation_id or dispatch_id,
        "hook_echo": hook_echo,
        "recovery_submit_credited": recovery_submit_credited,
        "idempotent_replay": False,
        "guard_held": held,
        "guard_owner": guard_owner,
        "swallowed_submit_detected": swallowed_submit_detected,
        "recovery_attempts": recovery_attempts,
        "failures": failures,
    }
    _SEND_IDEMPOTENCY.finish(
        operation_id, pane=phys_pane, payload_hash=idempotency_hash, result=result
    )
    return result


def _h_send_text(control, params):
    return _send_text_pipeline(
        control,
        pane=_s(params, "pane"),
        text=_s(params, "text"),
        route="/send-text",
        request_params=params,
        submit=_b(params, "submit", True),
        clear_prompt=_b(params, "clear_prompt"),
        verify=_b(params, "verify", _b(params, "submit", True)),
        verify_timeout=_f(params, "verify_timeout", 5.0),
        submit_settle_seconds=_f(params, "submit_settle_seconds", 1.0),
        ack_submit_retries=_i(params, "ack_submit_retries", 2),
        operation_id=_s(params, "operation_id"),
        pre_submit_keys=_parse_pre_submit_keys(params.get("pre_submit_keys", ())),
        hook_echo_pane=_s(params, "hook_echo_pane") or _s(params, "caller_pane"),
        correlation_id=_s(params, "correlation_id"),
    )


def _h_insert_text(control, params):
    pane = _s(params, "pane")
    text = _s(params, "text")
    return _insert_without_submit_pipeline(
        control,
        pane=pane,
        text=text,
        action=lambda: control.insert_text(pane, text) or {"pane": pane},
        route="/insert-text",
        request_params=params,
        operation_id=_s(params, "operation_id"),
        verify_timeout=_f(params, "verify_timeout", 1.0),
        effects={"route": "insert-text"},
    )


def _h_prompt_start(control, params):
    pane = _s(params, "pane")
    phys_pane = _resolve_physical_pane_or_gate(control, pane)
    deferred = _defer_or_drop_typing_guard(
        route="/prompt-start", params=params, pane=pane, phys_pane=phys_pane
    )
    if deferred is not None:
        return deferred
    with _agent_guard_transaction(control, pane):
        control.move_to_prompt_start(pane, page_ups=_i(params, "page_ups", 50))
    return {"pane": pane, "status": "prompt-start"}


def _h_prompt_end(control, params):
    pane = _s(params, "pane")
    phys_pane = _resolve_physical_pane_or_gate(control, pane)
    deferred = _defer_or_drop_typing_guard(
        route="/prompt-end", params=params, pane=pane, phys_pane=phys_pane
    )
    if deferred is not None:
        return deferred
    with _agent_guard_transaction(control, pane):
        control.move_to_prompt_end(pane, page_downs=_i(params, "page_downs", 50))
    return {"pane": pane, "status": "prompt-end"}


def _h_invoke_skill(control, params):
    instance_id = _s(params, "instance_id")
    pane = _s(params, "pane", "current")
    if instance_id:
        resolved = control.resolve_instance(instance_id)
        if not resolved["found"]:
            return {"instance_id": instance_id, "found": False}
        pane = resolved["pane_id"]
    skill = _s(params, "name") or _s(params, "skill")
    agent = _s(params, "agent", "auto")
    kind = _s(params, "kind", "skill")
    arguments = _s(params, "arguments") or None
    if _b(params, "submit"):
        resolved_kind = normalize_invocation_kind(kind)
        if resolved_kind == "command":
            resolved_agent = "auto"
        else:
            resolved_agent = resolve_agent_for_pane(control.adapter, pane, agent)
        rendered = invocation_text(
            skill,
            resolved_agent,
            kind=resolved_kind,
            arguments=arguments,
        )
        result = _send_text_pipeline(
            control,
            pane=pane,
            text=rendered,
            route="/invoke-skill",
            request_params=params,
            submit=True,
            clear_prompt=_b(params, "clear_prompt"),
            verify=_b(params, "verify", True),
            verify_timeout=_f(params, "verify_timeout", 5.0),
            submit_settle_seconds=_f(params, "submit_settle_seconds", 1.0),
            ack_submit_retries=_i(params, "ack_submit_retries", 2),
            pre_submit_keys=invocation_sink_keys(resolved_agent, kind=resolved_kind),
        )
        return {
            **result,
            "submitted": False if result.get("queued") or result.get("dropped") else True,
            "kind": resolved_kind,
            "agent": resolved_agent,
            "rendered": rendered,
        }
    result = _h_insert_invocation(
        control,
        {
            "pane": pane,
            "name": skill,
            "agent": agent,
            "kind": kind,
            "arguments": arguments or "",
            "operation_id": _s(params, "operation_id"),
            "verify_timeout": _s(params, "verify_timeout", "1.0"),
        },
    )
    return {"submitted": False, **result}


def _resolve_target_pane(control, params) -> tuple[str, str]:
    instance_id = _s(params, "instance_id")
    pane = _s(params, "pane", "current")
    if instance_id and (not pane or pane == "current"):
        resolved = control.resolve_instance(instance_id)
        if not resolved["found"]:
            raise ValueError(f"instance not found: {instance_id}")
        pane = resolved["pane_id"]
    return pane, instance_id


def _h_send_ethereal(control, params):
    pane, instance_id = _resolve_target_pane(control, params)
    requested_agent = _s(params, "agent", "auto")
    resolved_agent = resolve_agent_for_pane(control.adapter, pane, requested_agent, default="auto")
    message = _s(params, "message") or _s(params, "text")
    rendered = ethereal_invocation_text(resolved_agent, message)
    if resolved_agent == "claude":
        post_actions = (
            {"type": "key", "key": "c"},
            {"type": "key", "key": "C-c"},
        )
    elif resolved_agent == "codex":
        post_actions = (
            {"type": "literal_submit", "text": "/copy"},
            {"type": "key", "key": "C-c"},
        )
    else:  # ethereal_invocation_text should already fail closed; keep explicit.
        raise ValueError("ethereal send requires claude or codex")
    result = _send_text_pipeline(
        control,
        pane=pane,
        text=rendered,
        route="/send-ethereal",
        request_params=params,
        submit=True,
        clear_prompt=_b(params, "clear_prompt"),
        verify=_b(params, "verify", True),
        verify_timeout=_f(params, "verify_timeout", 5.0),
        submit_settle_seconds=_f(params, "submit_settle_seconds", 1.0),
        ack_submit_retries=_i(params, "ack_submit_retries", 2),
        post_submit_actions=post_actions,
    )
    return {
        **result,
        "submitted": False if result.get("queued") or result.get("dropped") else True,
        "kind": "ethereal",
        "agent": resolved_agent,
        "rendered": rendered,
        "message": message,
        "instance_id": result.get("instance_id") or instance_id,
    }


def _h_append_user_text(control, params):
    pane, instance_id = _resolve_target_pane(control, params)
    text = _s(params, "text")
    if not text:
        raise ValueError("direct-user text is empty")
    result = _insert_without_submit_pipeline(
        control,
        pane=pane,
        text=text,
        action=lambda: control.adapter.run("send-keys", "-t", pane, "-l", text) or {"pane": pane},
        route="/append-user-text",
        request_params=params,
        operation_id=_s(params, "operation_id"),
        verify_timeout=_f(params, "verify_timeout", 1.0),
        direct_user=True,
        effects={"route": "append-user-text"},
    )
    return {
        **result,
        "instance_id": result.get("instance_id") or instance_id,
        "direct_user": True,
        "submitted": False,
        "clear_prompt": False,
    }


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
    resolved_kind = normalize_invocation_kind(kind)
    if resolved_kind == "command":
        resolved_agent = "auto"
    else:
        resolved_agent = resolve_agent_for_pane(control.adapter, pane, agent)
    rendered = invocation_text(
        name,
        resolved_agent,
        kind=resolved_kind,
        arguments=arguments,
    )

    def _action() -> dict:
        return control.insert_invocation(
            pane, name, agent=resolved_agent, kind=resolved_kind, arguments=arguments
        )

    result = _insert_without_submit_pipeline(
        control,
        pane=pane,
        text=rendered,
        action=_action,
        route="/insert-invocation",
        request_params=params,
        operation_id=_s(params, "operation_id"),
        verify_timeout=_f(params, "verify_timeout", 1.0),
        effects={
            "route": "insert-invocation",
            "kind": resolved_kind,
            "agent": resolved_agent,
            "arguments": arguments or "",
            "sink_keys": list(invocation_sink_keys(resolved_agent, kind=resolved_kind)),
        },
    )
    return {"status": result.get("status", "inserted"), **result}


def _h_assert_instance(control, params):
    return control.assert_instance(_s(params, "pane"))


def _h_reconcile(control, params):
    # The detached daemon has no ambient tmux session; the fleet lives in `main`.
    return {
        "ledger": control.ledger_reconcile(),
        "results": control.reconcile_personas(session=_s(params, "session", "main")),
    }


def _h_event(control, params):
    if (_s(params, "event").strip().lower().replace("_", "-") == "pane-died") and _s(
        params, "pane"
    ):
        try:
            pane_label = _adapter_show_pane_option(control, _s(params, "pane"), "@PANE_ID")
            if pane_label:
                row = control.ledger_resolve(pane_positional_id=pane_label).get("row")
                if row and row.get("wrapper_id"):
                    control.ledger_close(row["wrapper_id"])
        except Exception:
            log.exception("tmuxctld ledger close on pane-died failed")
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


def _h_hook_user_prompt_submit(control, params):
    event = _PROMPT_SUBMIT_SNIFFER.record(params)
    echoes = [
        _emit_prompt_submit_callback(control, callback, event)
        for callback in _PROMPT_SUBMIT_SNIFFER.pop_matching_callbacks(event)
    ]
    return {**event, "hook_echoes": echoes}


def _h_clear_runtime(control, params):
    return control.clear_runtime(_s(params, "pane"))


def _h_ledger_upsert(control, params):
    env = params.get("env") if isinstance(params.get("env"), dict) else {}
    return control.ledger_upsert(
        wrapper_id=_s(params, "wrapper_id")
        or _s(params, "wrapper_launch_id")
        or _s(env, "TOKEN_API_WRAPPER_ID")
        or _s(env, "TOKEN_API_WRAPPER_LAUNCH_ID"),
        instance_id=_s(params, "instance_id"),
        persona=_s(params, "persona"),
        pane_positional_id=_s(params, "pane_positional_id")
        or _s(params, "pane_label")
        or _s(params, "pane_id"),
        engine=_s(params, "engine"),
        working_dir=_s(params, "working_dir") or _s(params, "cwd"),
        born_epoch=params.get("born_epoch"),
        state=_s(params, "state", "OPEN"),
    )


def _h_ledger_resolve(control, params):
    env = params.get("env") if isinstance(params.get("env"), dict) else {}
    return control.ledger_resolve(
        _s(params, "id") or _s(params, "value"),
        wrapper_id=_s(params, "wrapper_id")
        or _s(params, "wrapper_launch_id")
        or _s(env, "TOKEN_API_WRAPPER_ID")
        or _s(env, "TOKEN_API_WRAPPER_LAUNCH_ID"),
        instance_id=_s(params, "instance_id"),
        pane_positional_id=_s(params, "pane_positional_id")
        or _s(params, "pane_label")
        or _s(params, "pane_id"),
        include_closed=_b(params, "include_closed"),
    )


def _h_ledger_rows(control, params):
    from .wrapper_ledger import LEDGER

    return {
        "path": str(LEDGER.path),
        "rows": control.ledger_rows(include_closed=_b(params, "include_closed", True)),
    }


_WRAPPEREND_LIST_SEP = "__TMUXCTLD_WRAPPEREND_FIELD__"


def _wrapper_id_from_params(params: dict) -> str:
    env = params.get("env") if isinstance(params.get("env"), dict) else {}
    return (
        _s(params, "wrapper_id")
        or _s(params, "wrapper_launch_id")
        or _s(env, "TOKEN_API_WRAPPER_ID")
        or _s(env, "TOKEN_API_WRAPPER_LAUNCH_ID")
    )


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
        _WRAPPEREND_LIST_SEP.join(
            ["#{pane_id}", "#{@TOKEN_API_WRAPPER_ID}", "#{@TOKEN_API_WRAPPER_LAUNCH_ID}"]
        ),
        allow_failure=True,
    )
    for line in raw.splitlines():
        if not line:
            continue
        pane_id, owner, legacy_owner = (line.split(_WRAPPEREND_LIST_SEP, 2) + ["", ""])[:3]
        if wrapper_launch_id in {owner.strip(), legacy_owner.strip()} and pane_id.strip():
            return pane_id.strip()
    return ""


def _fetch_instance_for_wrapperend(instance_id: str) -> dict:
    """Best-effort Token-API read for PR/worktree state.

    Token-API owns instance registry truth.  WrapperEnd uses it only to decide
    whether deferred worktree teardown is safe; any fetch failure preserves.
    """
    if not instance_id:
        return {}
    base = os.environ.get("TOKEN_API_URL", "http://localhost:7777").rstrip("/")
    request = urllib.request.Request(f"{base}/api/instances/{instance_id}", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
            return payload if isinstance(payload, dict) else {}
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        log.warning(
            "tmuxctld wrapperend instance fetch failed instance=%s: %s",
            instance_id,
            exc,
        )
        return {}


def _wrapperend_worktree_path(
    params: dict,
    env: dict,
    *,
    instance: dict,
    ledger_row: dict | None,
    pane_cwd: str,
) -> str:
    """Resolve the candidate worktree path; caller preserves if this is wrong."""

    for value in (
        instance.get("working_dir") if isinstance(instance, dict) else "",
        pane_cwd,
        (ledger_row or {}).get("working_dir"),
        _s(params, "cwd"),
        _s(params, "working_dir"),
        _s(env, "TOKEN_API_TARGET_WORKING_DIR"),
        _s(env, "TOKEN_API_CWD"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _h_hook_wrapperend(control, params):
    """Authoritative wrapper-owned visual/runtime cleanup for tmux panes.

    Token-API owns process/session lifecycle. tmuxctld owns pane-local visual
    state, so WrapperEnd clears only the pane whose @TOKEN_API_WRAPPER_LAUNCH_ID
    matches the exiting wrapper. Missing/already-cleared panes are successful
    no-ops; a pane owned by a different wrapper is surfaced as an error.

    WrapperEnd also owns deferred worktree teardown.  A worktree cleanup is a
    shutdown request, so merge commands must never run it from inside their own
    cwd.  Here the wrapped process is already gone; even so, the cleanup removes
    only the linked worktree and preserves unmerged or dirty state.
    """
    env = params.get("env") if isinstance(params.get("env"), dict) else {}
    wrapper_launch_id = _wrapper_id_from_params(params)
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

    owner = _adapter_show_pane_option(control, pane, "@TOKEN_API_WRAPPER_ID")
    legacy_owner = _adapter_show_pane_option(control, pane, "@TOKEN_API_WRAPPER_LAUNCH_ID")
    owner = owner or legacy_owner
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

    pane_label = _adapter_show_pane_option(control, pane, "@PANE_ID")
    instance_id = _adapter_show_pane_option(control, pane, "@INSTANCE_ID")
    pane_cwd = _adapter_show_pane_option(control, pane, "@TOKEN_API_CWD")
    ledger_close = control.ledger_close(wrapper_launch_id)
    ledger_row = ledger_close.get("row") if isinstance(ledger_close, dict) else None
    if not instance_id and isinstance(ledger_row, dict):
        instance_id = str(ledger_row.get("instance_id") or "").strip()
    instance = _fetch_instance_for_wrapperend(instance_id)
    worktree_path = _wrapperend_worktree_path(
        params,
        env,
        instance=instance,
        ledger_row=ledger_row if isinstance(ledger_row, dict) else None,
        pane_cwd=pane_cwd,
    )
    try:
        from .worktree_lifecycle import cleanup_worktree_on_wrapper_end

        worktree_cleanup = cleanup_worktree_on_wrapper_end(worktree_path, instance=instance)
    except Exception as exc:  # preserve worktree; WrapperEnd pane cleanup still proceeds
        log.exception("tmuxctld wrapperend worktree cleanup failed path=%s", worktree_path)
        worktree_cleanup = {
            "status": "preserved",
            "reason": "exception",
            "worktree": worktree_path,
            "error": str(exc),
        }
    window_name = control.adapter.run(
        "display-message", "-t", pane, "-p", "#{window_name}", allow_failure=True
    ).strip()
    # Class-gated teardown — the SAME unified router the pane-died hook uses. A
    # pre-allocated palace/somnium SLOT is cleared IN PLACE and preserved (the
    # morning over-reap culled such a slot); only a dynamically-created WORKER is
    # culled. This scrubs the runtime exactly once (inside the router).
    teardown = control.teardown_pane(
        pane, pane_label=pane_label, window_name=window_name, source="wrapperend"
    )
    reap = teardown.get("result") if isinstance(teardown, dict) else None
    return {
        "status": "cleared",
        "wrapper_launch_id": wrapper_launch_id,
        "pane": (reap or {}).get("pane", pane),
        "pane_label": pane_label,
        "instance_id": instance_id,
        "ledger": ledger_close,
        "worktree_cleanup": worktree_cleanup,
        "reap": reap,
        "teardown": teardown,
    }


def _h_worktree_teardown(control, params):
    """On-demand worktree teardown — the tmuxctld single-executor route.

    The SAME universal sanitization gate + gated remote deletion the WrapperEnd
    path runs, exposed as an on-demand route so the manual CLI and the
    Golden-Throne victory cascade route through one executor instead of their own
    divergent `worktree-delete -f`.  token-api stays the merge-proof authority: an
    ``instance_id`` fetches ``pr_state`` (as WrapperEnd does), or ``pr_state`` is
    passed directly.  Teardown is pure worktree lifecycle — this touches no tmux.
    """
    del control  # teardown is worktree-only; no tmux side effects
    env = params.get("env") if isinstance(params.get("env"), dict) else {}
    worktree = (
        _s(params, "worktree")
        or _s(params, "cwd")
        or _s(params, "working_dir")
        or _s(env, "TOKEN_API_TARGET_WORKING_DIR")
        or _s(env, "TOKEN_API_CWD")
    )
    if not worktree:
        raise ValueError("worktree path required")
    instance_id = _s(params, "instance_id")
    instance = _fetch_instance_for_wrapperend(instance_id) if instance_id else {}
    # An explicitly-supplied pr_state is the caller's assertion of current truth
    # and wins over a (possibly staler) fetched row when both are present.
    pr_state = _s(params, "pr_state")
    if pr_state:
        instance = {**(instance or {}), "pr_state": pr_state}
    delete_remote = _s(params, "delete_remote", "1").strip().lower() not in {"0", "false", "no"}

    from .worktree_lifecycle import teardown_worktree

    return teardown_worktree(worktree, instance=instance or None, delete_remote=delete_remote)


def _h_hook_wrapperstart(control, params):
    """Authoritative tmux-side wrapper registration at agent birth.

    The symmetric front-half of :func:`_h_hook_wrapperend`. Token-API owns the
    instance registry/session state; tmuxctld owns the wrapper's pane-local
    identity and ledger reconciliation. So at wrapper start the daemon:

    1. Stamps the wrapper-ownership id (``@TOKEN_API_WRAPPER_LAUNCH_ID``) so the
       later WrapperEnd can always find + clear its own pane, even if the wrapper's
       local stamp never landed.
    2. Derives the persona from the pane's durable ``@PANE_ID`` label and paints
       its tint immediately. This binds from the LABEL, not from ``@INSTANCE_ID``,
       so a singleton seat (Custodes / Fabricator-General) is never left tint-less
       in the window between WrapperEnd clearing the pane and the next
       wrapper/reconcile landing — the empty-stamp → no-tint root.

    Fail-open and idempotent: a missing pane is a successful no-op (the wrapper
    fires this best-effort; it must never block a launch).
    """
    from . import assertions

    env = params.get("env") if isinstance(params.get("env"), dict) else {}
    wrapper_launch_id = _wrapper_id_from_params(params)
    pane = _s(params, "tmux_pane") or _s(env, "TMUX_PANE")
    if not wrapper_launch_id:
        log.error("tmuxctld wrapperstart missing wrapper_launch_id pane=%s", pane)
        raise ValueError("wrapper_launch_id required")

    if pane and not _pane_exists_for_wrapperend(control, pane):
        pane = ""
    if not pane:
        pane = _find_pane_by_wrapper_id(control, wrapper_launch_id)
    if not pane:
        return {
            "status": "no_pane",
            "wrapper_launch_id": wrapper_launch_id,
            "pane": "",
            "tint": "",
        }

    current_owner = _adapter_show_pane_option(control, pane, "@TOKEN_API_WRAPPER_ID")
    legacy_owner = _adapter_show_pane_option(control, pane, "@TOKEN_API_WRAPPER_LAUNCH_ID")
    current_owner = current_owner or legacy_owner
    if current_owner != wrapper_launch_id:
        if current_owner:
            control.ledger_close(current_owner)
        # Reuse path: the next wrapper must never inherit statusline/persona/guard
        # stamps from the prior occupant.  This is the same daemon-owned runtime
        # cleanup pathway WrapperEnd uses; wrapperstart then stamps the new owner.
        # A duplicate WrapperStart from the SAME wrapper is idempotent and must not
        # scrub its own just-started runtime state.
        control.clear_runtime(pane)

    # (1) Daemon-authoritative wrapper-ownership stamp (idempotent).
    control.adapter.run(
        "set-option",
        "-p",
        "-t",
        pane,
        "@TOKEN_API_WRAPPER_ID",
        wrapper_launch_id,
        allow_failure=True,
    )
    # Keep the legacy pane option populated until every consumer has moved to
    # TOKEN_API_WRAPPER_ID. Reconcile and old wrapper cleanup paths still accept it.
    control.adapter.run(
        "set-option",
        "-p",
        "-t",
        pane,
        "@TOKEN_API_WRAPPER_LAUNCH_ID",
        wrapper_launch_id,
        allow_failure=True,
    )

    # (2) Persona tint from the durable pane label (no @INSTANCE_ID dependency).
    pane_label = _adapter_show_pane_option(control, pane, "@PANE_ID")
    engine = _s(params, "engine") or _s(env, "TOKEN_API_ENGINE")
    working_dir = _s(params, "cwd") or _s(params, "working_dir") or _s(env, "TOKEN_API_CWD")
    persona = _s(params, "persona") or _s(env, "TOKEN_API_PERSONA") or pane_label
    ledger_row = control.ledger_upsert(
        wrapper_id=wrapper_launch_id,
        persona=persona,
        pane_positional_id=pane_label,
        engine=engine,
        working_dir=working_dir,
        born_epoch=time.time(),
        state="OPEN",
    )
    tint = ""
    voice_lock = _adapter_show_pane_option(control, pane, _VOICE_LOCK_OPTION)
    if not voice_lock:
        try:
            tint = assertions.apply_persona_pane_tint(control.adapter, pane, pane_label) or ""
        except Exception as exc:  # never let a tint failure break wrapper registration
            log.warning(
                "tmuxctld wrapperstart tint failed pane=%s label=%s: %s", pane, pane_label, exc
            )

    return {
        "status": "stamped",
        "wrapper_launch_id": wrapper_launch_id,
        "pane": pane,
        "pane_label": pane_label,
        "ledger": ledger_row,
        "tint": tint,
    }


def _h_close_pane(control, params):
    # Resolve a canonical id (e.g. ``mechanicus:1``) to its physical ``%NN`` FIRST,
    # exactly as _h_pane_live does. close_pane's tmux probes only understand real
    # pane handles; handed a canonical id, ``display-message -t mechanicus:1`` misses
    # and close_pane reports a bogus ``already_closed`` while the pane is still alive
    # (the #314-class stale-handle failure, liveness.py). A caller — including the
    # husk reaper — trusting that ``already_closed`` would believe a pane was reaped
    # when it was not. ``current`` is left for close_pane's own resolution.
    pane = _s(params, "pane")
    if pane and pane != "current":
        pane = _resolve_physical_pane_or_gate(control, pane)
    return control.close_pane(pane, timeout=_f(params, "timeout", 3.0))


def _h_close(control, params):
    return control.close_instance(
        _s(params, "instance_id"),
        lifecycle=_s(params, "lifecycle", "retire"),
        mode=_s(params, "mode", "now"),
        pane=_s(params, "pane"),
        timeout=_f(params, "timeout", 3.0),
    )


# -- Workspace + stack (POST) ----------------------------------------------


def _h_pane_live(control, params):
    from .liveness import detect_pane_tui

    requested_pane = _s(params, "pane", "current")
    pane = requested_pane
    if pane == "current":
        pane = control.adapter.run("display-message", "-p", "#{pane_id}").strip()
    physical_pane = _resolve_physical_pane_or_gate(control, pane)
    tui = detect_pane_tui(control.adapter, physical_pane)
    try:
        public_pane = control.public_pane_id(physical_pane)
    except Exception:
        public_pane = requested_pane if not requested_pane.startswith("%") else physical_pane
    return {
        "pane_id": public_pane,
        "physical_pane_id": physical_pane,
        "pane_pid": tui.pane_pid,
        "agent_pid": tui.agent_pid,
        "agent_command": tui.agent_command,
        "live": tui.live,
    }


def _h_live_agents(control, params):
    from .dispatch_liveness import live_agents_in_dir

    matches = live_agents_in_dir(
        control.adapter,
        _s(params, "dir"),
        exclude_pane=_s(params, "exclude_pane") or None,
    )
    if _s(params, "format", "text") == "json":
        return [
            {
                "pane_id": control.public_pane_id(m.pane_id),
                "physical_pane_id": m.pane_id,
                "pane_pid": m.pane_pid,
                "agent_pid": m.agent_pid,
                "agent_command": m.agent_command,
                "cwd": m.cwd,
            }
            for m in matches
        ]
    return "\n".join(
        f"{control.public_pane_id(m.pane_id)}\t{m.pane_id}\t{m.agent_command or '?'}\t{m.cwd}"
        for m in matches
    )


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


def _h_grid_expand(control, params):
    action = _s(params, "action", "toggle")
    if _b(params, "expand"):
        action = "expand"
    if _b(params, "retract"):
        action = "retract"
    return control.grid_expand(
        pane=_s(params, "pane"),
        client=_s(params, "client"),
        expand=action == "expand",
        retract=action == "retract",
    )


def _h_mode_toggle(control, params):
    pane = _s(params, "pane", "current")
    target_pane = control._keybind_target_pane(pane, client=_s(params, "client"))
    phys_pane = _resolve_physical_pane_or_gate(control, target_pane)
    if not _b(params, "status"):
        deferred = _defer_or_drop_typing_guard(
            route="/mode-toggle", params=params, pane=target_pane, phys_pane=phys_pane
        )
        if deferred is not None:
            return {**deferred, "sent": False}
    capture = control.adapter.run(
        "capture-pane", "-t", target_pane, "-p", "-S", "-5", allow_failure=True
    )
    before = control.detect_mode_from_capture(capture)
    if _b(params, "status"):
        return {"pane": target_pane, "mode": before, "presses": 0}

    presses_by_mode = {"plan": 1, "bypass": 3, "accept": 2, "none": 2}
    expected_by_mode = {
        "plan": "bypass",
        "bypass": "plan",
        "accept": "bypass",
        "none": "plan",
    }
    presses = presses_by_mode[before]
    expected = expected_by_mode[before]
    idempotency_hash = _send_operation_fingerprint(
        "keypress",
        text="BTab",
        effects={"route": "mode-toggle", "from": before, "to": expected, "presses": presses},
    )
    operation_id = _s(params, "operation_id")
    idempotent = _SEND_IDEMPOTENCY.begin(
        operation_id, pane=phys_pane, payload_hash=idempotency_hash
    )
    if idempotent is not None:
        return idempotent

    pre_gate = send_gate.evaluate(("send-keys", "-t", phys_pane, "BTab"))
    if pre_gate is not None and pre_gate.get("suppressed"):
        _SEND_IDEMPOTENCY.abort(operation_id)
        deferred = _defer_or_drop_typing_guard(
            route="/mode-toggle",
            params=params,
            pane=target_pane,
            phys_pane=phys_pane,
            gate=pre_gate,
        )
        if deferred is not None:
            return {**deferred, "sent": False}
        raise TmuxSendGated({**pre_gate, "policy": "cancel", "deferred": True})

    owner_token = _hold_agent_guard(phys_pane, seconds=8)
    held = bool(owner_token)
    override_ctx = (
        thread_local_override("tmuxctld-send-holder", owner=owner_token)
        if held
        else contextlib.nullcontext()
    )
    send_exception: BaseException | None = None
    try:
        with override_ctx:
            delay_seconds = _f(params, "delay", 0.15)
            for index in range(presses):
                control.adapter.send_keys(target_pane, "BTab")
                if delay_seconds > 0 and index + 1 < presses:
                    time.sleep(delay_seconds)
        gate = getattr(control.adapter, "last_send_gate_result", None)
        if gate and gate.get("suppressed"):
            raise TmuxSendGated(gate)
    except BaseException as exc:
        send_exception = exc
        raise
    finally:
        if held:
            _release_agent_guard(phys_pane, owner=owner_token)
        if send_exception is not None:
            _SEND_IDEMPOTENCY.abort(operation_id)

    after_capture = control.adapter.run(
        "capture-pane", "-t", target_pane, "-p", "-S", "-5", allow_failure=True
    )
    after = control.detect_mode_from_capture(after_capture)
    confirmed = after == expected
    result = {
        "pane": target_pane,
        "from": before,
        "to": expected,
        "observed": after,
        "presses": presses,
        "status": "toggled" if confirmed else "unverified",
        "verification_status": "toggled" if confirmed else "unverified",
        "verified_by": "capture-pane" if confirmed else None,
        "operation_id": operation_id or None,
        "payload_hash": idempotency_hash,
        "idempotent_replay": False,
        "guard_held": held,
        "failures": []
        if confirmed
        else [
            {
                "type": "keypress_unverified",
                "detail": f"mode capture did not change to expected {expected!r}",
            }
        ],
    }
    _SEND_IDEMPOTENCY.finish(
        operation_id, pane=phys_pane, payload_hash=idempotency_hash, result=result
    )
    return result


def _h_open_session_doc(control, params):
    return control.open_session_doc(_s(params, "arg", _s(params, "pane", "current")))


def _h_goto_spoken(control, params):
    return control.goto_spoken(
        db_path=_opt(params, "db_path"),
        max_age_seconds=_i(params, "max_age_seconds", 600),
    )


def _h_typing_guard_state(control, params):
    del control
    cmd = _s(params, "cmd", _s(params, "action"))
    if cmd not in {"arm", "pending", "hold", "release", "expire-pane", "status"}:
        raise ValueError(
            "typing guard state cmd must be arm, pending, hold, release, expire-pane, or status"
        )
    argv = [cmd]
    for key in ("pane", "seconds", "now", "client", "term", "pid", "session", "owner"):
        value = _opt(params, key)
        if value is not None and value != "":
            argv.extend([f"--{key}", str(value)])
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        rc = typing_guard_state.main(argv)
    text = stdout.getvalue().strip()
    payload = {"returncode": int(rc or 0)}
    if text:
        try:
            payload.update(json.loads(text))
        except json.JSONDecodeError:
            payload["stdout"] = text
    elif _s(params, "pane"):
        payload.update(
            typing_guard_state.status(
                typing_guard_state.Tmux(typing_guard_state.tmux_binary()),
                _s(params, "pane"),
            )
        )
    if _s(params, "pane"):
        payload.setdefault("pane", _s(params, "pane"))
    if cmd in {"arm", "pending"}:
        # Telemetry for the keystroke-driven guard writes lives here, in the
        # daemon log — never in a client-facing display-message (Emperor ruling
        # 2026-07-02: the arm/pending hooks must fail silently at the pane).
        log.debug(
            "tmuxctld: typing-guard %s pane=%s kind=%s until=%s rc=%s",
            cmd,
            _s(params, "pane"),
            payload.get("kind"),
            payload.get("until"),
            payload.get("returncode"),
        )
    if cmd == "arm":
        _schedule_typing_guard_expiry_rehydrate(payload)
    pane = _s(params, "pane")
    if pane and str(payload.get("kind") or "").lower() == typing_guard_state.OFF:
        _schedule_deferred_drain(pane)
    return payload


def _schedule_typing_guard_expiry_rehydrate(payload: dict) -> None:
    """One-shot topology repair after a HUMAN guard's deadline.

    The timer must not clear/extend state.  It only re-reads the currently
    focused pane's existing guard projections and enables root ``Any`` if no
    active HUMAN guard remains there.
    """

    if str(payload.get("kind") or "").lower() != typing_guard_state.HUMAN:
        return
    if not payload.get("active"):
        return
    try:
        until = int(float(payload.get("until")))
    except (TypeError, ValueError):
        return

    def _fire() -> None:
        delay = max(0.0, until - time.time())
        if delay:
            time.sleep(delay)
        try:
            typing_guard_state.rehydrate_any_binding(
                typing_guard_state.Tmux(typing_guard_state.tmux_binary()),
                now=typing_guard_state.now_epoch(),
            )
            pane = str(payload.get("pane") or "")
            if pane:
                _schedule_deferred_drain(pane)
        except Exception:
            pass

    threading.Thread(target=_fire, name="typing-guard-expiry-rehydrate", daemon=True).start()


def _h_typing_guard_topology(control, params):
    del control
    cmd = _s(params, "cmd", _s(params, "action", "rehydrate"))
    tmux = typing_guard_state.Tmux(typing_guard_state.tmux_binary())
    if cmd == "rehydrate":
        return typing_guard_state.rehydrate_any_binding(
            tmux,
            _s(params, "pane"),
            now=typing_guard_state.now_epoch(_opt(params, "now")),
        )
    if cmd == "enable":
        return typing_guard_state.enable_any_binding(tmux)
    if cmd == "disable":
        return typing_guard_state.disable_any_binding(tmux)
    if cmd == "reconcile":
        # Explicit deploy-coherence re-source of the permanent PENDING-branch keys
        # (Enter/C-m/BSpace/C-h/C-c). The same repair rides /health automatically;
        # this route lets a deploy step / operator force it on demand.
        return typing_guard_state.reconcile_pending_bindings(tmux)
    raise ValueError("typing guard topology cmd must be rehydrate, enable, disable, or reconcile")


def _h_client_lease(control, params):
    del control
    cmd = _s(params, "cmd", _s(params, "action"))
    if cmd not in {"attach", "activity", "detach", "away", "protect", "status"}:
        raise ValueError(
            "client lease cmd must be attach, activity, detach, away, protect, or status"
        )
    argv = [cmd]
    if cmd == "protect":
        argv.extend([_s(params, "role"), str(_i(params, "minutes", 0))])
    else:
        for key in ("client", "term", "pid", "session", "role", "reason"):
            value = _opt(params, key)
            if value is not None and value != "":
                argv.extend([f"--{key}", str(value)])
    try:
        from tmux_client_lease import main as client_lease_main
    except Exception as exc:  # pragma: no cover - install/runtime path issue
        raise ValueError(f"tmux_client_lease unavailable: {exc}") from exc

    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = client_lease_main(argv)
    return {
        "returncode": int(rc or 0),
        "stdout": stdout.getvalue(),
        "stderr": stderr.getvalue(),
    }


def _h_pane_rename(control, params):
    name = _s(params, "name")
    if name.strip():
        return not_implemented_anchor(
            "POST",
            "/pane-rename",
            detail=(
                "explicit rename still shells through instance-name plus agent /rename; "
                "only empty-name interview nudges are daemonized"
            ),
        )
    return control.pane_rename(_s(params, "pane", "current"), name=name)


def _keybind_anchor(path: str, detail: str) -> RouteHandler:
    def _anchor(_control, _params):
        return not_implemented_anchor("POST", path, detail=detail)

    return _anchor


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


def _h_instance_stamp(control, params):
    return control.instance_stamp(
        instance_id=_s(params, "instance_id"),
        pane=_s(params, "pane"),
        wrapper_id=_s(params, "wrapper_id") or _s(params, "wrapper_launch_id"),
        pane_positional_id=_s(params, "pane_positional_id"),
        persona=_s(params, "persona"),
        engine=_s(params, "engine"),
        working_dir=_s(params, "working_dir"),
        vacate_pane=_s(params, "vacate_pane"),
    )


def _h_instance_rename(control, params):
    return control.instance_rename(
        _s(params, "name"),
        instance_id=_s(params, "instance_id"),
        pane=_s(params, "pane"),
    )


def _h_context_governor_inject(control, params):
    """Daemon-owned actuation for Token-API context governor forced prompts."""
    text = _s(params, "text")
    pane = _s(params, "pane")
    instance_id = _s(params, "instance_id")
    target = pane
    found = True
    if not target and instance_id:
        resolved = control.resolve_instance(instance_id)
        found = bool(resolved.get("found"))
        target = resolved.get("pane_id") or ""
    if not target:
        return {"instance_id": instance_id, "found": False, "status": "unresolved"}
    result = _send_text_pipeline(
        control,
        pane=target,
        text=text,
        route="/context-governor/inject",
        request_params={**params, "pane": target},
        submit=_b(params, "submit", True),
        clear_prompt=_b(params, "clear_prompt", False),
        verify=_b(params, "verify", True),
        operation_id=(
            _s(params, "operation_id")
            or (
                f"context-governor:{instance_id}:{_s(params, 'stage')}:"
                f"{hashlib.sha256(text.encode()).hexdigest()[:12]}"
            )
        ),
        correlation_id=_s(params, "correlation_id"),
    )
    return {**result, "instance_id": instance_id, "found": found, "actuator": "context-governor"}


def _h_context_governor_stop(control, params):
    """No-progress stage: stop further autonomous input via daemon-owned actuation.

    For singleton/orchestrator panes this is intentionally conservative: insert a
    visible hard-stop handoff prompt rather than killing a pane. Worker lifecycle
    closure remains available through /close when policy class support is expanded.
    """
    reason = _s(params, "reason") or "context_exhausted"
    text = (
        "Context governor hard stop: no compaction, handoff, plan submission, or "
        "session-doc checkpoint was observed after the forced context warning. "
        "Stop autonomous work now, preserve handoff state, and wait for supervisor routing. "
        f"Reason: {reason}."
    )
    injected = _h_context_governor_inject(
        control, {**params, "text": text, "stage": "no_progress_stop"}
    )
    if not injected.get("found", True) or injected.get("status") == "unresolved":
        return {**injected, "status": "unresolved", "reason": reason}
    if injected.get("ok") is False or injected.get("error"):
        return {**injected, "reason": reason}
    return {**injected, "status": "stopped_autonomous_input", "reason": reason}


def _h_instance_send_text(control, params):
    instance_id = _s(params, "instance_id")
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
    result: dict = {}
    try:
        _voice_set_option_best_effort(control, session.target_role, _VOICE_PROCESSING_OPTION, "1")
        result = _insert_without_submit_pipeline(
            control,
            pane=session.target_role,
            text=segment,
            action=lambda: control.send_text(session.target_role, segment, submit=False),
            route="/send-text",
            request_params={
                **params,
                "pane": session.target_role,
                "text": segment,
                "submit": False,
            },
            operation_id=_s(params, "operation_id"),
            verify_timeout=_f(params, "verify_timeout", 1.0),
            effects={
                "route": "voice-session-append",
                "voice_session_id": session.voice_session_id,
                "utterance_index": session.utterances,
            },
        )
    finally:
        _voice_set_option_best_effort(control, session.target_role, _VOICE_PROCESSING_OPTION, "0")
    session.utterances += 1
    VOICE_SESSIONS.update(session)
    return {
        **result,
        "inserted": result.get("insert_confirmed", False),
        "target_role": session.target_role,
        "utterances": session.utterances,
    }


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

_DEFERRED_ROUTE_HANDLERS: dict[str, RouteHandler] = {
    "/tmux/send-keys": _h_send_keys,
    "/send-keys": _h_send_keys,
    "/send-text": _h_send_text,
    "/insert-text": _h_insert_text,
    "/prompt-start": _h_prompt_start,
    "/prompt-end": _h_prompt_end,
    "/mode-toggle": _h_mode_toggle,
    "/invoke-skill": _h_invoke_skill,
    "/send-ethereal": _h_send_ethereal,
    "/append-user-text": _h_append_user_text,
    "/insert-invocation": _h_insert_invocation,
    "/context-governor/inject": _h_context_governor_inject,
}

ROUTES: dict[tuple[str, str], RouteHandler] = {
    # Resolution (GET)
    ("GET", "/tmux/resolve-instance"): _h_resolve_instance,
    ("GET", "/tmux/instance-id-for-pane"): _h_instance_id_for_pane,
    ("GET", "/resolve-pane"): _h_resolve_pane,
    ("POST", "/resolve-pane"): _h_resolve_pane,
    ("GET", "/ledger/resolve"): _h_ledger_resolve,
    ("POST", "/ledger/resolve"): _h_ledger_resolve,
    ("GET", "/ledger/rows"): _h_ledger_rows,
    ("POST", "/ledger/upsert"): _h_ledger_upsert,
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
    ("POST", "/tmux/run"): _h_tmux_run,
    # Send + act (POST)
    ("POST", "/tmux/send-keys"): _h_send_keys,
    ("POST", "/send-text"): _h_send_text,
    ("POST", "/pane-live"): _h_pane_live,
    ("POST", "/live-agents"): _h_live_agents,
    ("POST", "/insert-text"): _h_insert_text,
    ("POST", "/prompt-start"): _h_prompt_start,
    ("POST", "/prompt-end"): _h_prompt_end,
    ("POST", "/invoke-skill"): _h_invoke_skill,
    ("POST", "/send-ethereal"): _h_send_ethereal,
    ("POST", "/append-user-text"): _h_append_user_text,
    ("POST", "/insert-invocation"): _h_insert_invocation,
    ("POST", "/assert-instance"): _h_assert_instance,
    ("POST", "/hooks/user-prompt-submit"): _h_hook_user_prompt_submit,
    ("POST", "/hooks/wrapperstart"): _h_hook_wrapperstart,
    ("POST", "/hooks/wrapperend"): _h_hook_wrapperend,
    ("POST", "/worktree/teardown"): _h_worktree_teardown,
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
    ("POST", "/grid-expand"): _h_grid_expand,
    ("POST", "/mode-toggle"): _h_mode_toggle,
    ("POST", "/open-session-doc"): _h_open_session_doc,
    ("POST", "/goto-spoken"): _h_goto_spoken,
    ("POST", "/typing-guard-state"): _h_typing_guard_state,
    ("POST", "/typing-guard-topology"): _h_typing_guard_topology,
    ("POST", "/client-lease"): _h_client_lease,
    ("POST", "/pane-rename"): _h_pane_rename,
    ("POST", "/shuttle"): _keybind_anchor(
        "/shuttle",
        "tmux-shuttle promotion/demotion is multi-step interactive swap/retag/relocate work",
    ),
    ("POST", "/mark-for-close"): _keybind_anchor(
        "/mark-for-close",
        "interactive closeout menu and final-message rollback contract remain script-owned",
    ),
    ("POST", "/reset"): _keybind_anchor(
        "/reset",
        "workspace reset rebuild/reinject flow remains too destructive to half-port",
    ),
    ("POST", "/ethereal-prompt"): _keybind_anchor(
        "/ethereal-prompt",
        "active-pane /btw capture and codex /side flow need a dedicated safe daemon primitive",
    ),
    ("POST", "/tts/listen"): _keybind_anchor(
        "/tts/listen",
        "tmux-tts-listen script is not present in this checkout",
    ),
    ("POST", "/legion-prompt"): _keybind_anchor(
        "/legion-prompt",
        "legion prompt popup is an interactive readline/dispatch selector",
    ),
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
    ("POST", "/instance/rename"): _h_instance_rename,
    ("POST", "/instance/stamp"): _h_instance_stamp,
    ("POST", "/context-governor/inject"): _h_context_governor_inject,
    ("POST", "/context-governor/stop"): _h_context_governor_stop,
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
        activated = _activated_listen_fd()
        self.socket_activation_source = ""
        if activated is None:
            super().__init__(server_address, TmuxctldHandler)
        else:
            fd, source = activated
            super().__init__(server_address, TmuxctldHandler, bind_and_activate=False)
            self.socket.close()
            self.socket = socket.fromfd(fd, socket.AF_INET, socket.SOCK_STREAM)
            try:
                os.close(fd)
            except OSError:
                pass
            self.server_address = self.socket.getsockname()
            host, port = self.server_address[:2]
            self.server_name = socket.getfqdn(host)
            self.server_port = port
            self.socket_activation_source = source
            log.info(
                "tmuxctld adopted activated listener source=%s fd=%d address=%s",
                source,
                fd,
                self.server_address,
            )
        self.adapter_factory: Callable[[], TmuxAdapter] = adapter_factory or (lambda: TmuxAdapter())
        self.version = version
        self.sha = sha
        self.advertised_port = advertised_port or server_address[1]
        self.ready = threading.Event()
        self.operation_monitor = OperationMonitor()
        try:
            from .wrapper_ledger import LEDGER

            LEDGER.load()
            # Producer reliability backstop: wrapperstart posts are intentionally
            # best-effort, but the wrapper's local fast-path stamp is durable in
            # tmux pane options. On daemon restart, immediately rebuild active
            # rows from those stamps so a missed POST does not become a comms
            # blackout until some later manual /reconcile.
            LEDGER.reconcile_from_tmux(self.adapter_factory())
        except Exception:
            log.exception("tmuxctld wrapper ledger load/reconcile failed")
        try:
            _PROMPT_SUBMIT_SNIFFER.load_callbacks()
        except Exception:
            log.exception("tmuxctld prompt-submit callback load failed")
        try:
            _DEFERRED_SEND_QUEUE.load()
            _schedule_all_deferred_drains()
        except Exception:
            log.exception("tmuxctld deferred-send queue load failed")
        # Throttle state for the /health-driven lifecycle-hook re-assertion. A
        # 0.0 deadline forces the re-install on the first health check after boot.
        self._hook_reassert_lock = threading.Lock()
        self._hook_reassert_deadline = 0.0
        # Throttle state for the /health-driven guard-binding deploy-coherence
        # reconcile (see _BINDING_RECONCILE_INTERVAL_SECONDS). A 0.0 deadline forces
        # the reconcile on the first health check after boot — so a daemon bounced by
        # a deploy re-sources the live key-table on its very first heartbeat.
        self._binding_reconcile_lock = threading.Lock()
        self._binding_reconcile_deadline = 0.0

    def maybe_reassert_lifecycle_hooks(self) -> bool:
        """Re-install the tmux lifecycle hooks if the throttle interval has elapsed.

        Rides the /health heartbeat instead of a dedicated poller. Returns True
        when a re-assertion was performed this call (so it ran), False when the
        throttle suppressed it. ``ensure_tmux_lifecycle_hooks`` is idempotent and
        non-fatal, and any failure is swallowed so /health never breaks.

        A re-assertion that does not actually land (``ensure_tmux_lifecycle_hooks``
        reports ``ok=False`` because ``set-hook`` timed out on a wedged tmux, or it
        raised) pulls the throttle deadline in to ``_HOOK_REASSERT_RETRY_SECONDS`` so
        the hook self-heals on the next heartbeat rather than staying uninstalled for
        a full interval.
        """
        now = time.monotonic()
        with self._hook_reassert_lock:
            if now < self._hook_reassert_deadline:
                return False
            self._hook_reassert_deadline = now + _HOOK_REASSERT_INTERVAL_SECONDS
        installed = False
        try:
            result = ensure_tmux_lifecycle_hooks()
            installed = bool(result and result.get("ok"))
        except Exception:  # never let a hook re-install break the health contract
            log.exception("tmux lifecycle hook re-assertion failed")
        if not installed:
            # The install did not land — retry soon instead of holding the full
            # interval, so a wedged tmux self-heals the moment it recovers. Only ever
            # pull the deadline EARLIER; never push a concurrent success later.
            retry_deadline = now + _HOOK_REASSERT_RETRY_SECONDS
            with self._hook_reassert_lock:
                if retry_deadline < self._hook_reassert_deadline:
                    self._hook_reassert_deadline = retry_deadline
        return True

    def maybe_reconcile_guard_bindings(self) -> bool:
        """Re-source the permanent guard PENDING-branch keys if the live table drifted.

        The deploy-coherence backstop: after a deploy advances the daemon SHA, the
        permanently-bound Enter/C-m/BSpace/C-h/C-c keys keep their OLD form until an
        explicit source-file (they never focus-toggle like ``Any``). Riding the
        /health heartbeat re-sources them onto the running tmux server within one
        throttle interval — no new poller, no restart/kickstart.
        ``reconcile_pending_bindings`` is idempotent (no-op when canonical) and
        fail-open (no-op when tmux is unreachable); any failure is swallowed so
        /health never breaks. Returns True when the reconcile ran this call.

        The reconcile runs synchronously on the health request but adds latency to
        at most ONE /health per throttle interval (60s), and even then only bounded
        cost: five 0.3s-capped ``list-keys`` reads plus, only on genuine drift, one
        1.0s-capped ``source-file``. This is the same bounded/throttled profile as
        the sibling pane-died ``maybe_reassert_lifecycle_hooks`` above — deliberately
        on the heartbeat (no new poller; the daemon reconcile-poll ban stands).
        """
        now = time.monotonic()
        with self._binding_reconcile_lock:
            if now < self._binding_reconcile_deadline:
                return False
            self._binding_reconcile_deadline = now + _BINDING_RECONCILE_INTERVAL_SECONDS
        try:
            typing_guard_state.reconcile_pending_bindings(
                typing_guard_state.Tmux(typing_guard_state.tmux_binary())
            )
        except Exception:  # never let a binding reconcile break the health contract
            log.exception("typing-guard binding reconcile failed")
        return True

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
        self._safe_dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._safe_dispatch("POST")

    def finish(self) -> None:
        """Suppress normal client-disconnect noise from stdlib connection cleanup."""

        try:
            super().finish()
        except OSError as exc:
            if _is_client_disconnect(exc):
                return
            raise

    def _safe_dispatch(self, method: str) -> None:
        try:
            self._dispatch(method)
        except OSError as exc:
            if _is_client_disconnect(exc):
                return
            raise

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
            # Durably keep the global pane-died hook installed by riding this
            # heartbeat (throttled; idempotent) — no dedicated reconcile poller.
            self.server.maybe_reassert_lifecycle_hooks()
            # Close the CD deploy-coherence gap the same way: re-source the permanent
            # guard PENDING-branch keys onto the running server if they drifted from
            # canonical after a deploy (throttled; idempotent; fail-open).
            self.server.maybe_reconcile_guard_bindings()
            self._write(200, self._health_payload())
            return

        handler = ROUTES.get((method, path))
        if handler is None:
            self._write(404, self._error("not_found", f"no route for {method} {path}"))
            return

        op_id = self.server.operation_monitor.begin(method, path)
        op_ok = False
        try:
            control = TmuxControlPlane(self.server.adapter_factory())
            result = handler(control, params)
            self._write(200, {"ok": True, "result": result})
            op_ok = True
        except TmuxctldNotImplementedAnchor as exc:
            # Forward tombstone: the daemon route is intentionally named but not
            # built. This is an actual HTTP 501 so transport shims / API callers
            # fail loudly. NOTE (2026-07-03): "fail loudly" is a caller/API-side
            # contract only — 501 anchors must NOT be wired into human-facing,
            # status-line-flashing keybindings. A keybind bound through
            # tmuxctld-ping to a 501 anchor makes curl exit nonzero and the tmux
            # `||` fallback flash a raw `tmuxctld-ping-/…-failed` token at the
            # Emperor (the bug fixed in tmux-base.conf: such keys are now disabled
            # with a concise non-raw message until a real handler exists). Keep
            # anchor routes off human keymaps; surface the crater to logs/API.
            self._write(
                501,
                self._error(
                    "not_implemented",
                    str(exc),
                    detail={"method": exc.method, "path": exc.path, "detail": exc.detail},
                ),
            )
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
        finally:
            self.server.operation_monitor.finish(op_id, ok=op_ok)

    def _health_payload(self) -> dict:
        socket_health = recover_missing_tmux_socket()
        return {
            "ok": True,
            "tmux_reachable": tmux_reachable(self.server.adapter_factory()),
            "tmux_socket_state": socket_health["state"],
            "tmux_socket_recovery": socket_health["recovery"],
            "version": self.server.version,
            "sha": self.server.sha,
            "port": self.server.advertised_port,
            **self.server.operation_monitor.snapshot(),
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
        except OSError as exc:
            if _is_client_disconnect(exc):
                return
            raise

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
    ensure_tmux_lifecycle_hooks()
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
