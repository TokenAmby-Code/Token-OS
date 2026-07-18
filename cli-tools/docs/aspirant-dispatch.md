# Aspirant Dispatch Launch Path

Last updated: 2026-05-15

## What is now implemented

There are two full-session aspirant entry points. Both create an aspirant note, bind a session document, and launch a managed Claude session into the `legion:new` stack.

### Token-API inbox path

Entry points:

- `POST /api/inbox/create`
- `POST /api/inbox/notify`

Behavior:

1. Creates an aspirant note under `Imperium-ENV/Aspirants/`.
2. Writes aspirant launch frontmatter, including launcher and dispatch target metadata.
3. Creates a linked session doc under `Imperium-ENV/Terra/Sessions/`.
4. Writes aspirant system prompt and initial prompt temp files.
5. Launches through:

   ```bash
   dispatch \
     --target legion:new \
     --dir <Imperium-ENV> \
     --session-doc <session-doc> \
     --system-prompt-file <aspirant-system-file> \
     --prompt-file <aspirant-prompt-file> \
     --gt
   ```

6. Suppresses duplicate launches when the note already has an active `aspirant_launch_id` with `aspirant_session_status: launching|launched`.
7. Marks failed dispatch attempts on the aspirant note with `aspirant_session_status: failed` and `aspirant_launch_error`.

### CLI dispatch aspirant path

Entry point:

```bash
dispatch --aspirant --aspirant-kind dispatch <objective>
```

Default behavior now launches a real full aspirant session. It no longer stops at intake unless explicitly requested.

Behavior:

1. Creates the aspirant note in `Imperium-ENV/Aspirants/` using `cli-tools/lib/aspirant_create.py`.
2. Creates or links an aspirant session doc under `Imperium-ENV/Mars/Sessions/` by default.
   - Generated session doc filenames are human-readable (`Aspirant - Worker plan.md`), Obsidian Sync-safe, and not date-prefixed.
3. Emits JSON creation metadata to stdout.
4. Builds an aspirant launch prompt that names:
   - the gene-seed-bearing aspirant note,
   - the linked session doc,
   - vault-context startup instructions,
   - the requirement to append `## Implantation` / `## Trials` output back to the aspirant note.
5. Launches through:

   ```bash
   dispatch \
     --engine claude \
     --dir <Imperium-ENV> \
     --target legion:new \
     --session-doc <session-doc> \
     --system-prompt-file cli-tools/prompts/aspirant-persona.md \
     --prompt-file <generated-aspirant-launch-prompt> \
     --gt
   ```

6. The nested dispatch sets `TOKEN_API_INTERNAL_DISPATCH=1`, so auto-aspirant policy does not recursively create another aspirant.

Preserved old behavior:

```bash
dispatch --aspirant --aspirant-kind dispatch --intake-only <objective>
```

`--intake-only` creates the note/session doc but does not launch a legion pane.

Still note-only:

```bash
dispatch --aspirant --aspirant-kind deploy_p <objective>
dispatch --aspirant --aspirant-kind deploy_d <objective>
```

`deploy_p` and `deploy_d` aspirants remain intake notes only. They do not launch Claude sessions.

Dry runs remain non-mutating and non-launching:

```bash
dispatch --dry-run --aspirant --aspirant-kind dispatch <objective>
```

## Contract for launched aspirants

A launched aspirant is a Claude session, not Codex and not the old MiniMax/Sonnet implantation pipeline.

The session must:

- read the linked session doc,
- read the aspirant note,
- treat the gene-seed as authoritative intent,
- use vault context actively,
- perform implantation/trials work,
- append useful output to the aspirant note under `## Implantation` and/or `## Trials`,
- maintain proactive `questions` entries in frontmatter,
- stop at the dispatch boundary and not launch downstream workers unless separately authorized.

## Automated verification completed

Validated on 2026-05-15:

```bash
bash -n cli-tools/bin/dispatch
```

Coverage included:

- Token-API managed legion launch,
- duplicate suppression,
- failure state recording,
- no legacy `run_implantation` call,
- CLI `dispatch` aspirant note/session creation,
- CLI launch through `dispatch --target legion:new`,
- `--session-doc`, `--system-prompt-file`, and `--prompt-file` propagation,
- `--dry-run` non-launch behavior,
- `--intake-only` note/session-only behavior.

## Real validation walk: no dry runs

Use real aspirant instances from this point forward. Do not use `--dry-run` for the validation walk.

### Preflight

1. Confirm Token-API is healthy:

   ```bash
   curl -s http://localhost:7777/health
   ```

2. Confirm `tmuxctl` and `dispatch` are on PATH:

   ```bash
   command -v tmuxctl
   command -v dispatch
   ```

3. Confirm the managed stack exists or can be created:

   ```bash
   tmux list-windows -t main
   ```

