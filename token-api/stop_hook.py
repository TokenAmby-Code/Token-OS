#!/usr/bin/env python3
"""
Stop Hook: JSONL session transcript cleaner + session doc writer.

Usage:
    python3 stop_hook.py <session-id>

Reads ~/.claude/projects/*/<session-id>.jsonl, produces a clean transcript,
and appends a blurb to the linked session doc (if any).
"""

import json
import re
import sys
import glob
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path


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
            # Count lines via '→' markers
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

        # Check for a run of consecutive tool events
        j = i + 1
        while j < len(events) and events[j]["role"] == "tool":
            j += 1

        run = events[i:j]

        if len(run) == 1:
            out.append(ev)
            i = j
            continue

        # Group consecutive Edit/Write to same file
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
            # Count how many consecutive same-file ops follow
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

    # Build tool_result lookup: tool_use_id -> content string
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
                # content can be str or list of blocks
                if isinstance(raw_content, list):
                    raw_content = " ".join(
                        b.get("text", b.get("tool_name", ""))
                        for b in raw_content
                        if isinstance(b, dict)
                    )
                tool_results[tid] = raw_content

    events = []
    seen_tool_ids = set()  # deduplicate streaming duplicates

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
                    # tool_result blocks are already captured; skip here

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

                # skip "thinking" blocks

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


# ---------------------------------------------------------------------------
# Token estimation (rough: 1 token ≈ 4 chars)
# ---------------------------------------------------------------------------

def estimate_tokens(text):
    return len(text) // 4


# ---------------------------------------------------------------------------
# Session doc resolution
# ---------------------------------------------------------------------------

