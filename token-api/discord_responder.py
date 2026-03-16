#!/usr/bin/env python3
"""
discord_responder.py <channel> <reply_to_message_id> <bot> <prompt_file> [model]

Invoked by token-api to respond to Discord messages.
Reads system prompt from file, calls claude CLI, posts response to daemon.
"""
import sys
import os
import subprocess
import json
import urllib.request
import pathlib

channel, reply_to, bot, prompt_file = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
model = sys.argv[5] if len(sys.argv) > 5 else "claude-haiku-4-5-20251001"

try:
    system_prompt = pathlib.Path(prompt_file).read_text()
finally:
    try:
        pathlib.Path(prompt_file).unlink()
    except Exception:
        pass

# Invoke claude CLI (use the binary directly, not the shell function)
claude_bin = pathlib.Path.home() / ".local" / "bin" / "claude"
result = subprocess.run(
    [
        str(claude_bin),
        "--model", model,
        "--system-prompt", system_prompt,
        "-p", f"Reply to the Discord message above as the {bot} bot.",
        "--dangerously-skip-permissions",
    ],
    capture_output=True,
    text=True,
    timeout=300 if "sonnet" in model or "opus" in model else 120,
    env={**os.environ, "CLAUDECODE": ""},
)

response = result.stdout.strip()
if not response:
    print(f"discord_responder: no response from claude (rc={result.returncode}, stderr: {result.stderr[:200]})", file=sys.stderr)
    sys.exit(1)

def _send_to_daemon(channel, bot, content, reply_to=None):
    body = {"channel": channel, "bot": bot, "content": content}
    if reply_to:
        body["reply_to"] = reply_to
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:7779/send",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)

# Post to daemon
try:
    _send_to_daemon(channel, bot, response, reply_to)
    print(f"discord_responder: sent {len(response)} chars as {bot} in #{channel}", file=sys.stderr)
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8", errors="replace")[:200]
    print(f"[discord_responder] HTTP {e.code} from daemon: {body}", file=sys.stderr, flush=True)
    if reply_to and e.code in (400, 404, 500):
        print("[discord_responder] Retrying without reply_to reference...", file=sys.stderr, flush=True)
        try:
            _send_to_daemon(channel, bot, response, reply_to=None)
            print(f"discord_responder: sent {len(response)} chars as {bot} in #{channel} (no reply_to)", file=sys.stderr)
        except Exception as e2:
            print(f"[discord_responder] Retry also failed: {e2}", file=sys.stderr, flush=True)
            sys.exit(1)
    else:
        sys.exit(1)
except urllib.error.URLError as e:
    print(f"[discord_responder] URLError (daemon down?): {e}", file=sys.stderr, flush=True)
    sys.exit(1)
except Exception as e:
    print(f"[discord_responder] Unexpected send error: {e}", file=sys.stderr, flush=True)
    sys.exit(1)
