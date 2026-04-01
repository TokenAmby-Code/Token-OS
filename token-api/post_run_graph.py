"""
PostRunGraph: LangGraph StateGraph for post-job orchestration.

Runs after each cron job completes (when guards_count > 0 or followup_delay_seconds is set).

Graph shape:
    check_victory → (victory?) notify_victory : run_guards
    run_guards → aggregate_guards → followup_decision

Victory path: agent emitted ##IMPERIUM_VICTORIOUS: <reason>## → notify Discord, stop chain.
Non-victory path: run N haiku guards in parallel, aggregate findings, then decide on follow-up.
"""

import asyncio
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Optional, TypedDict

import httpx
from langgraph.graph import END, StateGraph

_MINIMAX_BASE_URL = "https://api.minimax.io/anthropic"
_MINIMAX_MODEL = "MiniMax-M2.5"
def _get_minimax_key() -> str:
    """Read MiniMax API key from MINIMAX_API_KEY env var."""
    import os
    key = os.environ.get("MINIMAX_API_KEY")
    if not key:
        raise RuntimeError("MINIMAX_API_KEY environment variable not set")
    return key


# Guard lenses cycle through these validation perspectives
_GUARD_LENSES = [
    "Code correctness: Do the code changes mentioned actually implement what was claimed?",
    "Test coverage: Were tests run or written? Do they cover the changed paths?",
    "Commit integrity: Was a git commit made? Does the commit message match the diff?",
    "Claim verification: What factual claims were made? Can any be falsified from the output?",
    "Anti-spin check: Did the agent spin on the same problem without resolution?",
    "Documentation check: Were docs or logs updated as claimed?",
    "Regression risk: Do any changes risk breaking existing functionality?",
]

_HOME = str(Path.home())


class PostRunState(TypedDict):
    job_id: str
    job_name: str
    cron_run_id: int
    full_output: str
    guards_count: int
    followup_delay_seconds: Optional[int]
    victory_reason: Optional[str]
    guard_results: List[dict]
    followup_scheduled: bool


# ── Nodes ──────────────────────────────────────────────────────


def check_victory_node(state: PostRunState) -> PostRunState:
    """Victory was already detected by cron_engine; this node just passes it through."""
    # victory_reason is pre-populated by cron_engine._execute if found
    return state


