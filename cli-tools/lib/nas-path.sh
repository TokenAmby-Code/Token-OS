#!/usr/bin/env bash
# nas-path.sh — Centralized machine identity and config for all Imperium scripts
#
# Exports:
#   IMPERIUM_MACHINE — Machine identifier: mac, wsl, phone
#   IMPERIUM         — Root of the Imperium NAS share
#   CIVIC            — Root of the Civic NAS share
#   TOKEN_OS         — Token-OS directory ($IMPERIUM/Token-OS)
#   CLI_TOOLS        — CLI tools directory ($TOKEN_OS/cli-tools)
#   TOKEN_API_URL    — Token-API base URL (localhost on mac, tailscale elsewhere)
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
if [[ -n "${IMPERIUM_MACHINE:-}" ]] && type imperium_cfg &>/dev/null; then
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
    export IMPERIUM_MACHINE="linux"
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
#   ssh_alias     — SSH config host alias for this machine
#   device_name   — Canonical device name (matches Token-API DEVICE_IPS)
#   shell         — Default interactive shell (zsh/bash)
#   tmux_layout   — Workspace layout (bridge/grid)

# --- Mac Mini ---
_IMPERIUM_CFG_mac_nas_imperium="/Volumes/Imperium"
_IMPERIUM_CFG_mac_nas_civic="/Volumes/Civic"
_IMPERIUM_CFG_mac_tailscale_ip="100.95.109.23"
_IMPERIUM_CFG_mac_token_api_url="http://localhost:7777"
_IMPERIUM_CFG_mac_ssh_alias="mini"
_IMPERIUM_CFG_mac_device_name="Mac-Mini"
_IMPERIUM_CFG_mac_shell="zsh"
_IMPERIUM_CFG_mac_tmux_layout="bridge"

# --- WSL (Ubuntu on Windows PC) ---
_IMPERIUM_CFG_wsl_nas_imperium="/mnt/imperium"
_IMPERIUM_CFG_wsl_nas_civic="/mnt/civic"
_IMPERIUM_CFG_wsl_tailscale_ip="100.66.10.74"
_IMPERIUM_CFG_wsl_token_api_url="http://100.95.109.23:7777"
_IMPERIUM_CFG_wsl_ssh_alias="wsl"
_IMPERIUM_CFG_wsl_device_name="TokenPC"
_IMPERIUM_CFG_wsl_shell="bash"
_IMPERIUM_CFG_wsl_tmux_layout="grid"

# --- Phone (Termux) ---
_IMPERIUM_CFG_phone_nas_imperium=""
_IMPERIUM_CFG_phone_nas_civic=""
_IMPERIUM_CFG_phone_tailscale_ip="100.102.92.24"
_IMPERIUM_CFG_phone_token_api_url="http://100.95.109.23:7777"
_IMPERIUM_CFG_phone_ssh_alias="phone"
_IMPERIUM_CFG_phone_device_name="Token-S24"
_IMPERIUM_CFG_phone_shell="bash"
_IMPERIUM_CFG_phone_tmux_layout="grid"

# --- Linux fallback ---
_IMPERIUM_CFG_linux_nas_imperium="/mnt/imperium"
_IMPERIUM_CFG_linux_nas_civic="/mnt/civic"
_IMPERIUM_CFG_linux_tailscale_ip=""
_IMPERIUM_CFG_linux_token_api_url="http://100.95.109.23:7777"
_IMPERIUM_CFG_linux_ssh_alias=""
_IMPERIUM_CFG_linux_device_name=""
_IMPERIUM_CFG_linux_shell="bash"
_IMPERIUM_CFG_linux_tmux_layout="grid"

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
    local var="_IMPERIUM_CFG_${machine}_${key}"
    eval "echo \"\${${var}}\""
}

# ============================================================
# LEGACY-COMPATIBLE EXPORTS
# ============================================================
export IMPERIUM="$(imperium_cfg nas_imperium)"
export CIVIC="$(imperium_cfg nas_civic)"
export TOKEN_OS="$IMPERIUM/Token-OS"
export CLI_TOOLS="$TOKEN_OS/cli-tools"
export TOKEN_API_URL="${TOKEN_API_URL:-$(imperium_cfg token_api_url)}"
