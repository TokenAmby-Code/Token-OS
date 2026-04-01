#!/usr/bin/env python3
"""
Stop Hook: JSONL session transcript → contained transcript file + wikilink.

Usage:
    python3 stop_hook.py <session-id>
    echo '{"session_id": "..."}' | python3 stop_hook.py

Reads ~/.claude/projects/*/<session-id>.jsonl, produces a compacted transcript
file at Mars/Logs/Transcripts/, and appends a wikilink to the linked session
doc or daily note. MiniMax never writes directly to curated documents.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import glob
import subprocess
from datetime import datetime, timezone
from pathlib import Path


DB_PATH = Path.home() / ".claude" / "agents.db"
TOKEN_API_URL = os.environ.get("TOKEN_API_URL", "http://localhost:7777")

# Device → satellite URL mapping for cross-machine file access
SATELLITE_URLS = {
    "TokenPC": "http://100.66.10.74:7777",
}
LOCAL_DEVICE = os.environ.get("IMPERIUM_DEVICE_NAME", "Mac-Mini")


def mark_cron_instance_stopped(instance_id: str):
    """Explicitly mark a cron instance as stopped in agents.db.

    The stop hook fires during Claude Code teardown. The Token API may or may
    not have received a DELETE for this instance yet — explicitly writing
    'stopped' here gives the cron mutex check a reliable signal.
    """
    try:
        con = sqlite3.connect(str(DB_PATH))
        con.execute(
            "UPDATE claude_instances SET status = 'stopped' WHERE id = ? AND status != 'stopped'",
            (instance_id,),
        )
        rows = con.total_changes
        con.commit()
        con.close()
        if rows:
            print(f"[info] Cron instance {instance_id[:8]} marked stopped in DB", file=sys.stderr)
        else:
            print(f"[info] Cron instance {instance_id[:8]} already stopped or not found", file=sys.stderr)
    except Exception as e:
        print(f"[warn] Could not mark cron instance stopped: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Tool summarization
# ---------------------------------------------------------------------------

def _truncate(s, n=80):
    s = str(s).strip().replace("\n", " ")
    return s[:n] + "..." if len(s) > n else s


def _count_lines(content):
    """Count lines in a Read tool result (format: '     N→text')."""
    if isinstance(content, str):
        return content.count("\n→") + content.count("→")
    return 0


def summarize_tool_use(block, result_content=None):
    name = block.get("name", "?")
    inp = block.get("input", {})

    if name == "Bash":
        cmd = inp.get("command", "")
        return f"[Bash: {_truncate(cmd, 100)}]"

    if name == "Read":
        path = inp.get("file_path", "?")
        short = Path(path).name
        if result_content and isinstance(result_content, str):
            lines = result_content.count("→")
            return f"[Read: {short} → {lines} lines]"
        return f"[Read: {short}]"

    if name == "Write":
        path = inp.get("file_path", "?")
        content = inp.get("content", "")
        lines = content.count("\n") + 1 if content else 0
        return f"[Write: {Path(path).name} ({lines} lines)]"

    if name == "Edit":
        path = inp.get("file_path", "?")
        return f"[Edit: {Path(path).name}]"

    if name == "Glob":
        return f"[Glob: {inp.get('pattern', '?')}]"

    if name == "Grep":
        pat = inp.get("pattern", "?")
        inc = inp.get("include", "")
        return f"[Grep: {_truncate(pat, 50)}{' in ' + inc if inc else ''}]"

    if name == "ToolSearch":
        return f"[ToolSearch: {inp.get('query', '?')}]"

    if name == "TodoWrite":
        todos = inp.get("todos", [])
        return f"[TodoWrite: {len(todos)} items]"

    if name == "Agent":
        return f"[Agent: subagent_type={inp.get('subagent_type', '?')}]"

    # Generic fallback
    inp_str = _truncate(str(inp), 80) if inp else ""
    return f"[{name}: {inp_str}]" if inp_str else f"[{name}]"


# ---------------------------------------------------------------------------
# Collapse consecutive tool calls to the same file
# ---------------------------------------------------------------------------

_FILE_TOOL_RE = re.compile(r"^\[(Edit|Write|Read): ([^\]→ ]+)")


def collapse_tools(events):
    """Collapse runs of identical tool summaries and multi-edits to same file."""
    out = []
    i = 0
    while i < len(events):
        ev = events[i]
        if ev["role"] != "tool":
            out.append(ev)
            i += 1
            continue

        j = i + 1
        while j < len(events) and events[j]["role"] == "tool":
            j += 1

        run = events[i:j]

        if len(run) == 1:
            out.append(ev)
            i = j
            continue

        collapsed_run = []
        k = 0
        while k < len(run):
            cur = run[k]
            m = _FILE_TOOL_RE.match(cur["text"])
            if not m:
                collapsed_run.append(cur)
                k += 1
                continue

            tool_name = m.group(1)
            filename = m.group(2)
            count = 1
            while (k + count < len(run)
                   and _FILE_TOOL_RE.match(run[k + count]["text"])
                   and _FILE_TOOL_RE.match(run[k + count]["text"]).group(1) == tool_name
                   and _FILE_TOOL_RE.match(run[k + count]["text"]).group(2) == filename):
                count += 1

            if count > 1:
                collapsed_run.append({"role": "tool", "text": f"[{count}x {tool_name}: {filename}]"})
            else:
                collapsed_run.append(cur)
            k += count

        out.extend(collapsed_run)
        i = j

    return out


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------

def parse_jsonl(path):
    lines = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError:
                pass
    return lines


def clean_transcript(lines):
    """Convert raw JSONL lines into a list of {role, text} events."""

    tool_results = {}
    for line in lines:
        if line.get("type") != "user":
            continue
        content = line.get("message", {}).get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                tid = block.get("tool_use_id", "")
                raw_content = block.get("content", "")
                if isinstance(raw_content, list):
                    raw_content = " ".join(
                        b.get("text", b.get("tool_name", ""))
                        for b in raw_content
                        if isinstance(b, dict)
                    )
                tool_results[tid] = raw_content

    events = []
    seen_tool_ids = set()

    for line in lines:
        t = line.get("type")

        if t == "user":
            content = line.get("message", {}).get("content", "")
            if isinstance(content, str) and content.strip():
                events.append({"role": "user", "text": content.strip()})
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text = block.get("text", "").strip()
                        if text:
                            events.append({"role": "user", "text": text})

        elif t == "assistant":
            content = line.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")

                if btype == "text":
                    text = block.get("text", "").strip()
                    if text:
                        events.append({"role": "assistant", "text": text})

                elif btype == "tool_use":
                    tid = block.get("id", "")
                    if tid in seen_tool_ids:
                        continue
                    seen_tool_ids.add(tid)
                    result = tool_results.get(tid)
                    summary = summarize_tool_use(block, result)
                    events.append({"role": "tool", "text": summary})

    return collapse_tools(events)


# ---------------------------------------------------------------------------
# Render transcript to string
# ---------------------------------------------------------------------------

def render_transcript(events, max_user_chars=1500, max_assistant_chars=2000):
    parts = []
    for ev in events:
        role = ev["role"]
        text = ev["text"]

        if role == "user":
            if len(text) > max_user_chars:
                text = text[:max_user_chars] + f"\n... [{len(text) - max_user_chars} chars truncated]"
            parts.append(f"USER:\n{text}")

        elif role == "assistant":
            if len(text) > max_assistant_chars:
                text = text[:max_assistant_chars] + f"\n... [{len(text) - max_assistant_chars} chars truncated]"
            parts.append(f"ASSISTANT:\n{text}")

        elif role == "tool":
            parts.append(f"  {text}")

    return "\n\n".join(parts)


def estimate_tokens(text):
    return len(text) // 4


# ---------------------------------------------------------------------------
# Session doc resolution
# ---------------------------------------------------------------------------

def find_instance_for_session(session_id):
    """Return instance dict if found, else None."""
    try:
        import urllib.request
        req = urllib.request.Request(f"{TOKEN_API_URL}/api/instances/{session_id}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            if isinstance(data, dict) and data.get("id"):
                return data
        return None
    except Exception as e:
        print(f"[warn] Could not fetch instance: {e}", file=sys.stderr)
        return None


def fetch_session_doc(doc_id):
    """Return session doc dict if found, else None."""
    try:
        import urllib.request
        req = urllib.request.Request(f"{TOKEN_API_URL}/api/session-docs/{doc_id}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[warn] Could not fetch session doc: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Stats extraction
# ---------------------------------------------------------------------------

def extract_stats(events):
    """Extract turn counts and tool breakdown from events."""
    user_turns = sum(1 for e in events if e["role"] == "user")
    assistant_turns = sum(1 for e in events if e["role"] == "assistant")
    tool_calls = sum(1 for e in events if e["role"] == "tool")

    tool_counts = {}
    for e in events:
        if e["role"] == "tool":
            text = e["text"]
            m_collapsed = re.match(r"\[(\d+)x (\w+)", text)
            if m_collapsed:
                count = int(m_collapsed.group(1))
                name = m_collapsed.group(2)
                tool_counts[name] = tool_counts.get(name, 0) + count
                continue
            m = re.match(r"\[(\w+)", text)
            if m:
                name = m.group(1)
                tool_counts[name] = tool_counts.get(name, 0) + 1

    tool_summary = ", ".join(f"{v}x {k}" for k, v in sorted(tool_counts.items(), key=lambda x: -x[1]))
    return {
        "user_turns": user_turns,
        "assistant_turns": assistant_turns,
        "tool_calls": tool_calls,
        "tool_summary": tool_summary or "none",
    }


def extract_one_liner(events):
    """Extract a short one-line outcome from the last assistant message."""
    last_assistant = next((e["text"] for e in reversed(events) if e["role"] == "assistant"), "")
    one_liner = last_assistant[:120].replace("\n", " ").strip()
    if len(last_assistant) > 120:
        one_liner += "..."
    return one_liner or "(no output)"


# ---------------------------------------------------------------------------
# Transcript compaction (AI — removes failed attempts)
# ---------------------------------------------------------------------------

MINIMAX_BASE_URL = "https://api.minimax.io/anthropic"
MINIMAX_MODEL = "MiniMax-M2.5"


def compact_transcript(events, session_id: str) -> str | None:
    """Use MiniMax API to produce a compact markdown summary, removing failed attempts."""
    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        print("[warn] MINIMAX_API_KEY not set — skipping compaction", file=sys.stderr)
        return None

    transcript = render_transcript(events)
    system_prompt = (
        "You are a session historian for an AI coding agent. "
        "Summarize the transcript into structured markdown. "
        "IMPORTANT: Remove failed implementation attempts — only describe the working approach "
        "that was actually completed or finalized. Remove retry loops, abandoned paths, and "
        "debugging noise that went nowhere."
    )
    user_content = (
        "Output exactly this structure:\n"
        "## What Was Accomplished\n"
        "<3-5 bullet points of concrete outcomes>\n\n"
        "## Files Changed\n"
        "<list of files modified or created, one per line>\n\n"
        "## Approach\n"
        "<2-3 sentences describing the method that actually worked>\n\n"
        f"Session transcript:\n{transcript[:6000]}"
    )
    try:
        import urllib.request
        import urllib.error

        payload = json.dumps({
            "model": MINIMAX_MODEL,
            "max_tokens": 1024,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
        }).encode()

        req = urllib.request.Request(
            f"{MINIMAX_BASE_URL}/v1/messages",
            data=payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
            text = "".join(
                block["text"] for block in data.get("content", [])
                if block.get("type") == "text"
            )
            if text.strip():
                return text.strip()
    except Exception as e:
        print(f"[warn] compact_transcript failed: {e}", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Vault path helpers
# ---------------------------------------------------------------------------

def detect_vault(file_path: str) -> str | None:
    """Detect vault name from an absolute file path."""
    for part in Path(file_path).parts:
        if part.lower().endswith("-env"):
            return part  # e.g. "Imperium-ENV"
    return None


def vault_rel_path(file_path: str) -> str | None:
    """Get the vault-relative path from an absolute path."""
    parts = Path(file_path).parts
    for i, part in enumerate(parts):
        if part.lower().endswith("-env"):
            return str(Path(*parts[i + 1:]))
    return None


def obsidian_create(vault: str, rel_path: str, content: str) -> bool:
    """Create a note via obsidian CLI."""
    cmd = ["obsidian", f"vault={vault}", "create", f"path={rel_path}", f"content={content}"]
    print(f"[obsidian] create vault={vault} path={rel_path}", file=sys.stderr)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return True
        print(f"[warn] obsidian create failed: {result.stderr}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[warn] obsidian create failed: {e}", file=sys.stderr)
        return False


def obsidian_append(vault: str, rel_path: str, content: str) -> bool:
    """Append to a note via obsidian CLI."""
    cmd = ["obsidian", f"vault={vault}", "append", f"path={rel_path}", f"content={content}"]
    print(f"[obsidian] append vault={vault} path={rel_path}", file=sys.stderr)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return True
        print(f"[warn] obsidian append failed: {result.stderr}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[warn] obsidian append failed: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Transcript file creation
# ---------------------------------------------------------------------------

def write_transcript_file(session_id, events, instance, session_doc, summary):
    """Create a contained transcript file at Mars/Logs/Transcripts/.

    Returns (transcript_filename, one_liner) on success, (None, None) on failure.
    """
    now = datetime.now()
    tab_name = instance.get("tab_name", session_id[:8]) if instance else session_id[:8]
    doc_id = session_doc.get("id") if session_doc else None

    transcript_filename = f"{session_id[:8]}-{now.strftime('%Y%m%d-%H%M')}.md"
    transcript_rel_path = f"Mars/Logs/Transcripts/{transcript_filename}"

    stats = extract_stats(events)
    one_liner = extract_one_liner(events)

    # Build frontmatter + content
    frontmatter = (
        f"---\n"
        f"instance_id: {session_id}\n"
        f"tab_name: {tab_name}\n"
    )
    if doc_id:
        frontmatter += f"session_doc_id: {doc_id}\n"
    frontmatter += (
        f"created: {now.strftime('%Y-%m-%dT%H:%M:%S')}\n"
        f"type: transcript\n"
        f"---\n"
    )

    body = f"\n# Transcript: {tab_name} — {now.strftime('%Y-%m-%d %H:%M')}\n\n"

    # Summary section (MiniMax compacted or fallback)
    if summary:
        body += f"{summary}\n\n"
    else:
        body += f"## Summary\n\n{one_liner}\n\n"

    # Stats section
    body += (
        f"## Stats\n\n"
        f"- Turns: {stats['user_turns']} user / {stats['assistant_turns']} assistant / {stats['tool_calls']} tool calls\n"
        f"- Tools: {stats['tool_summary']}\n"
    )

    content = frontmatter + body

    success = obsidian_create("Imperium-ENV", transcript_rel_path, content)
    if success:
        print(f"[ok] Created transcript: {transcript_rel_path}", file=sys.stderr)
        return transcript_filename, one_liner
    else:
        print(f"[warn] Failed to create transcript file", file=sys.stderr)
        return None, None


# ---------------------------------------------------------------------------
# Wikilink insertion
# ---------------------------------------------------------------------------

def append_wikilink_to_session_doc(session_doc_file_path, transcript_filename, tab_name, one_liner):
    """Append a single wikilink line to the session doc's Activity Log."""
    vault = detect_vault(session_doc_file_path)
    rel_path = vault_rel_path(session_doc_file_path)
    if not vault or not rel_path:
        print(f"[warn] Could not detect vault/path from: {session_doc_file_path}", file=sys.stderr)
        return False

    now = datetime.now()
    ts = now.strftime("%H:%M")
    link_path = f"Mars/Logs/Transcripts/{transcript_filename.replace('.md', '')}"
    wikilink = f"\n- [[{link_path}|{ts} {tab_name}]] — {one_liner}\n"

    return obsidian_append(vault, rel_path, wikilink)


