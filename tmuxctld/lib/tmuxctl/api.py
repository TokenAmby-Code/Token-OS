from __future__ import annotations

import json
import os
import platform
import sys
import textwrap
import urllib.error
import urllib.request
import urllib.parse

from .enums import AttachmentClass
from .models import ClientAttachment, GroupedSessionSnapshot, InstanceRegistrySnapshot
from .registry import build_registry_snapshot


class RegistryError(RuntimeError):
    """Raised when the instance registry cannot be fetched."""


class SessionDocResolutionError(RegistryError):
    """Raised when pane -> instance -> session-doc resolution fails.

    ``reason`` is intentionally machine-stable so thin shell wrappers can
    distinguish API outages from benign unbound-doc cases without parsing
    tmux internals.
    """

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(f"{reason}: {message}")


_DEVICE_NAMES = {
    "mac": "Mac-Mini",
    "wsl": "TokenPC",
    "phone": "Token-S24",
    "linux": "",
}


def _detect_machine() -> str:
    machine = os.environ.get("IMPERIUM_MACHINE")
    if machine:
        return machine
    if sys.platform == "darwin":
        return "mac"
    if "microsoft" in platform.uname().release.lower():
        return "wsl"
    if os.path.isdir("/data/data/com.termux"):
        return "phone"
    return "linux"


def _token_api_url() -> str:
    env = os.environ.get("TOKEN_API_URL")
    if env:
        return env
    machine = _detect_machine()
    if machine == "mac":
        return "http://localhost:7777"
    return "http://100.95.109.23:7777"


def _device_name() -> str:
    env = os.environ.get("IMPERIUM_DEVICE_NAME")
    if env:
        return env
    return _DEVICE_NAMES.get(_detect_machine(), "")


def fetch_instance_registry() -> InstanceRegistrySnapshot:
    api_url = _token_api_url().rstrip("/")
    try:
        with urllib.request.urlopen(f"{api_url}/api/instances", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, urllib.error.URLError) as exc:
        raise RegistryError(f"failed to fetch instance registry from {api_url}") from exc
    return build_registry_snapshot(
        device_id=_device_name(),
        instances=payload,
    )


def stop_instance(instance_id: str) -> None:
    api_url = _token_api_url().rstrip("/")
    request = urllib.request.Request(f"{api_url}/api/instances/{instance_id}", method="DELETE")
    try:
        with urllib.request.urlopen(request, timeout=5):
            return
    except (OSError, urllib.error.URLError) as exc:
        raise RegistryError(f"failed to stop instance {instance_id} via {api_url}") from exc


def patch_instance(instance_id: str, suffix: str, body: dict) -> None:
    api_url = _token_api_url().rstrip("/")
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{api_url}/api/instances/{instance_id}/{suffix.lstrip('/')}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(request, timeout=5):
            return
    except (OSError, urllib.error.URLError) as exc:
        raise RegistryError(
            f"failed to patch instance {instance_id}/{suffix} via {api_url}"
        ) from exc


def update_instance_activity(instance_id: str, action: str = "prompt_submit") -> None:
    api_url = _token_api_url().rstrip("/")
    payload = json.dumps({"action": action}).encode("utf-8")
    request = urllib.request.Request(
        f"{api_url}/api/instances/{instance_id}/activity",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5):
            return
    except (OSError, urllib.error.URLError) as exc:
        raise RegistryError(
            f"failed to update activity for instance {instance_id} via {api_url}"
        ) from exc


