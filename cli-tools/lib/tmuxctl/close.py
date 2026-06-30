from __future__ import annotations

import json
import os
import signal
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Any

from .focus_guard import preserve_focus
from .liveness import detect_pane_tui, instance_live_tui
from .stack import stack_base_of
from .tmux_adapter import TmuxAdapter

PROTECTED_STATIC_PERSONA_PANES = frozenset(
    {
        "council:custodes",
        "mechanicus:fabricator-general",
        "council:administratum",
        "council:malcador",
        "council:pax",
        "mechanicus:orchestrator",
    }
)

LIFECYCLE_ALIASES = {
    "retire": "retire",
    "retire-only": "retire",
    "archive": "archive-session-doc",
    "archive-doc": "archive-session-doc",
    "archive-session-doc": "archive-session-doc",
    "retire-and-archive": "archive-session-doc",
    "banish": "banish",
}


@contextmanager
def close_contract_signal_shield():
    """Ignore terminal interrupt keys while a close contract is in flight.

    Operators often mash Ctrl-C to close Claude/Codex panes. When close is
    running inside a tmux popup or direct shell, those extra Ctrl-C keystrokes
    can reach this wrapper process as SIGINT. Once close starts, Token-API
    lifecycle, pane runtime cleanup, interrupts, kill fallback, and stack
    enforcement are one atomic contract; a human interrupt should hit only the
    target pane, never strand the wrapper halfway through its cleanup.
    """
    saved = {}
    signals = (
        getattr(signal, "SIGINT", None),
        getattr(signal, "SIGQUIT", None),
        getattr(signal, "SIGTSTP", None),
    )
    try:
        for sig in signals:
            if sig is None:
                continue
            try:
                saved[sig] = signal.signal(sig, signal.SIG_IGN)
            except (OSError, RuntimeError, ValueError):
                # Non-main threads cannot mutate signal handlers. The close
                # path still runs; callers that execute it in a main process get
                # the shield.
                pass
        yield
    finally:
        for sig, handler in saved.items():
            try:
                signal.signal(sig, handler)
            except (OSError, RuntimeError, ValueError):
                pass


def normalize_lifecycle(value: str | None) -> str:
    lifecycle = (value or "retire").strip().lower()
    normalized = LIFECYCLE_ALIASES.get(lifecycle)
    if not normalized:
        raise ValueError(f"unsupported lifecycle: {lifecycle}")
    return normalized


def _token_api_url() -> str:
    if os.environ.get("TOKEN_API_URL"):
        return os.environ["TOKEN_API_URL"].rstrip("/")
    try:
        from imperium_config import TOKEN_API_URL  # type: ignore

        return str(TOKEN_API_URL).rstrip("/")
    except Exception:
        return "http://localhost:7777"


