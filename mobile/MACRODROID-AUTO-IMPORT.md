# MacroDroid Auto-Import Discovery

Status: experimental, fail-closed.

## Baseline from discovery

- Phone MacroDroid package: `com.arlosoft.macrodroid`
- Observed app version from APK: `5.65.9` (`896500009`)
- Probe macro used: `Auto Import Probe`
  - disabled
  - manual-only `EmptyTrigger`
  - category `Import Tests`
  - no actions
- Baseline export before experiments:
  - path: `/tmp/macrodroid-state/EXPORT.mdr`
  - macro count: `40`
  - sha256: `058bf0332f1010addc81c5499420461b7a95604adf4bbb9302a090ea05ec21d4`

## Discovery findings

### AI generator path

The APK includes first-party AI schema assets under `assets/ai/`, including:

- `assets/ai/macrodroid-llm-schema.yaml`
- `assets/ai/prompts/system-prompt-template.txt`
- class YAML files for actions/triggers/constraints

Those assets confirm the official wrapper shape:

```json
{"macroExportVersion": 1, "macro": {}, "globalVariables": [], "userIcons": null, "aiFeedback": ""}
```

Static inspection did not reveal a separate exported "AI Builder import JSON" activity or intent. The AI builder appears to generate the same `.macro` wrapper JSON consumed by the normal MacroDroid import/file-handler path.

### Android file intent path

`AndroidManifest.xml` exposes:

- `com.arlosoft.macrodroid.filehandler.FileHandlerProxy`
- exported `android.intent.action.VIEW`
- `.macro` filters for `file://`, `content://`, `text/plain`, and `application/octet-stream`
- `android.intent.action.SEND` is exported separately by
  `com.arlosoft.macrodroid.triggers.activities.MacroDroidShareActivity`; that is
  the share-trigger entrypoint, not the `.macro` import handler.

The useful no-picker launch is an explicit VIEW intent:

```bash
am start --user 0 -W \
  -n com.arlosoft.macrodroid/.filehandler.FileHandlerProxy \
  -a android.intent.action.VIEW \
  -d file:///storage/emulated/0/Download/MacroDroid/auto-import/probe.macro \
  -t application/octet-stream
```

Important: from Termux, use Termux's `am` wrapper. Calling `/system/bin/am` directly can fail on current Android with a package/uid mismatch because it claims `com.android.shell` while running as the Termux UID.

This path avoids the Android file picker and opens MacroDroid's file handler directly. During discovery it did **not** prove a non-UI final confirmation path; after launch, `macrodroid-state --pull` still showed 40 macros and no `Auto Import Probe`.

### Template store path

Template-store upload/install was not used for import automation. No existing authenticated private-template push flow was proven, and `/api/install` should be treated as install-by-template-id rather than arbitrary `.macro` upload unless proven otherwise.

### Accessibility/input fallback

Termux cannot inject `input keyevent/tap` without privileged `INJECT_EVENTS`. Do not add Shizuku/root/ADB as an import dependency. If accessibility-assisted confirmation is added later, it must remain explicitly gated and verify by pulling MacroDroid state after the click.

## CLI

`macrodroid-import` implements the safe no-picker launch plus verification:

```bash
MACRODROID_AUTO_IMPORT=1 macrodroid-import probe.macro
```

Behavior:

1. Validates with `macrodroid-validate --quiet`.
2. Refuses phone interaction unless `MACRODROID_AUTO_IMPORT=1`.
3. Pulls current state with `macrodroid-state --pull`.
4. Refuses duplicate macro names by default.
5. Stages the `.macro` file to shared storage:
   `~/storage/downloads/MacroDroid/auto-import/` on Termux, resolved to the
   shared Android downloads path before launch.
6. Launches MacroDroid's explicit `.macro` VIEW handler.
7. Pulls state again and returns success only if macro count did not decrease,
   exactly one new macro was added, and the target macro name exists.
8. Logs file, macro name, SHA256, counts, export hashes, timestamp, and result to:
   `~/.cache/macrodroid-import/import.log`.

Expected current limitation: without a proven non-UI confirmation path, the command may return nonzero with `not_confirmed` after successfully launching MacroDroid's review/import UI.

## Confirmed import behavior after live prompt test

A second probe with one harmless `LogAction` imported successfully after the operator approved MacroDroid's prompt:

- macro: `Auto Import Probe Log`
- result: verified by `macrodroid-state --pull`
- macro count: `40 -> 41`

Re-importing the same macro with duplicate permission added another same-name macro instead of cleanly replacing it:

- macro count: `41 -> 42`
- two disabled `Auto Import Probe Log` records remained

Therefore replacement cannot be assumed. The safe tool semantics are:

- default: refuse if same-name macros already exist
- `--allow-existing`: legacy duplicate-test mode only
- `--replace`: explicit clean replacement attempt; succeeds only if exactly one target macro remains after verification pull

If duplicates remain, the agent can report the exact macro names/categories/GUIDs that the operator should delete in MacroDroid. There is no proven exported MacroDroid intent/API that lets an agent delete a macro directly. MacroDroid's schema does include `DisableMacroAction`, so an agent can generate a cleanup macro to disable an old macro by name, but disabling is not deletion and should not be treated as clean replacement.

## Agent-facing deletion suggestion

Current supported agent behavior is advisory, not destructive:

1. Pull state.
2. Detect same-name or retired macros.
3. Print a deletion plan with name, category, enabled state, and GUID.
4. Ask the operator to delete those records in MacroDroid.
5. Pull state again and verify deletion before importing the replacement.

Do not use full `.mdr` restore as a deletion mechanism unless a separate, explicit high-risk restore procedure is written and validated. Whole-state restore can wipe unrelated live macros.

## MacroDroid agent harness direction

The existing export path is the `List Exports API` macro:

- HTTP trigger: `/list-exports`
- action: `ExportMacrosAction`
- output path: Termux `~/macros/EXPORT.mdr`
- desktop pull: `macrodroid-state --pull` via SSH/SCP

This works, but it predates the current agent interface. The cleaner future harness is a single MacroDroid agent endpoint, for example `/macrodroid-agent`, with structured actions:

- `action=export` — run `ExportMacrosAction`, respond with JSON status/path/timestamp
- `action=health` — respond with MacroDroid harness version and enabled state
- `action=import-ready` — report import staging directory and current duplicate matches if provided a macro name
- `action=log` — append structured debug entries

The desktop side should keep SSH/SCP for file transfer unless MacroDroid gains a safe first-class file-return API. The agent endpoint should replace legacy ad-hoc endpoints like `/list-exports` over time, but keep the same export-then-pull verification invariant.
