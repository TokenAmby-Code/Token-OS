#!/usr/bin/env bash
# Report-only PreToolUse detector for direct managed-vault mutations.
# Phase 2 deliberately does not block; it records an actionable remediation so
# we can measure false positives before enforcing the boundary.
set -uo pipefail
payload="$(cat 2>/dev/null || true)"
case "$payload" in
  *Imperium-ENV*|*Pax-ENV*|*-Logs*) ;;
  *) exit 0 ;;
esac
case "$payload" in
  *'>'*|*sed*'-i'*|*perl*'-i'*|*\ cp\ *|*\ mv\ *|*\ rm\ *|*python*|*node*) ;;
  *) exit 0 ;;
esac
log_dir="${AGENT_HOOK_LOG_DIR:-${HOME}/.claude/logs}"
mkdir -p "$log_dir" 2>/dev/null || true
printf '[%s] report-only managed-vault mutation candidate. Remediation: use `obsidian session-docs …` or the relevant Token API `/api/session-docs` endpoint. payload=%s\n' \
  "$(date '+%Y-%m-%d %H:%M:%S')" "$payload" >> "$log_dir/managed-vault-write-report.log" 2>/dev/null || true
exit 0