def append_wikilink_to_daily_note(transcript_filename, tab_name):
    """Append a single wikilink line to today's daily note."""
    today = datetime.now().strftime("%Y-%m-%d")
    daily_note_path = f"Terra/Journal/Daily/{today}.md"

    now = datetime.now()
    ts = now.strftime("%H:%M")
    link_path = f"Mars/Logs/Transcripts/{transcript_filename.replace('.md', '')}"
    wikilink = f"\n- [[{link_path}|{ts} {tab_name}]]\n"

    return obsidian_append("Imperium-ENV", daily_note_path, wikilink)


# ---------------------------------------------------------------------------
# Remote JSONL fetch
# ---------------------------------------------------------------------------

def _fetch_remote_jsonl(satellite_url, session_id):
    """Fetch JSONL content from a remote satellite's /files/read endpoint."""
    import urllib.request
    import urllib.parse

    # The JSONL could be under any project subdir — try common patterns
    home_guess = "/home/token" if "66.10.74" in satellite_url else "/Users/tokenclaw"
    claude_dir = f"{home_guess}/.claude/projects"

    # Ask satellite to glob for the file — but satellite only has /files/read.
    # We know the pattern: ~/.claude/projects/*/<session_id>.jsonl
    # Try to find it by listing project dirs first, or just try known project paths.
    # Simpler: use the session's cwd from instance data to guess the project dir.
    # Simplest: try a wildcard-free approach — list projects dir and try each.

    # Strategy: fetch the projects directory listing isn't available via satellite.
    # Instead, encode the glob pattern as the path and let the satellite handle it.
    # Actually, the satellite only reads exact paths. Let's try the most common project paths.
    common_projects = [
        "-mnt-imperium-Imperium-ENV",
        "-mnt-imperium-Pax-ENV",
        "-mnt-civic-Pax-ENV",
        "-home-token",
    ]

    for project in common_projects:
        path = f"{home_guess}/.claude/projects/{project}/{session_id}.jsonl"
        try:
            url = f"{satellite_url}/files/read?path={urllib.parse.quote(path)}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                content = data.get("content", "")
                if content:
                    print(f"[info] Fetched JSONL from satellite: {path}", file=sys.stderr)
                    return content
        except Exception:
            continue

    print(f"[warn] Could not fetch JSONL from satellite for {session_id}", file=sys.stderr)
    return None