def find_instance_for_session(session_id):
    """Return instance dict if found, else None."""
    try:
        result = subprocess.run(
            ["curl", "-s", "http://localhost:7777/api/instances"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None
        instances = json.loads(result.stdout)
        for inst in instances:
            if inst.get("id") == session_id:
                return inst
        return None
    except Exception as e:
        print(f"[warn] Could not fetch instances: {e}", file=sys.stderr)
        return None


def fetch_session_doc(doc_id):
    """Return session doc dict if found, else None."""
    try:
        result = subprocess.run(
            ["curl", "-s", f"http://localhost:7777/api/session-docs/{doc_id}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except Exception as e:
        print(f"[warn] Could not fetch session doc: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Session doc blurb generation
# ---------------------------------------------------------------------------

def summarize_with_guardsman(transcript: str, session_id: str) -> str | None:
    """Call MiniMax via openclaw to summarize the session transcript. Returns summary or None."""
    snippet = transcript[:3000]
    prompt = (
        "You are summarizing a Claude Code session. Write exactly 2-3 sentences describing "
        "what was accomplished. Be specific: mention file names, features built, bugs fixed. "
        "Do not mention tools used or process steps. Just the outcome.\n\n"
        f"Session transcript:\n{snippet}"
    )
    try:
        result = subprocess.run(
            ["openclaw", "agent", "--agent", "main",
             "--session-id", f"stop-hook-summary-{session_id[:8]}",
             "-m", prompt, "--local", "--json"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            text = data.get("payloads", [{}])[0].get("text", "").strip()
            if text:
                return text
    except Exception as e:
        print(f"[warn] guardsman summary failed: {e}", file=sys.stderr)
    return None


def build_blurb(session_id, events, instance):
    """Build a short markdown blurb for the session doc Activity Log."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tab_name = instance.get("tab_name", session_id[:8]) if instance else session_id[:8]

    # Count stats
    user_turns = sum(1 for e in events if e["role"] == "user")
    assistant_turns = sum(1 for e in events if e["role"] == "assistant")
    tool_calls = sum(1 for e in events if e["role"] == "tool")

    # Extract tool breakdown
    tool_counts = {}
    for e in events:
        if e["role"] == "tool":
            text = e["text"]
            # Handle collapsed form: [Nx ToolName: ...]
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

    # Extract first real user message as task description (skip caveat/system noise)
    first_user = ""
    for e in events:
        if e["role"] == "user":
            text = e["text"]
            # Skip injected caveat blocks and very short messages
            if text.startswith("<") or len(text) < 20:
                continue
            first_user = text
            break
    task_desc = first_user[:200].replace("\n", " ").strip()
    if len(first_user) > 200:
        task_desc += "..."

    # Build transcript and try AI summary; fall back to last assistant message
    transcript = render_transcript(events)
    ai_summary = summarize_with_guardsman(transcript, session_id)
    if ai_summary:
        outcome = ai_summary
    else:
        last_assistant = next((e["text"] for e in reversed(events) if e["role"] == "assistant"), "")
        outcome = last_assistant[:300].replace("\n", " ").strip()
        if len(last_assistant) > 300:
            outcome += "..."

    blurb = f"""
### Session: {tab_name} ({session_id[:8]}) — {now}

**Task**: {task_desc}

**Turns**: {user_turns} user / {assistant_turns} assistant / {tool_calls} tool calls
**Tools**: {tool_summary if tool_summary else "none"}

**Outcome**: {outcome if outcome else "(no assistant output)"}

---
""".strip()

    return blurb


# ---------------------------------------------------------------------------
# Obsidian append
# ---------------------------------------------------------------------------

def append_to_session_doc(file_path, blurb):
    """Append blurb to session doc using obsidian CLI."""
    # Detect vault from path
    vault = None
    path_lower = file_path.lower()
    for candidate in ["imperium-env", "token-env", "pax-env", "claw-env", "personal-env"]:
        if candidate in path_lower:
            vault = candidate.replace("-", "-").title().replace("-", "-")
            # Normalize: Imperium-ENV
            vault = candidate
            break

    if not vault:
        print(f"[warn] Could not detect vault from path: {file_path}", file=sys.stderr)
        return False

    # Get relative path within vault
    # file_path is absolute; find vault root
    # Pattern: /Users/tokenclaw/<VaultDir>/<relative>
    # Vault dirs are like Imperium-ENV -> ~/Imperium-ENV
    rel_path = None
    for part in Path(file_path).parts:
        if part.lower().endswith("-env"):
            idx = Path(file_path).parts.index(part)
            rel_path = str(Path(*Path(file_path).parts[idx+1:]))
            break

    if not rel_path:
        print(f"[warn] Could not determine relative vault path from: {file_path}", file=sys.stderr)
        return False

    # Format vault name for obsidian CLI (e.g. Imperium-ENV)
    vault_name = next(
        (p for p in Path(file_path).parts if p.lower().endswith("-env")),
        vault
    )

    cmd = ["obsidian", f"vault={vault_name}", "append", f'path={rel_path}', f"content={blurb}"]
    print(f"[obsidian] vault={vault_name} path={rel_path}", file=sys.stderr)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return True
        else:
            print(f"[warn] obsidian append failed: {result.stderr}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"[warn] obsidian command failed: {e}", file=sys.stderr)
        return False


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

    # Find JSONL file
    pattern = str(Path.home() / ".claude" / "projects" / "*" / f"{session_id}.jsonl")
    matches = glob.glob(pattern)
    if not matches:
        print(f"[error] No JSONL found for session: {session_id}", file=sys.stderr)
        sys.exit(1)

    jsonl_path = matches[0]
    print(f"[info] Found JSONL: {jsonl_path}", file=sys.stderr)

    # Parse and clean
    raw_lines = parse_jsonl(jsonl_path)
    print(f"[info] Parsed {len(raw_lines)} raw lines", file=sys.stderr)

    events = clean_transcript(raw_lines)
    print(f"[info] Cleaned to {len(events)} events", file=sys.stderr)

    transcript = render_transcript(events)
    tokens_est = estimate_tokens(transcript)
    print(f"[info] Transcript ~{tokens_est} tokens ({len(transcript)} chars)", file=sys.stderr)

    # Print transcript to stdout
    print("=" * 70)
    print(f"SESSION TRANSCRIPT: {session_id}")
    print("=" * 70)
    print(transcript)
    print("=" * 70)

    # Resolve session doc
    instance = find_instance_for_session(session_id)
    if not instance:
        print(f"\n[info] No instance found for session {session_id} — skipping session doc", file=sys.stderr)
        return

    doc_id = instance.get("session_doc_id")
    if not doc_id:
        print(f"\n[info] Instance '{instance.get('tab_name')}' has no session doc linked", file=sys.stderr)
        return

    session_doc = fetch_session_doc(doc_id)
    if not session_doc:
        print(f"\n[info] Could not fetch session doc {doc_id}", file=sys.stderr)
        return

    file_path = session_doc.get("file_path", "")
    print(f"\n[info] Session doc: {file_path}", file=sys.stderr)

    # Build blurb
    blurb = build_blurb(session_id, events, instance)
    print("\n" + "=" * 70)
    print("BLURB TO APPEND:")
    print("=" * 70)
    print(blurb)
    print("=" * 70)

    # Append to session doc
    success = append_to_session_doc(file_path, blurb)
    if success:
        print(f"\n[ok] Appended blurb to session doc: {file_path}", file=sys.stderr)
    else:
        print(f"\n[warn] Failed to append blurb — check obsidian CLI", file=sys.stderr)


if __name__ == "__main__":
    main()
