#!/usr/bin/env python3
"""Google Chat message test utility.

Send simulated Google Chat webhook payloads to the local development server.
This is the primary interface for Google Chat testing.

Usage:
    google-chat-message "hello world"                    # Send to running server
    google-chat-message "hello" --one-shot               # Start, send, stop
    google-chat-message "hello" --one-shot --localhost   # Use localhost not ngrok
    google-chat-message "hello" --dry-run                # Show payload only
"""

from __future__ import annotations

import argparse
import json
import os
import random
import string
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests


def _detect_project_root() -> Path:
    """Detect the ProcurementAgentAI project root."""
    # Check environment variable first
    caller_dir = os.environ.get("CLI_TOOLS_CALLER_DIR")
    if caller_dir:
        path = Path(caller_dir)
        if (path / ".codex" / "utils").exists():
            return path

    # Try current directory and parents
    current = Path.cwd()
    for parent in [current] + list(current.parents):
        if (parent / ".codex" / "utils" / "trigger-deploy.js").exists():
            return parent
        if (parent / "app" / "api").exists() and (parent / "pyproject.toml").exists():
            return parent

    # Default to ProcurementAgentAI in home
    default = Path.home() / "ProcAgentDir" / "ProcurementAgentAI"
    if default.exists():
        return default

    return current


def _load_template(project_root: Path) -> dict[str, Any]:
    """Load the Google Chat message template."""
    template_path = project_root / ".codex" / "utils" / "payloads" / "google-chat-message.json"
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")
    return json.loads(template_path.read_text())


def _generate_message_id() -> str:
    """Generate a random message ID."""
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(11))


def _generate_mock_token(audience: str) -> str:
    """Generate a mock JWT for local testing.

    NOTE: This is NOT cryptographically valid - for local development only.
    The signature is properly base64url encoded but not cryptographically signed.
    """
    import base64

    header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        .decode()
        .rstrip("=")
    )

    now = int(datetime.now(UTC).timestamp())
    payload_data = {
        "aud": audience,
        "azp": "113421852997393319348",
        "email": "service-227975563@gcp-sa-gsuiteaddons.iam.gserviceaccount.com",
        "email_verified": True,
        "exp": now + 3600,
        "iat": now,
        "iss": "https://accounts.google.com",
        "sub": "113421852997393319348",
    }
    payload = base64.urlsafe_b64encode(json.dumps(payload_data).encode()).decode().rstrip("=")

    # Mock signature - base64url encoded placeholder (256 bytes = RS256 signature size)
    # This is NOT cryptographically valid but passes base64 validation
    mock_signature_bytes = bytes([0xAB] * 256)
    signature = base64.urlsafe_b64encode(mock_signature_bytes).decode().rstrip("=")
    return f"{header}.{payload}.{signature}"


def _get_server_url(localhost: bool = False) -> str:
    """Get the server URL (ngrok or localhost)."""
    if localhost:
        return "http://localhost:8080"

    # Try to read ngrok URL from state file
    project_root = _detect_project_root()
    state_file = project_root / ".local-server-state.json"

    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            ngrok_url = state.get("ngrokUrl")
            if ngrok_url:
                return ngrok_url
        except (json.JSONDecodeError, OSError):
            pass

    # Try ngrok API
    try:
        resp = requests.get("http://127.0.0.1:4040/api/tunnels", timeout=2)
        if resp.ok:
            tunnels = resp.json().get("tunnels", [])
            for tunnel in tunnels:
                url = tunnel.get("public_url", "")
                if url.startswith("https://"):
                    return url
    except requests.RequestException:
        pass

    return "http://localhost:8080"


def build_payload(
    message_text: str,
    user_email: str | None = None,
    user_name: str | None = None,
    space_name: str | None = None,
    server_url: str | None = None,
) -> dict[str, Any]:
    """Build a Google Chat webhook payload."""
    project_root = _detect_project_root()
    template = _load_template(project_root)

    event_time = datetime.now(UTC).isoformat()
    message_id = _generate_message_id()
    target_url = server_url or _get_server_url()

    # Convert template to string and replace placeholders
    payload_str = json.dumps(template)
    payload_str = payload_str.replace("{{MESSAGE_TEXT}}", message_text)
    payload_str = payload_str.replace("{{EVENT_TIME}}", event_time)
    payload_str = payload_str.replace("{{MESSAGE_ID}}", message_id)
    payload_str = payload_str.replace(
        "{{SYSTEM_ID_TOKEN}}",
        _generate_mock_token(f"{target_url}/api/local/webhook"),
    )

    payload = json.loads(payload_str)

    # Apply custom options
    if user_email:
        payload["chat"]["user"]["email"] = user_email
        payload["chat"]["messagePayload"]["message"]["sender"]["email"] = user_email
    if user_name:
        payload["chat"]["user"]["displayName"] = user_name
        payload["chat"]["messagePayload"]["message"]["sender"]["displayName"] = user_name
    if space_name:
        payload["chat"]["messagePayload"]["space"]["displayName"] = space_name
        payload["chat"]["messagePayload"]["message"]["space"]["displayName"] = space_name

    return payload


