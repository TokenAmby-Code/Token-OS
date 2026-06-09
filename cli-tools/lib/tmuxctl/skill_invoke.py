from __future__ import annotations

import subprocess
import time

from .api import fetch_instance_registry
from .tmux_adapter import TmuxAdapter


def normalize_agent(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if raw in {"codex", "openai"}:
        return "codex"
    if raw in {"claude", "claude-code", "anthropic"}:
        return "claude"
    return "auto"


def skill_invocation_leader(agent: str | None) -> str:
    return "$" if normalize_agent(agent) == "codex" else "/"


def normalize_skill_name(skill: str) -> str:
    name = (skill or "").strip()
    while name.startswith(("/", "$")):
        name = name[1:]
    name = name.strip()
    if not name:
        raise ValueError("skill name is empty")
    if any(ch.isspace() for ch in name):
        raise ValueError("skill name must not contain whitespace")
    return name


def skill_invocation_text(
    skill: str,
    agent: str | None,
    arguments: str | None = None,
) -> str:
    prefix = f"{skill_invocation_leader(agent)}{normalize_skill_name(skill)}"
    args = (arguments or "").strip()
    return f"{prefix} {args}" if args else f"{prefix} "


def detect_agent_from_pane_process(adapter: TmuxAdapter, pane: str) -> str:
    try:
        tty = adapter.run(
            "display-message", "-t", pane, "-p", "#{pane_tty}", allow_failure=True
        ).strip()
    except Exception:
        return "auto"
    if not tty:
        return "auto"
    tty_name = tty.removeprefix("/dev/")
    try:
        proc = subprocess.run(
            ["ps", "-t", tty_name, "-o", "command="],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return "auto"
    if proc.returncode != 0:
        return "auto"
    text = proc.stdout.lower()
    if "codex" in text or "@openai/codex" in text:
        return "codex"
    if "claude" in text:
        return "claude"
    return "auto"


def resolve_agent_for_pane(
    adapter: TmuxAdapter,
    pane: str,
    requested: str = "auto",
    *,
    default: str = "claude",
) -> str:
    normalized_default = normalize_agent(default)
    if normalized_default == "auto" and (default or "").strip().lower() != "auto":
        raise ValueError("default must be one of: claude, codex, or auto")

    explicit = normalize_agent(requested)
    if explicit != "auto":
        return explicit

    if pane == "current":
        resolved_pane = adapter.run("display-message", "-p", "#{pane_id}").strip()
    elif pane.startswith("%"):
        resolved_pane = pane
    else:
        resolved_pane = (
            adapter.run(
                "display-message", "-t", pane, "-p", "#{pane_id}", allow_failure=True
            ).strip()
            or pane
        )

    try:
        registry = fetch_instance_registry()
        stopped_match = ""
        for inst in registry.instances:
            if inst.tmux_pane != resolved_pane or not inst.engine:
                continue
            agent = normalize_agent(inst.engine)
            if getattr(inst.status, "value", inst.status) != "stopped":
                return agent
            stopped_match = stopped_match or agent
    except Exception:
        stopped_match = ""

    process_agent = detect_agent_from_pane_process(adapter, resolved_pane)
    if process_agent != "auto":
        return process_agent

    if stopped_match:
        return stopped_match

    hinted = adapter.show_pane_option(resolved_pane, "@PLANNING_AGENT")
    hinted_agent = normalize_agent(hinted)
    if hinted_agent != "auto":
        return hinted_agent
    return normalized_default


def insert_at_prompt_start(
    adapter: TmuxAdapter, pane: str, text: str, *, settle_seconds: float = 0.05
) -> None:
    for _ in range(50):
        adapter.send_keys(pane, "PgUp")
    adapter.send_keys(pane, "Home")
    if settle_seconds > 0:
        time.sleep(settle_seconds)
    adapter.run("send-keys", "-t", pane, "-l", text)
    if settle_seconds > 0:
        time.sleep(settle_seconds)
    for _ in range(50):
        adapter.send_keys(pane, "PgDn")
    adapter.send_keys(pane, "End")


def invoke_skill_in_pane(
    adapter: TmuxAdapter,
    pane: str,
    skill: str,
    *,
    agent: str = "auto",
    arguments: str | None = None,
    settle_seconds: float = 0.05,
) -> str:
    resolved_agent = resolve_agent_for_pane(adapter, pane, agent)
    text = skill_invocation_text(skill, resolved_agent, arguments)
    insert_at_prompt_start(adapter, pane, text, settle_seconds=settle_seconds)
    return text


def send_skill_invocation_to_pane(
    adapter: TmuxAdapter,
    pane: str,
    skill: str,
    *,
    agent: str = "auto",
    arguments: str | None = None,
    clear_prompt: bool = False,
) -> str:
    """Build a harness-correct skill invocation, send it, and submit it.

    This is the generic automation primitive for systems that need to wake an
    agent with a skill rather than prose instructions. Target resolution and the
    universal send gate stay inside ``TmuxAdapter.send_text_then_submit``.
    """
    resolved_agent = resolve_agent_for_pane(adapter, pane, agent)
    text = skill_invocation_text(skill, resolved_agent, arguments)
    adapter.send_text_then_submit(pane, text, clear_prompt=clear_prompt)
    return text
