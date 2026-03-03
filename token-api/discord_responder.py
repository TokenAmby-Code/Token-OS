#!/usr/bin/env python3
"""
discord_responder.py <channel> <reply_to_message_id> <bot> <prompt_file>

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
        "--model", "claude-sonnet-4-6",
        "--system-prompt", system_prompt,
        "-p", f"Reply to the Discord message above as the {bot} bot.",
        "--dangerously-skip-permissions",
        "--max-turns", "1",
    ],
    capture_output=True,
    text=True,
    timeout=120,
    env={**os.environ, "CLAUDECODE": ""},
)

# Strip claude CLI noise (e.g. "Error: Reached max turns (1)")
lines = result.stdout.strip().splitlines()
lines = [l for l in lines if not l.startswith("Error: Reached max turns")]
response = "\n".join(lines).strip()
if not response:
    print(f"discord_responder: no response from claude (rc={result.returncode}, stderr: {result.stderr[:200]})", file=sys.stderr)
    sys.exit(1)

# Post to daemon
payload = json.dumps({
    "channel": channel,
    "bot": bot,
    "content": response,
    "reply_to": reply_to,
}).encode()

req = urllib.request.Request(
    "http://127.0.0.1:7779/send",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
urllib.request.urlopen(req, timeout=10)
print(f"discord_responder: sent {len(response)} chars as {bot} in #{channel}")
