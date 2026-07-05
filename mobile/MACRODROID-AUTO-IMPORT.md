# MacroDroid Auto-Import Discovery

Status: gated, fail-closed. Direct file-handler launch is supported; optional MacroDroid accessibility auto-accept is available only through the explicit harness gate.

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
7. Pulls state again and returns success only after semantic verification:
   - default mode: exactly one newly added same-name macro semantically matches the candidate
   - `--replace`: exactly one same-name macro remains and it semantically matches the candidate
   - `--allow-existing`: legacy duplicate-test mode; one newly added semantic duplicate is required
8. Logs file, macro name, SHA256, counts, export hashes, candidate semantic fingerprint, timestamp, and result to:
   `~/.cache/macrodroid-import/import.log`.

Verification does not trust export-hash movement by itself. MacroDroid can mutate export state or add duplicates while rejecting or ignoring the intended candidate; import success requires the live export to contain the intended trigger/action/constraint structure and candidate-provided values.

Without the on-phone harness, the command may return nonzero with `not_confirmed` after successfully launching MacroDroid's review/import UI because MacroDroid still requires a final Add/Import prompt.


## Gated auto-accept harness

`mobile/macros/macrodroid-import-harness.macro` provides the sanctioned accessibility clicker for dev sessions. Import it manually once through the official MacroDroid path, then leave it enabled only if the endpoint gate behavior has been verified.

Endpoints on the phone MacroDroid HTTP server:

- `/macrodroid-import-arm` — sets global `md_import_auto_accept_enabled=true` and resets/starts stopwatch `macrodroid_import_auto_accept_ttl`.
- `/macrodroid-import-disarm` — sets the global gate false and resets the stopwatch.
- `/macrodroid-import-accept` — clicks the MacroDroid import prompt only while the global gate is true and the stopwatch is running under 30 minutes.

The click currently uses UI Interaction XY percentages at `88%,93%` near the bottom-right prompt button. This is the fragile part of the harness. It can work when the phone is awake and MacroDroid renders the expected import prompt, but it can miss when the phone is off/locked, when a prior prompt is still foregrounded, or when the prompt layout shifts. If this misses on the live phone, do **not** paper over it with a long blind sleep. The next harness revision should prefer a UI Interaction click by text/content description/view target for MacroDroid's actual Add/Import button, with XY only as a fallback.

Desktop usage:

```bash
MACRODROID_AUTO_IMPORT=1 macrodroid-import --auto-accept path/to/macro.macro
MACRODROID_AUTO_IMPORT=1 macrodroid-import --disarm-auto-accept
```

`--auto-accept` arms/refreshes the 30-minute TTL, launches MacroDroid's first-party file handler, waits only for the prompt-render gap before calling `/macrodroid-import-accept`, then verifies from a fresh live export. A short pre-click UI-load delay is legitimate; a post-click magic-number delay is not. It still fails closed if the exported macro does not semantically match the candidate.

For several back-to-back imports, use the batch wrapper. It validates every file before touching the phone, pulls one initial export, imports sequentially with per-file auto-accept, chains each verified post-import export as the next file's pre-state, stops on first failure, and disarms at the end by default:

```bash
MACRODROID_AUTO_IMPORT=1 macrodroid-import-batch macro-a.macro macro-b.macro macro-c.macro
MACRODROID_AUTO_IMPORT=1 macrodroid-import-batch --replace macro-a.macro macro-b.macro
```

Batch timing notes from live tests:

- With the phone off/locked, auto-accept can arm and log clicks without completing the import; keep the phone awake/unlocked for live batch tests until the harness is changed to target the actual button.
- A two-probe batch succeeded once the phone was awake, verifying both imports by fresh exports.
- Reducing the pre-click UI-render delay too far (`0`/`0.5s`) races MacroDroid's prompt and misses. `1.5s`/`2.0s` were still flaky in later prompt states with the XY clicker. Treat the UI-targeting bug, not sleeps, as the remaining speed/reliability gate.
- The batch wrapper no longer needs a blind post-click sleep; verification export is the event/state observation boundary.

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
