---
name: persona
description: "Register a persona in the DB mid-session. Usage: /persona <name> — sets legion and identity from Personas/<name>.md without restart."
user_invocable: true
---

# /persona — Mid-Session Persona Registration

## Purpose

When an instance loads a persona via `@Personas/<name>.md` **in an arbitrary pane**, the DB doesn't know about it — the instance shows as `legion: astartes, instance_type: one_off`. This skill bridges the gap: read the persona file, extract identity fields, and PATCH the instance's DB row via Token-API.

**Singleton persona panes auto-register — do not PATCH them.** The custodes, Fabricator-General, and Administratum panes carry a stable `@PANE_ID` (`legion:custodes`, `mechanicus:fabricator-general`, `mechanicus:admin`) stamped by tmuxctl. `SessionStart` derives the row identity (persona + rank, plus legion/primarch) from that pane label — it is an infrastructure invariant, not the agent's job. **Custodes identity is persona + rank** (`persona.slug=custodes`, `rank=overseer`), resolved on the canonical `instances` table — **never** by sync. `synced`/`instance_type=sync` are a runtime MODE the morning session sets while live (default `synced=0`); do not treat `synced=0` at registration time as a defect. If you are one of these personas, your row is **already correct on startup**. Run the verify step below and **report** if it is wrong (that means the harness is broken) — never self-PATCH. Patching is only for ad-hoc personas loaded into a non-persona pane.

## Usage

Parse the user's arguments:
- `/persona` or `/persona --help` — print the **Quick Reference** below, then stop
- `/persona <name>` — execute the **Registration Flow** below

---

## Quick Reference

```
/persona custodes    — Set legion: custodes, register as Custodes persona
/persona vulkan      — Set legion: astartes, primarch: vulkan
/persona mechanicus  — Set legion: mechanicus
/persona slaanesh    — Set legion: astartes, primarch: slaanesh
```

Valid names are derived from files in `Personas/` directory (case-insensitive match).

**Allowed legions:** `astartes`, `mechanicus`, `custodes`, `civic`

---

## Registration Flow

When invoked as `/persona <name>`:

### Step 1: Resolve the persona file

1. Glob `$IMPERIUM/Imperium-ENV/Personas/*.md` to get all available personas
2. Case-insensitive match `<name>` against filenames (without `.md`)
3. If no match, report available personas and stop
4. Read the matched persona file

### Step 2: Extract identity from frontmatter

Parse the YAML frontmatter. The key field is `class`:

```yaml
---
title: Custodes
type: persona
class: custodes        # <-- this maps to legion
---
```

**Mapping rules:**
- If `class` is one of the allowed legions (`custodes`, `mechanicus`, `civic`): set `legion` to that value
- If `class` is a primarch name (e.g., `vulkan`, `guilliman`, `slaanesh`): set `legion` to `astartes`
- The `title` field becomes the display identity

### Step 3: Resolve current instance

Get the current instance ID. Use the environment variable `$CLAUDE_INSTANCE_ID` if available.

If not available, resolve via Token-API:
```bash
curl -s "$TOKEN_API_URL/api/instances/resolve?session_id=$CLAUDE_SESSION_ID" | jq -r '.id'
```

If neither env var is set, try:
```bash
curl -s "$TOKEN_API_URL/api/instances/resolve?source_ip=127.0.0.1&status=processing" | jq -r '.id'
```

### Step 3.5: Verify before patching (persona panes are already registered)

Read the current row, its persona/rank, and its pane label first:
```bash
curl -s "$TOKEN_API_URL/api/instances" \
  | jq --arg id "$INSTANCE_ID" '.[] | select(.id==$id) | {persona: .persona.slug, rank, status, primarch, pane_label}'
```

- If `pane_label` is one of `legion:custodes` / `mechanicus:fabricator-general` / `mechanicus:admin`, this is a **singleton persona pane**. `SessionStart` already set its identity (persona + rank). **Verify it matches the expected identity and stop — do not PATCH.** For custodes specifically, the canonical check is exactly one non-retired row with `persona.slug == "custodes"` at `rank == "overseer"`:
  ```bash
  curl -s "$TOKEN_API_URL/api/instances" \
    | jq '[.[] | select(.persona.slug=="custodes" and .rank!="retired")] | {count: length, row: .[0] | {id, rank, status}}'
  ```
  If that is wrong (no row, wrong rank, or more than one), report it: the harness invariant is broken (e.g. tmuxctl didn't stamp `@PANE_ID`, or the SessionStart hook didn't fire). Surface that instead of papering over it with a manual PATCH. Sync mode (`synced`/`instance_type=sync`) is NOT part of this check — it is a morning-session mode, not identity.
- Otherwise (ad-hoc persona in a non-persona pane), continue to Step 4 and PATCH.

### Step 4: PATCH the instance (ad-hoc panes only)

```bash
# Set legion
curl -s -X PATCH "$TOKEN_API_URL/api/instances/$INSTANCE_ID/legion" \
  -H "Content-Type: application/json" \
  -d "{\"legion\": \"$LEGION\"}"

# Set tab_name to persona title (if current name is generic like "claude-code")
curl -s -X PATCH "$TOKEN_API_URL/api/instances/$INSTANCE_ID/rename" \
  -H "Content-Type: application/json" \
  -d "{\"tab_name\": \"$PERSONA_TITLE\"}"
```

**No sync flip.** The state-hook dispatcher (`_dispatch_custodes_intervention` in `main.py`) resolves the Custodes by **persona + rank** (`resolve_live_persona_instance`), not by `synced`/`instance_type=sync`, so registering a custodes does NOT require flipping sync. Sync is a runtime mode the morning session owns. (An ad-hoc custodes in a non-persona pane is unusual; setting `legion: custodes` above is enough for the dispatcher to find it by identity.)

### Step 5: Confirm

Report what was set:
```
Persona registered: Custodes
  legion: custodes
  tab_name: custodes
  instance: <id prefix>
```

### Step 6: Inject persona context

After registration, the persona file content is already in context (you read it in Step 1). Acknowledge the persona's identity and operating principles from the file — you are now operating as that persona.

---

## Error Cases

- **No TOKEN_API_URL:** Report that Token-API is unreachable. The persona file can still be read for context, but DB registration fails.
- **Instance not found:** The instance may not have registered yet (race condition on startup). Suggest retrying after a few seconds.
- **Singleton demotion:** Setting `legion: custodes` will demote any other active Custodes instance to `astartes`. This is expected — report the demotion.

---

## Notes

- This skill does NOT restart the instance or transplant. It's a hot update.
- The persona file content provides operating context; the DB write provides fleet visibility.
- For primarch personas, the `primarch` field in the DB is set by the primarch launcher at startup. This skill sets `legion` only — it doesn't override `primarch`.
