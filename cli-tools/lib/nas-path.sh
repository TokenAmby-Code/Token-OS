#!/usr/bin/env bash
# nas-path.sh — Centralized machine identity and config for all Imperium scripts
#
# Exports:
#   IMPERIUM_MACHINE — Machine identifier: mac, wsl, phone, k12-personal, k12-work, linux
#   IMPERIUM         — Root of the Imperium NAS share
#   CIVIC            — Root of the Civic NAS share
#   TOKEN_OS         — Token-OS runtime checkout (machine-local when available)
#   CLI_TOOLS        — CLI tools directory ($TOKEN_OS/cli-tools)
#   TOKEN_API_URL    — Token-API base URL (localhost on mac, tailscale elsewhere)
#   TMUXCTLD_URL     — tmuxctld loopback base URL (Mac-local daemon)
#
# Functions:
#   imperium_cfg <key>  — Look up machine-specific config value
#
# Usage in shell scripts:
#   source "$(dirname "$(readlink -f "$0")")/../lib/nas-path.sh"
#   # or, since cli-tools/bin is in PATH:
#   source "$(command -v nas-path.sh 2>/dev/null || echo "${BASH_SOURCE[0]%/bin/*}/lib/nas-path.sh")"
#
# Usage in .zshrc / .bashrc (sets env vars for all child processes):
#   source /path/to/cli-tools/lib/nas-path.sh
#
# For Python, see: cli-tools/lib/nas_path_env.py (or just read env vars)

# Skip if already fully resolved (idempotent sourcing)
# Check for the function, not just the env var — .zshenv may set IMPERIUM_MACHINE
# without defining imperium_cfg.
# Also require CLI_TOOLS to point at a real dir: a shell carrying a stale path
# (e.g. the archived legacy checkout) must NOT short-circuit — it has to re-derive
# so it self-heals instead of propagating the dead path into the offline cache.
if [[ -n "${IMPERIUM_MACHINE:-}" ]] && type imperium_cfg &>/dev/null && [[ -d "${CLI_TOOLS:-/nonexistent}" ]]; then
    return 0 2>/dev/null || true
fi

# ============================================================
# MACHINE IDENTITY — the one unavoidable detection
# ============================================================
if [[ "$(uname)" == "Darwin" ]]; then
    export IMPERIUM_MACHINE="mac"
elif [[ -d "/data/data/com.termux" ]]; then
    export IMPERIUM_MACHINE="phone"
elif [[ "$(uname -r)" == *microsoft* ]]; then
    export IMPERIUM_MACHINE="wsl"
else
    # Generic Linux: distinguish the K12 boxes by hostname so the personal/work
    # split is nameable for routing, TTS chains, and enforcement scoping. Any
    # other Linux node stays the generic "linux" fallback (never silently
    # inheriting k12-conditioned behavior).
    _imperium_host="$(hostname -s 2>/dev/null || hostname 2>/dev/null)"
    # Strip any domain suffix: a fallback `hostname` (where -s is unsupported)
    # may return a dotted FQDN. Mirrors platform.node().split(".")[0] in Python.
    _imperium_host="${_imperium_host%%.*}"
    case "$_imperium_host" in
        k12-personal) export IMPERIUM_MACHINE="k12-personal" ;;
        k12-work)     export IMPERIUM_MACHINE="k12-work" ;;
        *)            export IMPERIUM_MACHINE="linux" ;;
    esac
    unset _imperium_host
fi

# ============================================================
# MACHINE CONFIG REGISTRY
# ============================================================
# All machine-specific values live here. Add new fields as needed.
# Format: _IMPERIUM_CFG_<MACHINE>_<KEY>="value"
#
# Keys:
#   nas_imperium  — NAS mount path for Imperium share
#   nas_civic     — NAS mount path for Civic share
#   tailscale_ip  — This machine's Tailscale IP
#   token_api_url — How this machine reaches Token-API
#   tmuxctld_url  — How tmux hooks reach the loopback tmuxctld daemon
#   ssh_alias     — SSH config host alias for this machine
#   device_name   — Canonical device name (matches Token-API DEVICE_IPS)
#   shell         — Default interactive shell (zsh/bash)
#   token_os_runtime — Preferred machine-local Token-OS runtime checkout

