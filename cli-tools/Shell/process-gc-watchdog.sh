#!/usr/bin/env bash
# process-gc-watchdog - reap orphaned, egregiously-stuck search processes.
# TAGS: claw, watchdog, tmux-lag
# AUDIENCE: agent, human
#
# Workstream C of the tmux lag remediation. When a turn is interrupted mid-flight
# (the #117 status-line auto mode-toggle loop did this for hours), its in-flight
# ugrep/rg/find children reparent to launchd (ppid=1) and can spin at 150-170% CPU
# indefinitely with nothing to reap them. This is a LOOSE garbage collector: it
# kills ONLY processes that are simultaneously orphaned, on a search-command
# allowlist, AND egregiously stuck (high CPU + long-lived).
#
# Safety model (conservative by construction):
#   - ppid==1 is the "not active work" guarantee: a search spawned by a live agent
#     turn has its bash/claude as parent. Only a search whose spawning turn DIED
#     reparents to launchd. So this never touches a search a running agent awaits.
#   - Positive allowlist of command basenames ONLY (GC_PATTERN). This inherently
#     excludes every long-lived daemon: node (Discord daemon, ppid=1), python/uvicorn
#     (token-api, ppid=1), tmux, claude, codex, Obsidian. Build tools (node/tsc/
#     webpack/vite/esbuild) are deliberately NOT in the default set — see the
#     commented opt-in below — because they overlap with long-lived dev servers.
#   - BOTH gates must hold: %CPU > GC_CPU_MIN AND elapsed > GC_ETIME_MIN minutes.
#     The incident was 150-170% CPU for hours; this catches that class and ignores
#     brief or idle processes.
#
# All thresholds + the pattern are env-overridable (set in the plist) so tuning
# needs no code edit. There is no `timeout`/`gtimeout` on this Mac (confirmed
# absent) and the script does no unbounded work, so nothing is wrapped.
#
# Invoked by launchd every 300s via ai.openclaw.process-gc-watchdog.plist.
#
# DEPLOYMENT: this file is the canonical (version-controlled) copy. launchd cannot
# read/exec a script off the SMB/NAS mount (macOS Sequoia TCC network-volume gate →
# exit 126), so the plist runs a LOCAL copy at ~/.local/bin/process-gc-watchdog
# (same pattern as tokenapi-watchdog). Refresh that copy from here on any change —
# see the install block in the plist. The script touches the NAS not at all at
# runtime, so the local copy is fully self-contained.

set -u

LOG_FILE="$HOME/.claude/process-gc-watchdog.log"

# --- Tunables (env-overridable via the plist) -------------------------------
GC_CPU_MIN="${GC_CPU_MIN:-40}"       # integer %CPU; reap only above this
GC_ETIME_MIN="${GC_ETIME_MIN:-30}"   # minutes alive; reap only above this
# Positive allowlist of command basenames. Anchored full-match (see GC_RE).
GC_PATTERN="${GC_PATTERN:-ugrep|rg|ripgrep|grep|egrep|fgrep|find|mdfind}"
GC_EMIT_EVENTS="${GC_EMIT_EVENTS:-0}"  # 1 = best-effort POST reaps to Token-API
# OPT-IN (NOT default) — also reap orphaned build tools. DANGEROUS: these names
# overlap with long-lived dev servers (vite/webpack dev server, `tsc --watch`),
# and the allowlist is positive-match only, so the safe default is simply to omit
# them. To enable, set GC_PATTERN in the plist to ADD them while keeping the
# daemons (node/python/uvicorn/uv) OUT — e.g.:
#   GC_PATTERN='ugrep|rg|ripgrep|grep|egrep|fgrep|find|mdfind|tsc|webpack|vite|esbuild'
# Never add node/python/uvicorn/uv: the Discord daemon (node .../daemon.js) and
# token-api (python/uvicorn) both run with ppid=1 and would be killed.

GC_RE="^(${GC_PATTERN})$"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"
}

# Parse BSD `etime` (macOS): [[DD-]HH:]MM:SS -> total seconds.
etime_to_secs() {
  local e="${1:-}" days=0 rest a=0 b=0 c="" hh=0 mm=0 ss=0
  [[ -z "$e" ]] && { echo 0; return; }
  if [[ "$e" == *-* ]]; then days="${e%%-*}"; rest="${e#*-}"; else rest="$e"; fi
  local IFS=:
  read -r a b c <<< "$rest"
  if [[ -n "${c:-}" ]]; then hh="${a:-0}"; mm="${b:-0}"; ss="${c:-0}"
  else hh=0; mm="${a:-0}"; ss="${b:-0}"; fi
  # 10# forces base-10 so leading-zero fields (08, 09) are not read as octal.
  echo $(( 10#${days:-0}*86400 + 10#${hh:-0}*3600 + 10#${mm:-0}*60 + 10#${ss:-0} ))
}

# Best-effort surface a reap in the Token-API events table (guarded, silent).
emit_event() {
  [[ "$GC_EMIT_EVENTS" == "1" ]] || return 0
  local pid="$1" cpu="$2" etime="$3" cmd="$4" payload
  payload=$(printf '{"event_type":"process_gc_reap","details":{"pid":%s,"cpu":"%s","etime":"%s","cmd":"%s","source":"process-gc-watchdog"}}' \
    "$pid" "$cpu" "$etime" "$cmd")
  curl -fsS -m 2 -X POST -H 'Content-Type: application/json' \
    -d "$payload" "http://localhost:7777/api/events/log" >/dev/null 2>&1 || true
}

# One ps snapshot. Columns: pid ppid %cpu etime comm. `comm` is the executable
# path (basename stripped below); using process substitution (not a pipe) keeps
# the counters in this shell so the summary line is accurate.
scanned=0
reaped=0
while read -r pid ppid cpu etime comm; do
  [[ "$ppid" == "1" ]] || continue
  base="${comm##*/}"
  [[ "$base" =~ $GC_RE ]] || continue
  scanned=$((scanned + 1))

  secs=$(etime_to_secs "$etime")
  cpu_int="${cpu%.*}"; cpu_int="${cpu_int:-0}"
  (( secs > GC_ETIME_MIN * 60 && 10#$cpu_int > GC_CPU_MIN )) || continue

  log "REAP pid=$pid cpu=$cpu etime=$etime cmd=$base"
  kill -TERM "$pid" 2>/dev/null || true
  sleep 2
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
  fi
  reaped=$((reaped + 1))
  emit_event "$pid" "$cpu" "$etime" "$base"
done < <(ps -Ao pid=,ppid=,%cpu=,etime=,comm= 2>/dev/null)

log "scanned=$scanned reaped=$reaped"
