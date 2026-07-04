---
name: mobile
description: MacroDroid, Termux, phone automation, Pavlok, and Android-side Token-OS work. Use when editing, validating, importing, replacing, or verifying official .macro files with the locked macrodroid-import path; pulling phone state; checking phone HTTP endpoints; or working on Termux/MacroDroid integration.
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
- Validate before any phone interaction: `macrodroid-validate <file.macro>`.
- The only official macro delivery path is: `MACRODROID_AUTO_IMPORT=1 macrodroid-import <file.macro>`. This replaces all “push” language and workflows.
- `macrodroid-import` must validate, stage, launch MacroDroid’s `.macro` file handler, and verify by pulling state. A staged file or launched prompt is not success.
- After phone import or live changes, pull/export and treat deployed MacroDroid JSON as canonical truth.
- Do not revive Shizuku, ADB/root flows, direct `am`/file-picker hacks, retired custom macro DSLs or trigger/action builder compilers, or full `.mdr` restore as a macro delivery/deletion path.

## Safe Checks

```bash
source "${TOKEN_OS:-$HOME/runtimes/Token-OS/live}/cli-tools/lib/nas-path.sh"
imperium_cfg tailscale_ip phone
macrodroid-state --list 2>/dev/null || true
curl -sf "http://$(imperium_cfg tailscale_ip phone):7777/server-heartbeat"
```

## Official Macro Delivery Path

This is the only approved way to deliver MacroDroid macros to the operator’s phone. Do not use alternate “push” methods.

- Create or edit an official `.macro` JSON wrapper.
- Validate locally before touching the phone.
- Import only with the gated launcher:
  ```bash
  MACRODROID_AUTO_IMPORT=1 macrodroid-import <file.macro>
  ```
- The operator must approve MacroDroid’s import prompt on the phone.
- The command must pull/export after the prompt and return success only when deployed state verifies the macro.
- For existing macro replacement, use only:
  ```bash
  MACRODROID_AUTO_IMPORT=1 macrodroid-import --replace <file.macro>
  ```
- If replacement leaves duplicates or the tool prints a deletion plan, report the exact duplicate records for manual deletion; do not attempt destructive restore/delete shortcuts.

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
4. Import only through the official gated launcher:

   ```bash
   MACRODROID_AUTO_IMPORT=1 macrodroid-import /tmp/name.macro
   ```

   For replacement of an existing same-name macro:

   ```bash
   MACRODROID_AUTO_IMPORT=1 macrodroid-import --replace /tmp/name.macro
   ```

   Approve the MacroDroid import prompt on the phone.
5. Trust success only after `macrodroid-import` verifies via pull/export. If it returns nonzero, report the result and do not claim the macro was deployed.

## Do Not

- Do not author YAML macro specs or invoke retired spec-to-macro builders.
- Do not add custom class builders to `macrodroid-gen`; use official JSON.
- Do not assume phone reachability; resolve IP through `imperium_cfg`.
- Do not import unvalidated macros to the phone.
- Do not use `macrodroid-push`; it is retired in favor of verified `macrodroid-import`.
- Do not `scp`/stage files and call that a push. Staging is internal to `macrodroid-import` only.
- Do not use Shizuku, ADB/root, direct `am`, Android file picker workarounds, or accessibility clickers as delivery dependencies unless a future skill update explicitly replaces this rule.
- Do not use full `.mdr` restore to delete or replace a macro.
