from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from .focus_guard import preserve_focus
from .stack import stack_base_of
from .tmux_adapter import TmuxAdapter

PROTECTED_STATIC_PERSONA_PANES = frozenset(
    {
        "legion:custodes",
        "mechanicus:fabricator-general",
        "mechanicus:admin",
        "legion:malcador",
        "koronus:pax",
        "koronus:orchestrator",
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


def close_pane(adapter: TmuxAdapter, pane: str, *, timeout: float = 3.0) -> dict[str, Any]:
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


def close_instance(
    adapter: TmuxAdapter,
    instance_id: str,
    *,
    lifecycle: str = "retire",
    mode: str = "now",
    pane: str | None = None,
    timeout: float = 3.0,
) -> dict[str, Any]:
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

    lifecycle_result = _http_json("PATCH", f"/api/instances/{instance_id}/{lifecycle}")
    if not resolved_pane:
        return {
            "status": "lifecycle_applied",
            "instance_id": instance_id,
            "lifecycle": lifecycle,
            "lifecycle_result": lifecycle_result,
            "close": {"status": "skipped", "reason": "pane_unresolved"},
        }
    close_result = close_pane(adapter, resolved_pane, timeout=timeout)
    return {
        "status": "closed"
        if close_result.get("status") == "closed"
        else close_result.get("status"),
        "instance_id": instance_id,
        "lifecycle": lifecycle,
        "lifecycle_result": lifecycle_result,
        "close": close_result,
    }