# Token-API host — the single tailnet node currently serving Token-API (the mac
# today; migrates to k12-personal at cutover). Hoisted once so satellite rows
# don't each embed the literal IP. Machines that run their OWN local Token-API
# (mac, k12-personal) point at localhost instead of this host.
_IMPERIUM_TOKEN_API_HOST="100.95.109.23"

# --- Mac Mini ---
_IMPERIUM_CFG_mac_nas_imperium="/Volumes/Imperium"
_IMPERIUM_CFG_mac_nas_civic="/Volumes/Civic"
_IMPERIUM_CFG_mac_tailscale_ip="100.95.109.23"
_IMPERIUM_CFG_mac_token_api_url="http://localhost:7777"
_IMPERIUM_CFG_mac_tmuxctld_url="http://127.0.0.1:7778"
_IMPERIUM_CFG_mac_ssh_alias="mini"
_IMPERIUM_CFG_mac_device_name="Mac-Mini"
_IMPERIUM_CFG_mac_shell="zsh"
_IMPERIUM_CFG_mac_token_os_runtime="$HOME/runtimes/Token-OS/live"

# --- WSL (Ubuntu on Windows PC) ---
_IMPERIUM_CFG_wsl_nas_imperium="/mnt/imperium"
_IMPERIUM_CFG_wsl_nas_civic="/mnt/civic"
_IMPERIUM_CFG_wsl_tailscale_ip="100.66.10.74"
_IMPERIUM_CFG_wsl_token_api_url="http://${_IMPERIUM_TOKEN_API_HOST}:7777"
_IMPERIUM_CFG_wsl_tmuxctld_url="http://127.0.0.1:7778"
_IMPERIUM_CFG_wsl_ssh_alias="wsl"
_IMPERIUM_CFG_wsl_device_name="TokenPC"
_IMPERIUM_CFG_wsl_shell="bash"
_IMPERIUM_CFG_wsl_token_os_runtime="/home/token/runtimes/token-os/live"

# --- Phone (Termux) ---
_IMPERIUM_CFG_phone_nas_imperium=""
_IMPERIUM_CFG_phone_nas_civic=""
_IMPERIUM_CFG_phone_tailscale_ip="100.102.92.24"
_IMPERIUM_CFG_phone_token_api_url="http://${_IMPERIUM_TOKEN_API_HOST}:7777"
_IMPERIUM_CFG_phone_tmuxctld_url="http://127.0.0.1:7778"
_IMPERIUM_CFG_phone_ssh_alias="phone"
_IMPERIUM_CFG_phone_device_name="Token-S24"
_IMPERIUM_CFG_phone_shell="bash"
_IMPERIUM_CFG_phone_token_os_runtime=""

# --- Linux fallback ---
_IMPERIUM_CFG_linux_nas_imperium="/mnt/imperium"
_IMPERIUM_CFG_linux_nas_civic="/mnt/civic"
_IMPERIUM_CFG_linux_tailscale_ip=""
_IMPERIUM_CFG_linux_token_api_url="http://${_IMPERIUM_TOKEN_API_HOST}:7777"
_IMPERIUM_CFG_linux_tmuxctld_url="http://127.0.0.1:7778"
_IMPERIUM_CFG_linux_ssh_alias=""
_IMPERIUM_CFG_linux_device_name=""
_IMPERIUM_CFG_linux_shell="bash"
_IMPERIUM_CFG_linux_token_os_runtime="/home/token/runtimes/token-os/live"

# --- K12 personal (GMKtec K12; Imperium domain — replaces the Mac Mini) ---
# NOTE: IMPERIUM_MACHINE is the hyphenated public id "k12-personal", but bash
# variable names cannot contain hyphens, so the registry suffix uses underscores
# ("k12_personal"). imperium_cfg maps hyphens→underscores before the lookup.
# Runs its OWN local Token-API (docket: per-box registry pre-cutover) and is the
# long-term Token-API home, so token_api_url is localhost. Civic is NOT mounted
# here: the personal/work boundary is physical — cross-mounting is prohibited.
_IMPERIUM_CFG_k12_personal_nas_imperium="/mnt/imperium"
_IMPERIUM_CFG_k12_personal_nas_civic=""
_IMPERIUM_CFG_k12_personal_tailscale_ip="100.113.115.32"
_IMPERIUM_CFG_k12_personal_token_api_url="http://localhost:7777"
_IMPERIUM_CFG_k12_personal_tmuxctld_url="http://127.0.0.1:7778"
_IMPERIUM_CFG_k12_personal_ssh_alias="k12-personal"
_IMPERIUM_CFG_k12_personal_device_name="K12-Personal"
_IMPERIUM_CFG_k12_personal_shell="bash"
_IMPERIUM_CFG_k12_personal_token_os_runtime="$HOME/runtimes/token-os/live"