4. Watch state in another pane:

   ```bash
   agents-db instances
   agents-db events --limit 20
   ```

### Walk 1 — CLI dispatch aspirant creates and launches a real legion pane

Run:

```bash
dispatch \
  --aspirant \
  --aspirant-kind dispatch \
  --engine claude \
  --persona aspirant \
  --dir "$IMPERIUM_VAULT" \
  --target legion:new \
  --victory-condition "Aspirant reads note/session doc and writes trials output back to the note" \
  "VALIDATION: CLI dispatch aspirant full-session launch. Read this note and session doc, append a short Implantation and Trials section, and stop at the dispatch boundary."
```

Expected:

- stdout first line is JSON with `kind: dispatch`, `status: aspirant_trials`, `dispatch_schema_complete: true`, and a `session_doc` path.
- stdout includes `launch_action: dispatch --engine claude ... --target legion:new ...`.
- A new managed legion worker pane opens.
- Token-API records a row with:
  - `launcher=dispatch`,
  - `dispatch_target=legion:new`,
  - `target_working_dir` / cwd equal to `Imperium-ENV`,
  - `dispatch_session_doc_path` set to the created session doc,
  - Golden Throne metadata enabled.
- The aspirant pane starts with the generated prompt and aspirant persona system prompt.
- The aspirant appends `## Implantation` and/or `## Trials` to the aspirant note.

Check:

```bash
agents-db query "SELECT session_id, launcher, dispatch_target, target_working_dir, dispatch_session_doc_path, instance_type, zealotry, tmux_pane FROM claude_instances WHERE launcher='dispatch' ORDER BY last_activity DESC LIMIT 5;"
agents-db events --limit 20
```

Then inspect the note and session doc named in the JSON output.

### Walk 2 — CLI `--intake-only` preserves note/session-only behavior

Run:

```bash
dispatch \
  --aspirant \
  --aspirant-kind dispatch \
  --intake-only \
  --engine claude \
  --persona aspirant \
  --dir "$IMPERIUM_VAULT" \
  --target legion:new \
  --victory-condition "No pane should launch" \
  "VALIDATION: intake-only dispatch aspirant should create note and session doc only."
```

Expected:

- Aspirant note is created.
- Session doc is created/linked.
- No new legion pane opens.
- No new `claude_instances` row appears for this intake-only command.

### Walk 3 — Token-API inbox creates and launches a real legion pane

Run a real API-created aspirant:

```bash
curl -s -X POST http://localhost:7777/api/inbox/create \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "VALIDATION Token API aspirant launch",
    "content": "Gene-seed: read this note/session doc, append a concise Implantation and Trials result, and stop at the dispatch boundary.",
    "type": "prescriptive",
    "source": "validation"
  }'
```

Expected:

- Response includes an `aspirant_session` object with `launched: true`.
- Aspirant note frontmatter includes:
  - `aspirant_launch_id`,
  - `aspirant_session_status: launched`,
  - `aspirant_launcher: dispatch`,
  - `aspirant_dispatch_target: legion:new`,
  - `aspirant_session_doc`.
- A new managed legion worker pane opens.
- Token-API DB row records dispatch metadata with cwd `Imperium-ENV` and linked session doc.
- The aspirant appends implantation/trials output to the note.

### Walk 4 — Token-API duplicate suppression

Re-submit or re-trigger launch for the same note path if available through the existing API surface.

Expected:

- The second launch returns duplicate/no-op metadata.
- No second legion pane opens for the same aspirant note.
- The aspirant note keeps the original `aspirant_launch_id` and launched status.

### Pass/fail criteria

Pass only if all of these are true:

- Both CLI and Token-API entry points launch real managed legion panes.
- The launched panes are Claude aspirant sessions with aspirant system prompt + gene-seed prompt.
- The generated/linked session doc path is visible in Token-API DB metadata.
- The working directory is `Imperium-ENV`, not the code repo.
- Aspirant notes receive actual `## Implantation` / `## Trials` output.
- `--intake-only` creates no pane.
- Duplicate API launch attempts do not create duplicate panes.

If any item fails, capture:

```bash
agents-db instances
agents-db events --limit 50
tmux list-panes -a -F '#{session_name}:#{window_name}.#{pane_index} #{pane_id} #{pane_current_command} #{pane_current_path}'
```

Then inspect the aspirant note frontmatter and the linked session doc before changing code.

## Questions gate

Aspirant session docs use the generic Golden Throne questions gate: when `questions: []` is present, each entry must reach `state: closed` before the first StopValidate pass is allowed. See `$IMPERIUM_VAULT/Terra/Ultramar/Golden Throne Protocol.md` for hook behavior and `$IMPERIUM_VAULT/Aspirants/test.md` Design Decisions D1–D4 for schema and predicate context.