async def run_guards_node(state: PostRunState) -> PostRunState:
    """Spawn guards_count haiku validators in parallel."""
    guards_count = state["guards_count"]
    if guards_count == 0:
        return {**state, "guard_results": []}

    job_name = state["job_name"]
    full_output = state["full_output"]
    cron_run_id = state["cron_run_id"]

    minimax_key = _get_minimax_key()

    async def _run_one_guard(index: int) -> dict:
        lens = _GUARD_LENSES[index % len(_GUARD_LENSES)]
        prompt = (
            f"You are an Imperial Guard validator auditing an AI agent's work output.\n\n"
            f"Job: {job_name}\n"
            f"Lens: {lens}\n\n"
            f"--- OUTPUT START ---\n{full_output[:3000]}\n--- OUTPUT END ---\n\n"
            f"Evaluate the output through your assigned lens. Be adversarial — look for gaps, "
            f"unsupported claims, and incomplete work. Respond with:\n"
            f"verdict: valid|concern|invalid\n"
            f"findings: <1-3 sentences>\n"
        )
        start_ms = int(datetime.now().timestamp() * 1000)
        verdict = "concern"
        findings = ""
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(
                    f"{_MINIMAX_BASE_URL}/v1/messages",
                    headers={
                        "x-api-key": minimax_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": _MINIMAX_MODEL,
                        "max_tokens": 256,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                # Extract text from Anthropic-compatible response
                output = ""
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        output += block["text"]

            # Parse verdict/findings lines
            for line in output.splitlines():
                line = line.strip()
                if line.lower().startswith("verdict:"):
                    v = line.split(":", 1)[1].strip().lower()
                    if v in ("valid", "concern", "invalid"):
                        verdict = v
                elif line.lower().startswith("findings:"):
                    findings = line.split(":", 1)[1].strip()
        except asyncio.TimeoutError:
            verdict = "concern"
            findings = "Guard timed out after 90s"
        except Exception as e:
            verdict = "concern"
            findings = f"Guard error: {e}"

        duration_ms = int(datetime.now().timestamp() * 1000) - start_ms
        return {
            "guard_index": index,
            "verdict": verdict,
            "findings": findings,
            "lens": lens,
            "duration_ms": duration_ms,
        }

    results = await asyncio.gather(*[_run_one_guard(i) for i in range(guards_count)])
    return {**state, "guard_results": list(results)}


async def aggregate_guards_node(state: PostRunState) -> PostRunState:
    """Store guard results in DB and post summary to Discord."""
    results = state.get("guard_results", [])
    if not results:
        return state

    cron_run_id = state["cron_run_id"]
    job_id = state["job_id"]
    job_name = state["job_name"]

    # Store in DB
    try:
        import aiosqlite
        db_path = Path(_HOME) / ".claude" / "agents.db"
        async with aiosqlite.connect(db_path) as db:
            for r in results:
                await db.execute("""
                    INSERT INTO guard_runs
                        (cron_run_id, job_id, guard_index, verdict, findings, model, duration_ms, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    cron_run_id, job_id, r["guard_index"],
                    r["verdict"], r["findings"],
                    _MINIMAX_MODEL,
                    r["duration_ms"],
                    datetime.now().isoformat(),
                ))
            await db.commit()
    except Exception as e:
        print(f"PostRunGraph: Failed to store guard_runs: {e}")

    # Build Discord summary
    counts = {"valid": 0, "concern": 0, "invalid": 0}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    concern_lines = [
        f"  • {r['findings']}" for r in results
        if r["verdict"] in ("concern", "invalid") and r["findings"]
    ]

    verdict_icons = {
        "valid": "✅",
        "concern": "⚠️",
        "invalid": "❌",
    }
    overall = "invalid" if counts["invalid"] > 0 else ("concern" if counts["concern"] > 0 else "valid")
    icon = verdict_icons[overall]

    lines = [
        f"{icon} **Guard Report** — {job_name}",
        f"✅ {counts['valid']} valid  ⚠️ {counts['concern']} concern  ❌ {counts['invalid']} invalid",
    ]
    if concern_lines:
        lines.append("Concerns:")
        lines.extend(concern_lines[:3])  # cap at 3 to avoid spam

    msg = "\n".join(lines)
    try:
        subprocess.run(
            ["discord", "send", "operations", msg],
            timeout=10,
            env=_subprocess_env(),
        )
    except Exception as e:
        print(f"PostRunGraph: Discord guard summary failed: {e}")

    return state


async def notify_victory_node(state: PostRunState) -> PostRunState:
    """Send Discord victory notification."""
    reason = state.get("victory_reason", "")
    job_name = state["job_name"]
    msg = f"⚔️ **IMPERIUM VICTORIOUS** — {job_name}\n> {reason}"
    try:
        subprocess.run(
            ["discord", "send", "operations", msg],
            timeout=10,
            env=_subprocess_env(),
        )
    except Exception as e:
        print(f"PostRunGraph: Victory notify failed: {e}")
    return state


async def followup_decision_node(state: PostRunState) -> PostRunState:
    """Schedule follow-up run if no victory and followup_delay_seconds is set.

    Note: actual delayed re-trigger is handled in cron_engine._execute to avoid
    circular imports. This node is a no-op hook for future extensions (e.g.,
    LangGraph checkpointing, conditional suppression based on guard results).
    """
    return {**state, "followup_scheduled": bool(state.get("followup_delay_seconds"))}


# ── Graph Assembly ─────────────────────────────────────────────


def _subprocess_env() -> dict:
    import os
    env = dict(os.environ)
    extra = [
        f"{_HOME}/Scripts/cli-tools/bin",
        f"{_HOME}/.local/bin",
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
    ]
    current = env.get("PATH", "")
    for p in reversed(extra):
        if p not in current:
            current = f"{p}:{current}"
    env["PATH"] = current
    env["HOME"] = _HOME
    return env


workflow = StateGraph(PostRunState)
workflow.add_node("check_victory", check_victory_node)
workflow.add_node("run_guards", run_guards_node)
workflow.add_node("aggregate_guards", aggregate_guards_node)
workflow.add_node("followup_decision", followup_decision_node)
workflow.add_node("notify_victory", notify_victory_node)

workflow.set_entry_point("check_victory")
workflow.add_conditional_edges(
    "check_victory",
    lambda s: "notify_victory" if s.get("victory_reason") else "run_guards",
)
workflow.add_edge("notify_victory", END)
workflow.add_edge("run_guards", "aggregate_guards")
workflow.add_edge("aggregate_guards", "followup_decision")
workflow.add_edge("followup_decision", END)

post_run_graph = workflow.compile()
