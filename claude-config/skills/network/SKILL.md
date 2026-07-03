---
name: network
description: Imperium and askCivic machine topology and config routing. Use for Tailscale, SSH aliases, NAS paths and the NAS-to-local storage doctrine, Token-API reachability, Mac/WSL/phone roles, MacroDroid phone routing, future CIVIC_MACHINE handling, Deskflow/remote operator paths, replacing hardcoded machine values, or the incoming headless Linux boxes (GMKtec K12 pair + Comet Pro network KVMs, mac mini migration).
---

# Network

Imperium network values are centralized in machine config. For topology detail, read `references/topology.md`.

Two headless Linux boxes (GMKtec K12) with dedicated network KVMs (GL.iNet Comet Pro) are
incoming (July 2026) — one personal/Imperium, one work/civic — to take over from the mac
mini. That expansion is PLANNED, not live; see "Incoming Expansion" in `references/topology.md`
before touching machine-identity config for them.

## Surfaces

- Shell config: `${TOKEN_OS:-$HOME/runtimes/Token-OS/live}/cli-tools/lib/nas-path.sh`.
- Python config: `${TOKEN_OS:-$HOME/runtimes/Token-OS/live}/cli-tools/lib/imperium_config.py`.
- Exports: `$IMPERIUM_MACHINE`, `$IMPERIUM`, `$CIVIC`, `$TOKEN_OS`, `$CLI_TOOLS`, `$TOKEN_API_URL`.
- Lookup: `imperium_cfg tailscale_ip mac|wsl|phone`, `imperium_cfg token_api_url`.
- Diagnostic script: `${TOKEN_OS:-$HOME/runtimes/Token-OS/live}/Shell/network-test.sh`.

## Safe Checks

```bash
source "${TOKEN_OS:-$HOME/runtimes/Token-OS/live}/cli-tools/lib/nas-path.sh"
printf '%s\n' "$IMPERIUM_MACHINE $IMPERIUM $TOKEN_API_URL"
imperium_cfg tailscale_ip phone
curl -sf "$TOKEN_API_URL/health"
```

## Do Not

- Do not hardcode Tailscale IPs, NAS paths, Token-API URLs, or runtime checkout paths.
- Do not assume Mac paths work on WSL/phone; resolve through config.
- Do not run broad SSH/Tailscale probes unless the task is network diagnosis.
- Do not bind new tooling to quarantined legacy/recycle paths.