def _http_json(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        _token_api_url() + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("utf-8")
            payload = json.loads(text) if text.strip() else {}
            if not isinstance(payload, dict):
                payload = {"response": payload}
            return payload
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise ValueError(f"Token-API {method} {path} failed: {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Token-API unavailable at {_token_api_url()}: {exc}") from exc


def _resolve_current(adapter: TmuxAdapter, pane: str) -> str:
    if pane != "current":
        return pane
    return adapter.run("display-message", "-p", "#{pane_id}").strip()


def _pane_exists(adapter: TmuxAdapter, pane: str) -> bool:
    return bool(
        adapter.run("display-message", "-t", pane, "-p", "#{pane_id}", allow_failure=True).strip()
    )


def _pane_role(adapter: TmuxAdapter, pane: str) -> str:
    return adapter.show_pane_option(pane, "@PANE_ID").strip()


def _pane_window(adapter: TmuxAdapter, pane: str) -> tuple[str, str]:
    meta = adapter.run(
        "display-message",
        "-t",
        pane,
        "-p",
        "#{session_name}:#{window_index}\t#{window_name}",
        allow_failure=True,
    ).strip()
    if not meta:
        return "", ""
    window_target, window_name = (meta.split("\t", 1) + [""])[:2]
    return window_target, window_name


def _enforce_stack(adapter: TmuxAdapter, window_target: str, window_name: str) -> str:
    base = stack_base_of(window_name.split("(", 1)[0])
    if not base:
        return "skipped"
    try:
        from .stack import enforce_stack_layout

        return enforce_stack_layout(adapter, window_target, kill_pending_clear=True)
    except Exception as exc:  # best-effort recovery after pane closure
        return f"failed: {exc}"


def clear_runtime(adapter: TmuxAdapter, pane: str) -> dict[str, Any]:
    pane = _resolve_current(adapter, pane)
    adapter.clear_runtime_state(pane)
    return {"status": "cleared", "pane": pane}


def pane_dead(adapter: TmuxAdapter, pane: str) -> bool:
    value = adapter.run(
        "display-message", "-t", pane, "-p", "#{pane_dead}", allow_failure=True
    ).strip()
    return value in {"1", "true", "yes"}


def reap_dead_husk(adapter: TmuxAdapter, pane: str, *, pane_role: str = "") -> dict[str, Any]:
    """Kill a dead remain-on-exit husk after runtime scrub.

    WrapperEnd is already terminal for the departed wrapper.  If tmux reports
    the pane as dead, leaving it behind creates the empty husk graveyard.  Live
    panes are left alone; SessionStop/SessionEnd never call this surface.
    """
    if not pane_dead(adapter, pane):
        return {"status": "skipped", "reason": "pane_live", "pane": pane}
    adapter.run("kill-pane", "-t", pane, allow_failure=True)
    if _pane_exists(adapter, pane):
        return {
            "status": "failed",
            "reason": "kill_pane_failed",
            "pane": pane,
            "pane_role": pane_role,
        }
    return {"status": "killed", "pane": pane, "pane_role": pane_role}


def _close_pane_unshielded(
    adapter: TmuxAdapter, pane: str, *, timeout: float = 3.0
) -> dict[str, Any]:
    pane = _resolve_current(adapter, pane)
    role = _pane_role(adapter, pane)
    if role in PROTECTED_STATIC_PERSONA_PANES:
        return {
            "status": "refused",
            "reason": "static_persona_pane",
            "pane": pane,
            "pane_role": role,
        }

    if not _pane_exists(adapter, pane):
        return {"status": "already_closed", "pane": pane, "pane_role": role}

    window_target, window_name = _pane_window(adapter, pane)
    method = "graceful"
    with preserve_focus(
        adapter,
        source="tmuxctl close-pane",
        attempted_target=pane,
        enabled=os.environ.get("IMPERIUM_ALLOW_TMUX_FOCUS") != "1",
    ):
        adapter.clear_runtime_state(pane)
        for _ in range(3):
            adapter.send_keys(pane, "C-c", allow_failure=True)
            time.sleep(0.2)

        deadline = time.time() + max(timeout, 0.0)
        while time.time() < deadline:
            if not _pane_exists(adapter, pane):
                stack = _enforce_stack(adapter, window_target, window_name)
                return {
                    "status": "closed",
                    "pane": pane,
                    "pane_role": role,
                    "method": method,
                    "stack_enforcement": stack,
                }
            time.sleep(0.25)

        method = "kill-pane"
        adapter.run("kill-pane", "-t", pane, allow_failure=True)

    stack = _enforce_stack(adapter, window_target, window_name)
    return {
        "status": "closed" if not _pane_exists(adapter, pane) else "failed",
        "pane": pane,
        "pane_role": role,
        "method": method,
        "stack_enforcement": stack,
    }


def close_pane(adapter: TmuxAdapter, pane: str, *, timeout: float = 3.0) -> dict[str, Any]:
    with close_contract_signal_shield():
        return _close_pane_unshielded(adapter, pane, timeout=timeout)


def close_instance(
    adapter: TmuxAdapter,
    instance_id: str,
    *,
    lifecycle: str = "retire",
    mode: str = "now",
    pane: str | None = None,
    timeout: float = 3.0,
    force: bool = False,
) -> dict[str, Any]:
    with close_contract_signal_shield():
        lifecycle = normalize_lifecycle(lifecycle)
        mode = (mode or "now").strip().lower()
        if mode not in {"now", "after-stop"}:
            raise ValueError(f"unsupported close mode: {mode}")

        resolved_pane = pane or ""
        if not resolved_pane:
            from .resolver import resolve_instance

            resolved = resolve_instance(adapter, instance_id)
            resolved_pane = resolved.pane_id or ""

        if mode == "after-stop":
            body = {"mode": "after-stop", "lifecycle": lifecycle}
            if resolved_pane:
                body["pane"] = resolved_pane
            result = _http_json("POST", f"/api/instances/{instance_id}/mark-for-close", body)
            if result.get("success") is False:
                raise ValueError(f"mark-for-close failed: {json.dumps(result, sort_keys=True)}")
            return {
                "status": "armed",
                "instance_id": instance_id,
                "lifecycle": lifecycle,
                "result": result,
            }

        # --- mode == "now": refuse-retire-while-TUI-live guard + atomic order ---
        # The reap lifecycle must NEVER retire a DB row whose pane still runs a
        # live Claude/Codex TUI. We detect liveness from the process tree (robust
        # to stamp churn and to a stale resolved-pane handle, the #314 orphan),
        # and either fail closed or kill the proc atomically BEFORE the retire.
        live = instance_live_tui(adapter, instance_id, resolved_pane)
        if live is not None and not force:
            # Fail closed: do not retire, do not kill. A live worker flagged on a
            # stale stamp (the mechanicus:1 near-miss) is refused at the tool
            # layer; pass --force to deliberately kill-then-retire.
            return {
                "status": "refused",
                "reason": "live_tui",
                "guard": "refuse-retire-while-tui-live",
                "instance_id": instance_id,
                "lifecycle": lifecycle,
                "pane": live.pane_id,
                "pane_pid": live.pane_pid,
                "agent_pid": live.agent_pid,
                "agent_command": live.agent_command,
                "hint": (
                    "pane has a live agent TUI; verify the worker is idle, or pass "
                    "--force to kill the process and retire atomically"
                ),
            }

        # Atomic kill-before-retire: close the pane FIRST, then retire — and only
        # retire once the pane is confirmed clear. The target is the live pane if
        # the divergence sweep relocated it, else the resolved handle.
        target_pane = (live.pane_id if live is not None else resolved_pane) or ""
        close_result: dict[str, Any] | None = None
        if target_pane:
            close_result = _close_pane_unshielded(adapter, target_pane, timeout=timeout)
            # Re-verify the pane we acted on: a kill that failed to clear the TUI
            # must not be papered over by retiring the row.
            post = detect_pane_tui(adapter, target_pane)
            if post.live:
                return {
                    "status": "refused",
                    "reason": "live_tui_survived_close",
                    "guard": "refuse-retire-while-tui-live",
                    "instance_id": instance_id,
                    "lifecycle": lifecycle,
                    "pane": target_pane,
                    "agent_pid": post.agent_pid,
                    "agent_command": post.agent_command,
                    "close": close_result,
                }
            if close_result.get("status") == "failed":
                # Pane stubbornly persists (no agent, but kill-pane could not
                # remove it). Fail closed rather than retire a still-present pane.
                return {
                    "status": "failed",
                    "reason": "pane_close_failed",
                    "instance_id": instance_id,
                    "lifecycle": lifecycle,
                    "close": close_result,
                }

        # Pane confirmed clear (or genuinely absent). Retire the DB row LAST.
        lifecycle_result = _http_json("PATCH", f"/api/instances/{instance_id}/{lifecycle}")
        if close_result is None:
            return {
                "status": "lifecycle_applied",
                "instance_id": instance_id,
                "lifecycle": lifecycle,
                "lifecycle_result": lifecycle_result,
                "close": {"status": "skipped", "reason": "pane_unresolved"},
            }
        return {
            "status": "closed"
            if close_result.get("status") in {"closed", "already_closed"}
            else close_result.get("status"),
            "instance_id": instance_id,
            "lifecycle": lifecycle,
            "lifecycle_result": lifecycle_result,
            "close": close_result,
        }
