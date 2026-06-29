---
name: network
description: Imperium network and machine-config shorthand. Use when checking Tailscale, SSH aliases, NAS paths, Token-API reachability, phone/Mac/WSL topology, or replacing hardcoded machine values.
---

# Network

Imperium network values are centralized in machine config. Use this skill when a task involves Tailscale, NAS paths, SSH aliases, Token-API reachability, phone/Mac/WSL topology, or replacing hardcoded machine values.

## Surfaces

- Shell config: `${TOKEN_OS:-$HOME/runtimes/Token-OS/live}/cli-tools/lib/nas-path.sh`.
- Python config: `${TOKEN_OS:-$HOME/runtimes/Token-OS/live}/cli-tools/lib/imperium_config.py`.
- Exports: `$IMPERIUM_MACHINE`, `$IMPERIUM`, `$CIVIC`, `$TOKEN_OS`, `$CLI_TOOLS`, `$TOKEN_API_URL`.
- Lookup: `imperium_cfg tailscale_ip mac|wsl|phone`, `imperium_cfg token_api_url`.
- Diagnostic script: `${TOKEN_OS:-$HOME/runtimes/Token-OS/live}/Shell/network-test.sh`.

## Safe checks

```bash
source "${TOKEN_OS:-$HOME/runtimes/Token-OS/live}/cli-tools/lib/nas-path.sh"
printf '%s
' "$IMPERIUM_MACHINE $IMPERIUM $TOKEN_API_URL"
imperium_cfg tailscale_ip phone
token-ping --raw /health
```

## Do Not

- Do not hardcode Tailscale IPs, NAS paths, Token-API URLs, or runtime checkout paths.
- Do not assume Mac paths work on WSL/phone; resolve through config.
- Do not run broad SSH/Tailscale probes against devices unless the task is network diagnosis.
- Do not bind new tooling to quarantined legacy/recycle paths.