def _parse_jsonl_string(content):
    """Parse JSONL from a string (fetched remotely)."""
    lines = []
    for raw in content.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            lines.append(json.loads(raw))
        except json.JSONDecodeError:
            pass
    return lines


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) >= 2:
        session_id = sys.argv[1].strip()
    else:
        try:
            data = json.load(sys.stdin)
            session_id = data.get("session_id", "").strip()
        except Exception:
            session_id = ""

    if not session_id:
        print(f"Usage: {sys.argv[0]} <session-id>", file=sys.stderr)
        sys.exit(1)

    # Resolve instance first — need device_id to know where JSONL lives
    instance = find_instance_for_session(session_id)

    # Find JSONL file — local first, then remote via satellite
    jsonl_path = None
    remote_content = None

    pattern = str(Path.home() / ".claude" / "projects" / "*" / f"{session_id}.jsonl")
    matches = glob.glob(pattern)
    if matches:
        jsonl_path = matches[0]
        print(f"[info] Found JSONL locally: {jsonl_path}", file=sys.stderr)
    elif instance:
        device_id = instance.get("device_id", LOCAL_DEVICE)
        satellite_url = SATELLITE_URLS.get(device_id)
        if satellite_url:
            print(f"[info] JSONL not local, fetching from {device_id} satellite...", file=sys.stderr)
            remote_content = _fetch_remote_jsonl(satellite_url, session_id)

    if not jsonl_path and not remote_content:
        print(f"[error] No JSONL found for session: {session_id}", file=sys.stderr)
        sys.exit(1)

    # Parse and clean
    if jsonl_path:
        raw_lines = parse_jsonl(jsonl_path)
    else:
        raw_lines = _parse_jsonl_string(remote_content)
    print(f"[info] Parsed {len(raw_lines)} raw lines", file=sys.stderr)

    events = clean_transcript(raw_lines)
    print(f"[info] Cleaned to {len(events)} events", file=sys.stderr)

    transcript = render_transcript(events)
    tokens_est = estimate_tokens(transcript)
    print(f"[info] Transcript ~{tokens_est} tokens ({len(transcript)} chars)", file=sys.stderr)

    # Mutex: cron instances must be explicitly marked stopped
    if instance and str(instance.get("spawner", "")).startswith("cron:"):
        mark_cron_instance_stopped(session_id)

    tab_name = instance.get("tab_name", session_id[:8]) if instance else session_id[:8]

    # Skip trivial sessions (< 3 events = no real work)
    if len(events) < 3:
        print(f"[info] Trivial session ({len(events)} events) — skipping transcript", file=sys.stderr)
        return

    # Compact via MiniMax (only for substantial sessions)
    summary = None
    if tokens_est > 100:
        print(f"[info] Compacting transcript via MiniMax...", file=sys.stderr)
        summary = compact_transcript(events, session_id)

    # Resolve session doc
    session_doc = None
    if instance:
        doc_id = instance.get("session_doc_id")
        if doc_id:
            session_doc = fetch_session_doc(doc_id)

    # Write transcript file
    transcript_filename, one_liner = write_transcript_file(
        session_id, events, instance, session_doc, summary
    )
    if not transcript_filename:
        print(f"[warn] Failed to write transcript file — aborting", file=sys.stderr)
        return

    # Append wikilink to the right target
    if session_doc:
        file_path = session_doc.get("file_path", "")
        print(f"[info] Linking transcript to session doc: {file_path}", file=sys.stderr)
        success = append_wikilink_to_session_doc(file_path, transcript_filename, tab_name, one_liner)
        if success:
            print(f"[ok] Wikilink appended to session doc", file=sys.stderr)
        else:
            print(f"[warn] Failed to append wikilink to session doc", file=sys.stderr)
    else:
        print(f"[info] No session doc — linking transcript to daily note", file=sys.stderr)
        success = append_wikilink_to_daily_note(transcript_filename, tab_name)
        if success:
            print(f"[ok] Wikilink appended to daily note", file=sys.stderr)
        else:
            print(f"[warn] Failed to append wikilink to daily note", file=sys.stderr)


if __name__ == "__main__":
    main()
