"""Prompt templates for followup agents."""

from __future__ import annotations

MINIMAX_TEMPLATE = """\
You are a followup agent. Execute this task, then stop.

TASK: {task}

INSTRUCTIONS:
1. Complete the task described above
2. Log your result to memory/followup_log.md in this format:
   [{timestamp}] FOLLOWUP: {name} | RESULT: <what you did and found>
3. Be concise but thorough

{expiry_notice}\
RULES:
- Do NOT delete any cron jobs
- Do NOT modify openclaw.json
- Do NOT restart the gateway
- Keep your work focused on the task above"""

CC_TEMPLATE = """\
You are a followup agent. Your task requires code implementation via Claude Code.

TASK: {task}

INSTRUCTIONS:
1. Implement the task directly — you ARE Claude Code
2. Log your result to memory/followup_log.md in this format:
   [{timestamp}] FOLLOWUP: {name} | ROUTE: claude-code | RESULT: <what was implemented>
3. If the implementation fails, log the error and reason

{expiry_notice}\
RULES:
- Do NOT delete any cron jobs
- Do NOT modify openclaw.json
- Do NOT restart the gateway
- Do NOT use claude -p to delegate — implement directly"""

EXPIRY_NOTICE = """\
EXPIRY: This recurring job expires after {expires}. When the expiry condition is met,
disable yourself by running: openclaw cron disable --name "{name}"
Check the current time and your creation context to determine if expired.

"""


def build_prompt(
    task: str,
    name: str,
    route: str = "minimax",
    expires: str | None = None,
) -> str:
    """Build an agent prompt with the appropriate routing template."""
    expiry_notice = ""
    if expires:
        expiry_notice = EXPIRY_NOTICE.format(expires=expires, name=name)

    escaped_task = task.replace('"', '\\"')

    if route == "cc":
        return CC_TEMPLATE.format(
            task=task,
            escaped_task=escaped_task,
            name=name,
            timestamp="YYYY-MM-DD HH:MM:SS",
            expiry_notice=expiry_notice,
        )
    else:
        return MINIMAX_TEMPLATE.format(
            task=task,
            name=name,
            timestamp="YYYY-MM-DD HH:MM:SS",
            expiry_notice=expiry_notice,
        )