# --- K12 work (GMKtec K12; Civic/Pax domain — first physical CIVIC_MACHINE) ---
# In the Imperium registry only to be nameable for routing/enforcement scoping;
# civic-specific config lives in Pax-ENV. Imperium is NOT mounted on the work box
# (boundary), and it runs no Token-OS runtime, so those fields are empty.
_IMPERIUM_CFG_k12_work_nas_imperium=""
_IMPERIUM_CFG_k12_work_nas_civic="/mnt/civic"
_IMPERIUM_CFG_k12_work_tailscale_ip="100.67.168.105"
_IMPERIUM_CFG_k12_work_token_api_url="http://${_IMPERIUM_TOKEN_API_HOST}:7777"
_IMPERIUM_CFG_k12_work_tmuxctld_url="http://127.0.0.1:7778"
_IMPERIUM_CFG_k12_work_ssh_alias="k12-work"
_IMPERIUM_CFG_k12_work_device_name="K12-Work"
_IMPERIUM_CFG_k12_work_shell="bash"
_IMPERIUM_CFG_k12_work_token_os_runtime=""

# ============================================================
# CONFIG LOOKUP FUNCTION
# ============================================================
# Usage: imperium_cfg <key> [machine]
#   imperium_cfg nas_imperium        → value for current machine
#   imperium_cfg tailscale_ip wsl    → value for wsl specifically
#
# Portable across bash and zsh (no ${!var} or ${(P)var}).
imperium_cfg() {
    local key="$1"
    local machine="${2:-$IMPERIUM_MACHINE}"
    # Registry suffixes use underscores; public machine ids may be hyphenated
    # (e.g. k12-personal). Normalize so the variable name is a valid identifier.
    machine="${machine//-/_}"
    local var="_IMPERIUM_CFG_${machine}_${key}"
    eval "echo \"\${${var}}\""
}

# imperium_path_is_quarantined <path> — returns 0 (true) for paths that must
# NEVER win runtime/bare resolution: a Synology recycle bin (#recycle), a macOS
# Trash, or a dated legacy archive (…legacy-YYYYMMDD). These are purge targets;
# binding the runtime or a worktree's bare there silently destroys work when the
# bin is emptied (incident 2026-06-22). Mirrors _is_quarantined in imperium_config.py.
imperium_path_is_quarantined() {
    case "/${1#/}/" in
        *"/#recycle/"*|*"/.Trash/"*|*"/.Trashes/"*) return 0 ;;
        *.legacy-[0-9]*) return 0 ;;
    esac
    return 1
}

# ============================================================
# LEGACY-COMPATIBLE EXPORTS
# ============================================================
export IMPERIUM="$(imperium_cfg nas_imperium)"
export CIVIC="$(imperium_cfg nas_civic)"
# Token-OS now runs from a deploy-owned runtime checkout (protected-main/local-CD).
# Hot runtime execution is machine-local when that checkout exists; $IMPERIUM remains
# the NAS root for vault/archive/exchange and worktree skeletons. Agents edit branch
# worktrees under ~/worktrees/Token-OS/wt-<branch>, never runtime checkouts.
# Unconditional (not ${TOKEN_OS:-...}): long-lived tmux/launchd parents may export a
# stale legacy TOKEN_OS, and this is the one canonical derivation — it must override.
_token_os_runtime="$(imperium_cfg token_os_runtime)"
if [[ -n "$_token_os_runtime" && -d "$_token_os_runtime" ]] \
        && ! imperium_path_is_quarantined "$_token_os_runtime"; then
    export TOKEN_OS="$_token_os_runtime"
else
    export TOKEN_OS="$IMPERIUM/runtimes/token-os/live"
fi
unset _token_os_runtime
export CLI_TOOLS="$TOKEN_OS/cli-tools"
export TOKEN_API_URL="${TOKEN_API_URL:-$(imperium_cfg token_api_url)}"
export TMUXCTLD_URL="${TMUXCTLD_URL:-$(imperium_cfg tmuxctld_url)}"
