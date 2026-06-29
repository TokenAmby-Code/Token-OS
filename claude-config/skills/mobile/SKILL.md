---
name: mobile
description: MacroDroid, Termux, phone automation, Pavlok, and Android-side Token-OS work. Use when editing or validating .macro files, using macrodroid-* tools, pushing/pulling phone automation, checking phone HTTP endpoints, or working on Termux/MacroDroid integration.
---

# Mobile

Use this skill for phone-side automation: MacroDroid, Termux, Pavlok intents, phone HTTP endpoints, and `macrodroid-*` tooling.

## Required First Read

Before authoring or modifying any MacroDroid macro, read:

```bash
${TOKEN_OS:-$HOME/runtimes/Token-OS/live}/mobile/macrodroid-llm-schema.yaml
```

For broader mobile context, read `${TOKEN_OS:-$HOME/runtimes/Token-OS/live}/mobile/AGENTS.md`.

## MacroDroid Invariants

- Official `.macro` JSON wrapper files only.
- `macrodroid-llm-schema.yaml` is the class/field source of truth.
- Validate before push: `macrodroid-validate <file.macro>`.
- Push with the sanctioned tool only: `macrodroid-push <file.macro>`.
- After phone import or live changes, pull/export and treat deployed MacroDroid JSON as canonical truth.
- Do not revive Shizuku, ADB/root flows, retired custom macro DSLs or trigger/action builder compilers.

## Safe Checks

```bash
source "${TOKEN_OS:-$HOME/runtimes/Token-OS/live}/cli-tools/lib/nas-path.sh"
imperium_cfg tailscale_ip phone
macrodroid-state --list 2>/dev/null || true
curl -sf "http://$(imperium_cfg tailscale_ip phone):7777/server-heartbeat"
```

## Macro Workflow

1. Pull/read current state if changing an existing macro:
   ```bash
   macrodroid-state --pull
   macrodroid-read /tmp/macrodroid-state/EXPORT.mdr --macro "<Name>" --export-macro > /tmp/name.macro
   ```
2. Edit the official JSON directly using the schema.
3. Validate:
   ```bash
   macrodroid-validate /tmp/name.macro
   ```
4. Push only after validation:
   ```bash
   macrodroid-push /tmp/name.macro
   ```
5. Verify by pull/export, logs, endpoint response, or live phone behavior.

## Do Not

- Do not author YAML macro specs or invoke retired spec-to-macro builders.
- Do not add custom class builders to `macrodroid-gen`; use official JSON.
- Do not assume phone reachability; resolve IP through `imperium_cfg`.
- Do not push unvalidated macros to the phone.
