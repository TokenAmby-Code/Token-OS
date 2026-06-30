from __future__ import annotations

import subprocess
import time

from .api import fetch_instance_registry
from .custodes import _process_tree
from .tmux_adapter import TmuxAdapter

SkillSinkKeys = tuple[str, ...]

# An invocation is either a SKILL (engine-specific leader: ``$`` for Codex, ``/``
# for Claude) or a COMMAND (a universal ``/`` leader in every harness). Keeping the
# two kinds explicit is the whole point of the daemon primitive: the Shift+Tab menu
# hands the daemon a bare name + kind, and the leader/sink policy lives in ONE place
# instead of being re-derived in bash per call.
INVOCATION_KINDS = ("skill", "command")
ETHEREAL_COMMANDS = {"claude": "btw", "codex": "side"}


def normalize_invocation_kind(kind: str | None) -> str:
    value = (kind or "skill").strip().lower()
    if value == "":
        return "skill"
    if value not in INVOCATION_KINDS:
        raise ValueError(f"invocation kind must be one of {INVOCATION_KINDS}, got {kind!r}")
    return value


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


def invocation_leader(agent: str | None, *, kind: str = "skill") -> str:
    """The prompt leader for an invocation of ``kind``.

    Commands are universal slash leaders in every harness; only skills take the
    engine-specific leader (``$`` for Codex, ``/`` for Claude).
    """
    if normalize_invocation_kind(kind) == "command":
        return "/"
    return skill_invocation_leader(agent)


def invocation_text(
    name: str,
    agent: str | None,
    *,
    kind: str = "skill",
    arguments: str | None = None,
) -> str:
    prefix = f"{invocation_leader(agent, kind=kind)}{normalize_skill_name(name)}"
    args = (arguments or "").strip()
    return f"{prefix} {args}" if args else f"{prefix} "


def ethereal_invocation_text(agent: str | None, message: str) -> str:
    """Render a side-channel message for the resolved harness.

    The caller supplies only semantic ``kind=ethereal`` text.  tmuxctld owns the
    harness-specific command spelling: Claude gets ``/btw`` and Codex gets
    ``/side``.  Unknown engines fail closed rather than polluting the main
    conversation with a bare message.
    """
    resolved_agent = normalize_agent(agent)
    command = ETHEREAL_COMMANDS.get(resolved_agent)
    if not command:
        raise ValueError("ethereal send requires a resolved claude or codex agent")
    payload = (message or "").strip()
    if not payload:
        raise ValueError("ethereal message is empty")
    return invocation_text(command, "auto", kind="command", arguments=payload)


def invocation_sink_keys(agent: str | None, *, kind: str = "skill") -> SkillSinkKeys:
    """Keys to send after typing an invocation but before any submit.

    A command renders no skill chip, so it must never receive the Codex Tab-sink
    (a stray Tab on a universal slash command would mangle or submit it). Only a
    Codex *skill* needs the sink.
    """
    if normalize_invocation_kind(kind) == "command":
        return ()
    return codex_skill_sink_keys(agent)


def skill_invocation_text(
    skill: str,
    agent: str | None,
    arguments: str | None = None,
) -> str:
    return invocation_text(skill, agent, kind="skill", arguments=arguments)


def codex_skill_sink_keys(agent: str | None) -> SkillSinkKeys:
    """Keys needed after typing a skill invocation but before submit.

    Codex accepts literal ``$skill`` text, but it does not reliably materialize
    the skill chip until Tab/Enter. Tab is lower risk than Enter because it sinks
    the skill without submitting arbitrary prompt text. Claude slash commands do
    not need this and must not receive it.
    """
    return ("Tab",) if normalize_agent(agent) == "codex" else ()


def looks_like_codex_skill_invocation(text: str) -> bool:
    stripped = (text or "").lstrip()
    if not stripped.startswith("$"):
        return False
    parts = stripped[1:].split(None, 1)
    if not parts:
        return False
    head = parts[0]
    return bool(head) and all(ch.isalnum() or ch in {"-", "_"} for ch in head)


def detect_agent_from_pane_process(adapter: TmuxAdapter, pane: str) -> str:
    try:
        pane_pid_raw = adapter.run(
            "display-message", "-t", pane, "-p", "#{pane_pid}", allow_failure=True
        ).strip()
        pane_pid = int(pane_pid_raw) if pane_pid_raw else None
    except Exception:
        pane_pid = None
    if pane_pid:
        children, commands = _process_tree()
        stack = [pane_pid, *children.get(pane_pid, [])]
        seen: set[int] = set()
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            command = commands.get(pid, "")
            if "@openai/codex" in command or "codex" in command:
                return "codex"
            if "claude" in command:
                return "claude"
            stack.extend(children.get(pid, []))

    # Compatibility fallback for platforms/tests where pane_pid is unavailable.
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

    token_api_hint = adapter.show_pane_option(resolved_pane, "@TOKEN_API_ENGINE")
    token_api_agent = normalize_agent(token_api_hint)
    if token_api_agent != "auto":
        return token_api_agent

    if stopped_match:
        return stopped_match

    hinted = adapter.show_pane_option(resolved_pane, "@PLANNING_AGENT")
    hinted_agent = normalize_agent(hinted)
    if hinted_agent != "auto":
        return hinted_agent
    return normalized_default


