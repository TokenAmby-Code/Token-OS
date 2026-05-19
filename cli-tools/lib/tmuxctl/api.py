from __future__ import annotations

import json
import os
import platform
import sys
import urllib.error
import urllib.request

from .enums import AttachmentClass
from .models import ClientAttachment, GroupedSessionSnapshot, InstanceRegistrySnapshot
from .registry import build_registry_snapshot


class RegistryError(RuntimeError):
    """Raised when the instance registry cannot be fetched."""


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


def _api_get_json(path: str) -> dict | list:
    api_url = _token_api_url().rstrip("/")
    try:
        with urllib.request.urlopen(f"{api_url}{path}", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, urllib.error.URLError) as exc:
        raise RegistryError(f"failed to fetch {path} from {api_url}") from exc


def fetch_session_doc_for_pane_label(pane_label: str) -> dict:
    """Resolve a cardinal pane label to its linked session document.

    This intentionally keys on stable @PANE_ID/pane_label values such as
    ``palace:N`` or ``legion:custodes``. It does not accept or require raw tmux
    ``%pane`` ids.
    """
    instances = _api_get_json("/api/instances?status=processing&sort=recent_activity")
    if not isinstance(instances, list):
        instances = []
    candidates = [row for row in instances if row.get("pane_label") == pane_label]
    if not candidates:
        all_instances = _api_get_json("/api/instances?sort=recent_activity")
        if isinstance(all_instances, list):
            candidates = [
                row
                for row in all_instances
                if row.get("pane_label") == pane_label and row.get("status") != "stopped"
            ]
    if not candidates:
        raise RegistryError(f"no live instance for pane label {pane_label}")
    doc_id = candidates[0].get("session_doc_id")
    if not doc_id:
        raise RegistryError(f"instance for pane label {pane_label} has no session doc")
    doc = _api_get_json(f"/api/session-docs/{int(doc_id)}")
    if not isinstance(doc, dict):
        raise RegistryError(f"malformed session-doc response for {doc_id}")
    doc["instance_id"] = candidates[0].get("id")
    doc["pane_label"] = pane_label
    return doc


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
