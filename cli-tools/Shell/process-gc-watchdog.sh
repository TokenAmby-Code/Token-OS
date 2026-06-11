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
# 2026-06-10 tightening (second NAS-ugrep lag incident): the ppid==1 orphan gate
# MISSED six ugreps spinning 3-4h at ~98% CPU on /Volumes/Imperium/runtimes —
# their `zsh -c source ~/.claude/shell-snapshots/...` wrappers stayed alive, so
# they were never orphans. Two rules added; both exploit a hard invariant: the
# Claude Bash/Grep tool call timeout caps at 600s, so NO live agent turn is ever
# awaiting a search older than 10 minutes. Both exempt processes with a
# controlling TTY, so a human's interactive foreground search is never touched.
#
# Rules (a process is reaped if ANY rule matches; allowlist applies to all):
#   A. orphan      — ppid==1            AND cpu > GC_CPU_MIN AND etime > GC_ETIME_MIN
#   B. nas-search  — args match GC_NAS_PATH AND tty=?? AND etime > GC_NAS_ETIME_MIN
#                    (no CPU gate: an SMB-stalled grep can sit in I/O-wait at low
#                    CPU and is just as dead; 10 min >> the 600s tool ceiling)
#   C. ancient     — tty=??             AND cpu > GC_CPU_MIN AND etime > GC_ANCIENT_MIN
#                    (catch-all for non-NAS stuck searches with live wrappers)
#
# Safety model (conservative by construction):
#   - Positive allowlist of command basenames ONLY (GC_PATTERN). This inherently
#     excludes every long-lived daemon: node (Discord daemon, ppid=1), python/uvicorn
#     (token-api, ppid=1), tmux, claude, codex, Obsidian. Build tools (node/tsc/
#     webpack/vite/esbuild) are deliberately NOT in the default set — see the
#     commented opt-in below — because they overlap with long-lived dev servers.
#   - Rules B/C require NO controlling terminal (tty=??): tool-spawned searches
#     run detached; a human's interactive search keeps its tty and is exempt.
#   - Rule thresholds are all env-overridable (set in the plist) so tuning needs
#     no code edit.
#
# Invoked by launchd every 300s via ai.openclaw.process-gc-watchdog.plist.
#
# DEPLOYMENT: this file is the canonical (version-controlled) copy. launchd cannot
# read/exec a script off the SMB/NAS mount (macOS Sequoia TCC network-volume gate →
# exit 126), so the plist runs a LOCAL copy at ~/.local/bin/process-gc-watchdog
# (same pattern as tokenapi-watchdog). Refresh that copy from here on any change —
# see the install block in the plist. The script touches the NAS not at all at
# runtime, so the local copy is fully self-contained.

set -euo pipefail

LOG_FILE="$HOME/.claude/process-gc-watchdog.log"

# Keep our own log bounded (one summary line per 5-min run). launchd's
# stdout/stderr logs stay ~empty, so only this file needs trimming. The
# if-condition keeps this exempt from `set -e`; the mv is atomic, and the
# tmp file is removed on any failure.
if [[ -f "$LOG_FILE" ]] && (( $(wc -l < "$LOG_FILE" 2>/dev/null || echo 0) > 2000 )); then
  if tail -n 1000 "$LOG_FILE" > "$LOG_FILE.tmp" 2>/dev/null; then
    mv -f "$LOG_FILE.tmp" "$LOG_FILE" 2>/dev/null || true
  else
    rm -f "$LOG_FILE.tmp"
  fi
fi

# --- Tunables (env-overridable via the plist) -------------------------------
GC_CPU_MIN="${GC_CPU_MIN:-40}"             # integer %CPU; rules A/C reap only above this
GC_ETIME_MIN="${GC_ETIME_MIN:-30}"         # minutes alive; rule A (orphan)
GC_NAS_PATH="${GC_NAS_PATH:-/Volumes/Imperium}"  # rule B: args substring marking a NAS search
GC_NAS_ETIME_MIN="${GC_NAS_ETIME_MIN:-10}" # minutes alive; rule B (NAS search, tty-less)
GC_ANCIENT_MIN="${GC_ANCIENT_MIN:-60}"     # minutes alive; rule C (any tty-less search)
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
  local pid="$1" cpu="$2" etime="$3" cmd="$4" rule="$5" payload
  payload=$(printf '{"event_type":"process_gc_reap","details":{"pid":%s,"cpu":"%s","etime":"%s","cmd":"%s","rule":"%s","source":"process-gc-watchdog"}}' \
    "$pid" "$cpu" "$etime" "$cmd" "$rule")
  curl -fsS -m 2 -X POST -H 'Content-Type: application/json' \
    -d "$payload" "http://localhost:7777/api/events/log" >/dev/null 2>&1 || true
}

reap() {
  local pid="$1" cpu="$2" etime="$3" base="$4" rule="$5"
  log "REAP rule=$rule pid=$pid cpu=$cpu etime=$etime cmd=$base"
  kill -TERM "$pid" 2>/dev/null || true
  sleep 2
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
  fi
  reaped=$((reaped + 1))
  emit_event "$pid" "$cpu" "$etime" "$base" "$rule"
}

# One ps snapshot. Columns: pid ppid %cpu etime tty comm. `comm` is the executable
# path (basename stripped below); using process substitution (not a pipe) keeps
# the counters in this shell so the summary line is accurate. Full args are
# fetched per-candidate only (allowlist hits are rare), keeping the scan cheap.
scanned=0
reaped=0
while read -r pid ppid cpu etime tty comm; do
  base="${comm##*/}"
  [[ "$base" =~ $GC_RE ]] || continue
  scanned=$((scanned + 1))

  secs=$(etime_to_secs "$etime")
  cpu_int="${cpu%.*}"; cpu_int="${cpu_int:-0}"

  # Rule A: orphan (ppid==1), high CPU, long-lived.
  if [[ "$ppid" == "1" ]] && (( secs > GC_ETIME_MIN * 60 && 10#$cpu_int > GC_CPU_MIN )); then
    reap "$pid" "$cpu" "$etime" "$base" orphan
    continue
  fi

  # Rules B/C apply only to tty-less processes (tool-spawned, not interactive).
  [[ "$tty" == "??" ]] || continue

  # Rule B: NAS search past the tool-timeout ceiling — dead by construction,
  # regardless of parent liveness or CPU (SMB stalls park in I/O-wait).
  if (( secs > GC_NAS_ETIME_MIN * 60 )); then
    args=$(ps -ww -o args= -p "$pid" 2>/dev/null || true)
    if [[ "$args" == *"$GC_NAS_PATH"* ]]; then
      reap "$pid" "$cpu" "$etime" "$base" nas-search
      continue
    fi
  fi

  # Rule C: any tty-less search spinning hot for an hour+ (live-wrapper variant
  # of rule A — the 2026-06-10 incident class when the search is NOT on the NAS).
  if (( secs > GC_ANCIENT_MIN * 60 && 10#$cpu_int > GC_CPU_MIN )); then
    reap "$pid" "$cpu" "$etime" "$base" ancient
  fi
done < <(ps -Ao pid=,ppid=,%cpu=,etime=,tty=,comm= 2>/dev/null)

log "scanned=$scanned reaped=$reaped"