def send_message(
    message_text: str,
    localhost: bool = False,
    user_email: str | None = None,
    user_name: str | None = None,
    space_name: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Send a Google Chat test message to the local server."""
    server_url = _get_server_url(localhost)
    payload = build_payload(
        message_text,
        user_email=user_email,
        user_name=user_name,
        space_name=space_name,
        server_url=server_url,
    )

    # Use local-only endpoint that bypasses JWT authentication completely
    # Production endpoint (/webhooks/webhook) requires valid JWT tokens from Google Chat
    endpoint = f"{server_url}/api/local/webhook"
    print(f"Sending message to {endpoint}")
    print(f"Message: {message_text}")

    headers = {
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            endpoint,
            json=payload,
            headers=headers,
            timeout=timeout,
        )
        print(f"Status: {response.status_code}")
        try:
            body = response.json()
            print(f"Response: {json.dumps(body, indent=2)}")
            return {"success": True, "status": response.status_code, "body": body}
        except json.JSONDecodeError:
            print(f"Response: {response.text}")
            return {"success": True, "status": response.status_code, "body": response.text}
    except requests.RequestException as e:
        print(f"Error: {e}")
        return {"success": False, "error": str(e)}


def run_oneshot(
    message_text: str,
    localhost: bool = False,
) -> dict[str, Any]:
    """Run one-shot test: start server, send message, stop server.

    Uses trigger-deploy.js with --google-chat-message parameter.
    """
    project_root = _detect_project_root()
    trigger_deploy = project_root / ".codex" / "utils" / "trigger-deploy.js"

    if not trigger_deploy.exists():
        print(f"Error: Script not found: {trigger_deploy}")
        return {"success": False, "error": "Script not found"}

    args = [
        "node",
        str(trigger_deploy),
        "development",
        "-l",
        "--blocking",
        "--one-shot",
        "--google-chat-message",
        message_text,
    ]

    if localhost:
        args.append("--force-localhost")

    print(f"Running one-shot test: {message_text}")
    result = subprocess.run(args, cwd=project_root)

    if result.returncode == 0:
        return {"success": True}
    return {"success": False, "exitCode": result.returncode}


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="google-chat-message",
        description="Send simulated Google Chat webhook payloads for testing.",
        epilog="""
Examples:
  google-chat-message "hello world"                    # Send to running server
  google-chat-message "hello" --one-shot               # Full test cycle
  google-chat-message "hello" --one-shot --localhost   # Use localhost not ngrok
  google-chat-message "hello" --dry-run                # Show payload only
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "message",
        nargs="?",
        help="The message text to send",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate payload without sending",
    )
    parser.add_argument(
        "--one-shot",
        action="store_true",
        help="Start server, send message, stop server",
    )
    parser.add_argument(
        "--localhost",
        action="store_true",
        help="Force localhost instead of ngrok",
    )
    parser.add_argument(
        "--user-email",
        type=str,
        help="Set sender email address",
    )
    parser.add_argument(
        "--user-name",
        type=str,
        help="Set sender display name",
    )
    parser.add_argument(
        "--space-name",
        type=str,
        help="Set chat space name",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Main entry point."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.message:
        parser.error("message is required")

    if args.dry_run:
        payload = build_payload(
            args.message,
            user_email=args.user_email,
            user_name=args.user_name,
            space_name=args.space_name,
        )
        print(json.dumps(payload, indent=2))
        return

    if args.one_shot:
        result = run_oneshot(
            args.message,
            localhost=args.localhost,
        )
    else:
        result = send_message(
            args.message,
            localhost=args.localhost,
            user_email=args.user_email,
            user_name=args.user_name,
            space_name=args.space_name,
        )

    if not result.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