def log_event(event_type: str, *, instance_id: str = "", details: dict | None = None) -> None:
    api_url = _token_api_url().rstrip("/")
    try:
        # Serialize inside the try: telemetry is best-effort, so a
        # non-serializable `details` payload must not raise into callers.
        # `default=str` coerces stragglers (Paths, datetimes, etc.).
        payload = json.dumps(
            {
                "event_type": event_type,
                "instance_id": instance_id or None,
                "details": details or {},
            },
            default=str,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{api_url}/api/events/log",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3):
            return
    except (OSError, urllib.error.URLError, TypeError, ValueError):
        return


def _api_get_json(path: str) -> dict | list:
    api_url = _token_api_url().rstrip("/")
    try:
        with urllib.request.urlopen(f"{api_url}{path}", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, urllib.error.URLError) as exc:
        raise RegistryError(f"failed to fetch {path} from {api_url}") from exc


def fetch_instance_rows_raw() -> list[dict]:
    """Fetch raw instance rows (full JSON), including correlation columns.

    The typed :class:`InstanceRegistryEntry` model omits columns like ``pid`` and
    ``session_id``; callers that need them (e.g. stack-sweep pane reconciliation)
    read the raw dicts instead of the parsed snapshot.
    """
    data = _api_get_json("/api/instances")
    return data if isinstance(data, list) else []


def rebind_instance_pane(instance_id: str, pane_id: str) -> None:
    """Retired: Token-API no longer exposes pane rebinding as a DB/API mutation."""
    raise RegistryError(
        "rebind_instance_pane is retired; use tmuxctl/live @INSTANCE_ID stamps, not Token-API"
    )


def fetch_session_doc_for_instance_id(instance_id: str, *, pane_label: str = "") -> dict:
    """Resolve an instance id to its linked session document via the durable FK.

    The pane leg is resolved before this function by tmuxctl's live
    ``@INSTANCE_ID`` stamp oracle. This function deliberately never joins on
    ``pane_label``/``tmux_pane`` registry fields; those are presentation-only and
    may be absent for codex/undercount panes.
    """
    iid = (instance_id or "").strip()
    if not iid:
        raise SessionDocResolutionError("pane_mismatch", "no live instance stamp for pane")

    row = _api_get_json(f"/api/instances/{iid}")
    if not isinstance(row, dict) or not row.get("id"):
        raise SessionDocResolutionError("pane_mismatch", f"no active instance row for {iid}")
    if str(row.get("status") or "") in {"stopped", "archived"}:
        raise SessionDocResolutionError(
            "pane_mismatch",
            f"instance {iid} is not live (status={row.get('status')})",
        )

    doc_id = row.get("session_doc_id")
    if not doc_id:
        raise SessionDocResolutionError("no_doc_bound", f"instance {iid} has no session doc bound")

    try:
        doc = _api_get_json(f"/api/session-docs/{int(doc_id)}")
    except (TypeError, ValueError) as exc:
        raise SessionDocResolutionError("no_doc_bound", f"invalid session_doc_id {doc_id!r}") from exc
    if not isinstance(doc, dict):
        raise RegistryError(f"malformed session-doc response for {doc_id}")
    doc["instance_id"] = iid
    if pane_label:
        doc["pane_label"] = pane_label
    return doc


def fetch_session_doc_for_pane_label(pane_label: str) -> dict:
    """Resolve a pane label to its session document through pane -> instance -> FK.

    Deprecated compatibility wrapper for callers that only have a public pane
    label. It uses Token-API's pane stamp resolver endpoint; it does not scan
    ``/api/instances`` by ``pane_label``.
    """
    pane = (pane_label or "").strip()
    if not pane:
        raise SessionDocResolutionError("pane_mismatch", "pane label is required")
    result = _api_get_json(f"/api/panes/{urllib.parse.quote(pane, safe='')}/instance")
    if not isinstance(result, dict) or not result.get("id"):
        raise SessionDocResolutionError("pane_mismatch", f"no live instance for pane {pane}")
    return fetch_session_doc_for_instance_id(str(result["id"]), pane_label=pane)


def _session_doc_resolution_diagnostics(pane_label: str, rows: dict | list) -> str:
    if not isinstance(rows, list):
        return ""
    matching_panes = {
        row.get("tmux_pane")
        for row in rows
        if row.get("pane_label") == pane_label and row.get("tmux_pane")
    }
    related = [
        row
        for row in rows
        if row.get("pane_label") == pane_label
        or (row.get("tmux_pane") and row.get("tmux_pane") in matching_panes)
    ][:8]
    if not related:
        recent = rows[:5]
        lines = ["diagnostics: no rows with matching pane_label; recent rows:"]
        source = recent
    else:
        lines = ["diagnostics: related registry rows:"]
        source = related
    for row in source:
        lines.append(
            textwrap.shorten(
                "  "
                f"id={row.get('id')} status={row.get('status')} "
                f"tmux_pane={row.get('tmux_pane')} pane_label={row.get('pane_label')} "
                f"session_doc_id={row.get('session_doc_id')} tab_name={row.get('tab_name')}",
                width=240,
                placeholder="…",
            )
        )
    return "\n".join(lines)


def build_client_attachments(
    client_rows: list[dict[str, str]],
    managed_sessions: tuple[GroupedSessionSnapshot, ...],
) -> tuple[ClientAttachment, ...]:
    session_map = {session.session_name: session for session in managed_sessions}
    attachments: list[ClientAttachment] = []
    for row in client_rows:
        session_name = row["session_name"]
        session = session_map.get(session_name)
        if session is None:
            continue
        tty = row["client_tty"]
        is_remote = "/pts/" in tty or tty.startswith("/dev/pts/")
        is_grouped = session_name != session.leader_session_name
        if is_remote and is_grouped:
            attachment_class = AttachmentClass.REMOTE_GROUPED
        elif is_remote:
            attachment_class = AttachmentClass.REMOTE_LEADER
        elif is_grouped:
            attachment_class = AttachmentClass.LOCAL_GROUPED
        else:
            attachment_class = AttachmentClass.LOCAL_LEADER
        attachments.append(
            ClientAttachment(
                client_tty=tty,
                session_name=session_name,
                client_name=row.get("client_name", ""),
                is_remote=is_remote,
                leader_session_name=session.leader_session_name,
                selected_window_index=int(row.get("window_index", session.selected_window_index)),
                selected_window_name=row.get("window_name", session.selected_window_name),
                attachment_class=attachment_class,
            )
        )
    return tuple(attachments)
