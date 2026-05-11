# MacroDroid Official Schema Workflow

Date: 2026-05-09
Status: active and mandatory

`macrodroid-llm-schema.yaml` supersedes the old custom YAML DSL and the old `macrodroid-gen` compiler.

## Policy

- Generate official `.macro` JSON wrapper files directly.
- Validate every `.macro` with `macrodroid-validate` before push/import.
- Do not create new `macros/*.yaml` files.
- Do not add custom class builders to `macrodroid-gen`.
- Do not maintain lossy YAML conversion paths.
- Use `.mdr` exports and official `.macro` JSON as canonical MacroDroid state.

The previous YAML specs and compiler were archived, not kept active:

- `mobile/macros/archive/legacy-yaml-dsl-2026-05-09/`
- `cli-tools/archive/macrodroid-legacy-yaml-dsl-2026-05-09/`

## Official Wrapper

Every importable `.macro` file must be strict JSON with this top-level shape:

```json
{
  "macroExportVersion": 1,
  "macro": {
    "m_name": "Example",
    "m_enabled": true,
    "m_completed": true,
    "m_GUID": 0,
    "m_category": "Automation",
    "aiGenerated": 1,
    "m_description": "What this macro does",
    "m_triggerList": [
      {"m_classType": "EmptyTrigger", "m_SIGUID": 0}
    ],
    "m_actionList": [],
    "m_constraintList": []
  },
  "globalVariables": [],
  "userIcons": null,
  "aiFeedback": "Generated from official MacroDroid schema."
}
```

## Structural Rules

- `m_triggerList` contains only triggers.
- `m_actionList` contains only actions.
- `m_constraintList` contains only constraints.
- Item-level `m_constraintList` also contains only constraints.
- `IfConditionAction`/`ElseAction`/`ElseIfConditionAction`/`EndIfAction` are flat sibling actions.
- Every `IfConditionAction` needs a matching `EndIfAction`.
- Every `LoopAction` needs a matching `EndLoopAction`.
- Every selectable item needs `m_classType` and `m_SIGUID`.
- Placeholder `m_SIGUID: 0` is acceptable for import.
- New/importable macro `m_GUID` must be `0`.
- Manual macros use `EmptyTrigger`.

## Tooling

### Generate skeletons

```bash
macrodroid-gen --empty "Manual Macro" --category Automation --pretty > manual.macro
macrodroid-gen --http-endpoint heartbeat --name Heartbeat --response-text OK --pretty > heartbeat.macro
```

### Normalize official JSON

```bash
macrodroid-gen official.macro --pretty --validate > normalized.macro
```

### Wrap a bare macro object

```bash
macrodroid-gen bare-macro.json --wrap --pretty --validate > importable.macro
```

### Extract from `.mdr`

```bash
macrodroid-read EXPORT.mdr --macro "Heartbeat" --export-macro > heartbeat.macro
macrodroid-validate heartbeat.macro
```

### Push

```bash
macrodroid-push heartbeat.macro
```

`macrodroid-push` runs strict validation first and refuses malformed files.

## Editing Workflow

1. Pull the current phone export:
   ```bash
   macrodroid-state --pull
   ```

2. Extract the macro to edit:
   ```bash
   macrodroid-read /tmp/macrodroid-state/EXPORT.mdr --macro "Macro Name" --export-macro > macro-name.macro
   ```

3. Edit the JSON directly using `macrodroid-llm-schema.yaml`.

4. Validate:
   ```bash
   macrodroid-validate macro-name.macro
   ```

5. Push and import on phone:
   ```bash
   macrodroid-push macro-name.macro
   ```

6. Export/pull again and verify:
   ```bash
   macrodroid-state --detail
   ```

7. Delete staged `.macro` files after import.

## Validator Behavior

`macrodroid-validate` is intentionally strict:

- Invalid JSON fails.
- Missing official wrapper fields fail.
- Unknown class names fail unless `--allow-unknown` is explicitly used for audit/debug.
- Non-zero macro GUIDs fail unless `--allow-mdr-guids` is explicitly used for raw export audit.
- Misplaced triggers/actions/constraints fail.
- Unbalanced if/loop markers fail.

Use `--allow-unknown` only when inspecting old deployed exports, not for new imports.

## Legacy Cleanup

The active `macros/` directory should contain exports/docs, not YAML source specs or stale staging files. Old YAML specs and staged `.macro` files were moved under `macros/archive/` on 2026-05-09.

If a future migration needs one of those files, read it as historical evidence and then create fresh official `.macro` JSON. Do not revive the YAML DSL.