def move_to_prompt_start(adapter: TmuxAdapter, pane: str, *, page_ups: int = 50) -> None:
    """Drive the pane cursor to the very start of the prompt.

    ``page_ups`` PgUp scrolls a multi-page draft to the top, then Home parks at
    column 0. PgUp-at-top is idempotent, so an overshoot is harmless.  Use tmux's
    own repeat-count (`send-keys -N`) instead of expanding 50 separate argv keys:
    the macro is emitted by tmux as one tight burst, with no sleeps between inputs
    and no large Python argv construction on the hot path.
    """
    count = max(0, int(page_ups))
    if count:
        adapter.run("send-keys", "-N", str(count), "-t", pane, "PgUp", "Home")
    else:
        adapter.send_keys(pane, "Home")


def insert_text(adapter: TmuxAdapter, pane: str, text: str) -> None:
    """Insert literal text at the cursor with a right-side separator buffer.

    Generic prompt-start insertion is usually prepending onto unknown existing
    composer text. Preload a single space then step ``Left`` so a Codex Tab-sink
    (or any prepend onto existing text) can never see a concatenated token like
    ``$preplanexisting``. ``text`` is ``rstrip``-ped because the buffer space is
    the separator — no leader logic, no submit.
    """
    payload = text.rstrip()
    adapter.run("send-keys", "-t", pane, "-l", " ")
    adapter.send_keys(pane, "Left")
    adapter.run("send-keys", "-t", pane, "-l", payload)


def move_to_prompt_end(adapter: TmuxAdapter, pane: str, *, page_downs: int = 50) -> None:
    """Return the pane cursor to the end of the prompt (PgDn x N, then End).

    Uses tmux's repeat-count for the same reason as
    :func:`move_to_prompt_start`: emit one tight macro burst, not a slow series of
    sleeps or independently gated inputs.
    """
    count = max(0, int(page_downs))
    if count:
        adapter.run("send-keys", "-N", str(count), "-t", pane, "PgDn", "End")
    else:
        adapter.send_keys(pane, "End")


def insert_at_prompt_start(
    adapter: TmuxAdapter,
    pane: str,
    text: str,
    *,
    settle_seconds: float = 0.0,
    sink_keys: SkillSinkKeys = (),
) -> None:
    move_to_prompt_start(adapter, pane)
    if settle_seconds > 0:
        time.sleep(settle_seconds)
    insert_text(adapter, pane, text)
    for key in sink_keys:
        adapter.send_keys(pane, key)
    if settle_seconds > 0:
        time.sleep(settle_seconds)
    move_to_prompt_end(adapter, pane)


def insert_invocation_in_pane(
    adapter: TmuxAdapter,
    pane: str,
    name: str,
    *,
    agent: str = "auto",
    kind: str = "skill",
    arguments: str | None = None,
    settle_seconds: float = 0.0,
) -> dict:
    """Engine-agnostic daemon primitive: insert an invocation at the prompt start.

    The caller hands a BARE name plus a ``kind``; this resolves the engine (skills
    only), applies the correct leader, types it at the prompt start, sinks the
    Codex skill chip when needed, and returns the cursor to the prompt end -- the
    whole Shift+Tab insert in one warm in-process call. A command short-circuits
    engine resolution entirely: its leader is universal, so probing the pane would
    only add latency and risk a wrong leader.
    """
    resolved_kind = normalize_invocation_kind(kind)
    if resolved_kind == "command":
        resolved_agent = "auto"
    else:
        resolved_agent = resolve_agent_for_pane(adapter, pane, agent)
    text = invocation_text(name, resolved_agent, kind=resolved_kind, arguments=arguments)
    insert_at_prompt_start(
        adapter,
        pane,
        text,
        settle_seconds=settle_seconds,
        sink_keys=invocation_sink_keys(resolved_agent, kind=resolved_kind),
    )
    return {"pane": pane, "agent": resolved_agent, "kind": resolved_kind, "rendered": text}


def invoke_skill_in_pane(
    adapter: TmuxAdapter,
    pane: str,
    skill: str,
    *,
    agent: str = "auto",
    arguments: str | None = None,
    settle_seconds: float = 0.0,
) -> str:
    return insert_invocation_in_pane(
        adapter,
        pane,
        skill,
        agent=agent,
        kind="skill",
        arguments=arguments,
        settle_seconds=settle_seconds,
    )["rendered"]


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
    return send_invocation_to_pane(
        adapter,
        pane,
        skill,
        agent=agent,
        kind="skill",
        arguments=arguments,
        clear_prompt=clear_prompt,
    )


def send_invocation_to_pane(
    adapter: TmuxAdapter,
    pane: str,
    name: str,
    *,
    agent: str = "auto",
    kind: str = "skill",
    arguments: str | None = None,
    clear_prompt: bool = False,
) -> str:
    """Build a kind-aware invocation, send it, and submit it.

    This is the submit-side sibling of :func:`insert_invocation_in_pane`: callers
    provide a bare name plus ``kind`` and the central renderer owns both the
    leader and any pre-submit sink keys. Commands never resolve an engine and
    never receive the Codex skill-chip Tab sink.
    """
    resolved_kind = normalize_invocation_kind(kind)
    if resolved_kind == "command":
        resolved_agent = "auto"
    else:
        resolved_agent = resolve_agent_for_pane(adapter, pane, agent)
    text = invocation_text(name, resolved_agent, kind=resolved_kind, arguments=arguments)
    adapter.send_text_then_submit(
        pane,
        text,
        clear_prompt=clear_prompt,
        pre_submit_keys=invocation_sink_keys(resolved_agent, kind=resolved_kind),
    )
    return text
