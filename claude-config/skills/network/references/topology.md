# Network Topology Reference

## Machine Roles

- `IMPERIUM_MACHINE=mac`: primary local operator host. NAS mounted at `/Volumes/Imperium`; local Token-API normally at `http://localhost:7777`.
- `IMPERIUM_MACHINE=wsl`: Windows/WSL satellite. Uses NAS mount under `/mnt/imperium` and reaches Mac-hosted services over Tailscale unless a local service is explicitly running.
- `IMPERIUM_MACHINE=phone`: Android/Termux satellite. MacroDroid HTTP server listens on port `7777`; SSH normally uses the phone alias/tooling rather than raw IPs.
- `IMPERIUM_MACHINE=linux`: generic Linux fallback; do not assume Mac or WSL paths.
- Future `CIVIC_MACHINE`: keep askCivic-specific machine branching separate from Imperium branching when introduced.

## Shared Roots

- `$IMPERIUM`: NAS root for Imperium-ENV.
- `$CIVIC`: civic/askCivic root when mounted/configured.
- `$TOKEN_OS`: Token-OS runtime checkout.
- `$TOKEN_API_URL`: active Token-API URL for the current machine.

## Tailscale and Remote Operators

Use `imperium_cfg tailscale_ip <role>` and named SSH wrappers/aliases. Raw Tailscale IPs are configuration data, not code constants.

Remote operators should hit Token-API through configured URLs or Tailscale DNS/IP lookups, never by copying a machine-specific literal into scripts.

## WSL Satellite

WSL may hold worktrees and run tests but commonly depends on Mac-hosted Token-API, tmux walls, NAS, or browser surfaces. Cross-device worktree movement should use `worktree-sync` or explicit transplant/SSH flows, not path substitution.

## Phone / MacroDroid

The phone is a constrained satellite. Resolve its Tailscale IP through config, use MacroDroid port `7777` for HTTP endpoints, and use mobile tooling for SSH/push/pull. Do not revive ADB/Shizuku assumptions.

## Deskflow / UI Control

Deskflow and vertical-monitor operator surfaces are physical/UI routing concerns. Browser automation should prefer same-host localhost/dev surfaces unless the task explicitly tests physical display behavior.
