# Network Topology Reference

## Machine Roles

- `IMPERIUM_MACHINE=mac`: primary local operator host. NAS mounted at `/Volumes/Imperium`; local Token-API normally at `http://localhost:7777`.
- `IMPERIUM_MACHINE=wsl`: Windows/WSL satellite. Uses NAS mount under `/mnt/imperium` and reaches Mac-hosted services over Tailscale unless a local service is explicitly running.
- `IMPERIUM_MACHINE=phone`: Android/Termux satellite. MacroDroid HTTP server listens on port `7777`; SSH normally uses the phone alias/tooling rather than raw IPs.
- `IMPERIUM_MACHINE=linux`: generic Linux fallback; do not assume Mac or WSL paths.
- Future `CIVIC_MACHINE`: keep askCivic-specific machine branching separate from Imperium branching when introduced.

## NAS

The NAS is a first-class network node, not just a path prefix. It hosts two volumes forming
a domain boundary: **Imperium** (personal, `/Volumes/Imperium` on mac, `/mnt/imperium` on
Linux/WSL) and **Civic** (work). Known operational constraints: SMB is fragile under load
(no tree-grep against live runtime, sandboxed writes, wedge/lag spikes under small-file
churn), remounts detach Obsidian vaults, and TCC forces launchd scripts to live locally.

**Storage doctrine (direction of travel):** the NAS is being demoted from
everything-store to bulk/cold/backup tier. Live working sets (repos, worktrees, runtimes,
DBs, vaults) move to machine-local disk; the NAS receives pushed snapshots and holds
media/archives. Once the incoming boxes give computer-level domain separation, the machine
— not the NAS volume split — is the work/personal boundary. Cross-mounting across that
boundary (work box mounting Imperium, or vice versa) is prohibited. Full rationale and
migration plan: `Aspirants/Network Expansion First Principles.md` in the Imperium vault.

## Incoming Expansion — Headless Linux Boxes (PLANNED, hardware arrives 2026-07-10)

Status: **none of this is live yet.** Do not add registry entries, SSH aliases, or Tailscale
assumptions for these nodes until the hardware is provisioned. This section is the forward
spec so the rollout lands consistently.

### Hardware

- 2× **GMKtec K12** mini PC — AMD Ryzen 7 H 255 (8745HS-class, Zen 4 8c/16t), 32GB DDR5,
  2TB SSD, 3× M.2 2280 slots, OCuLink, dual 2.5G NIC, HDMI 2.1, USB4. Deliberately
  hardware-identical: one provisioning recipe, mutual failover spares.
- 2× **GL.iNet Comet Pro (GL-RM10)** network KVM — Wi-Fi 6, 4K@30 passthrough, touchscreen,
  32GB eMMC, native Tailscale client, ATX/fingerbot board for hard power-cycle and
  BIOS-level disaster recovery. Each K12 gets a dedicated Comet Pro.

### Planned Roles

- **Personal box** (Imperium domain): new `IMPERIUM_MACHINE` role — a real registry entry,
  not the generic `linux` fallback. Takes over Token-OS personal workloads from the mac mini.
- **Work box** (civic/Pax domain): the first concrete `CIVIC_MACHINE`. Civic branching stays
  separate from Imperium branching; civic items belong in Pax-ENV.
- Both boxes run headless Linux; operator access is SSH/tmux over Tailscale, with the Comet
  Pros as the out-of-band hardware path (phone + DeX portable monitor as the mobile console).
- The two KVMs are tailnet nodes in their own right — 4 new Tailscale devices total.

### Planned Config Changes (when live)

- `cli-tools/lib/nas-path.sh`: new `_IMPERIUM_CFG_<role>_*` block per box (tailscale_ip,
  token_api_url, NAS mount root); detection logic to distinguish the new role(s) from
  generic `linux`.
- `cli-tools/lib/imperium_config.py`: matching `_REGISTRY` entries.
- NAS mount path on the boxes: expected `/mnt/imperium` (Linux convention, same as WSL) —
  confirm at provisioning.
- Token-API: boxes initially point at the Mac-hosted instance via config (never a hardcoded
  IP); whether a box later hosts its own Token-API is a migration-spec decision, not
  assumed here.

### Open Decisions (resolve in the migration spec before provisioning)

- Machine role names for the two boxes (config keys, SSH aliases, tmux page names).
- Distro choice and provisioning order.
- Domain-cutover sequence for peeling work/personal off the mac mini, and the mac mini's
  end-state role.
- Whether the second 2.5G NIC gets a dedicated use (KVM link, box-to-box, LAN vs tailnet split).

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
