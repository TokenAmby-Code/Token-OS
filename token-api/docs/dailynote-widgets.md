# Daily-note widgets

Token-API can maintain refreshable Obsidian callout widgets inside existing daily notes without clobbering operator edits.

## Managed callout wire format

Widgets are bounded by invisible HTML markers:

```markdown
<!-- callout:now BEGIN -->
> [!info]+ NOW
> **Block:** 16:23 MST live snapshot
> **Balance:** +12min · timer mode: WORKING
<!-- callout:now END -->
```

Only the region between matching `BEGIN` / `END` markers for the same `callout_id` is replaced. If no marker pair exists, the block is appended to the end of the note. A partial marker pair is rejected as malformed.

Writes use `tempfile.NamedTemporaryFile(dir=note_path.parent, delete=False)` plus `os.replace`, with an mtime guard and one retry if the file changes during the read/modify/write cycle.

## API

`PUT /api/daily-note/callout`

```json
{
  "callout_id": "now",
  "content": "**Block:** test\n**Posture:** test",
  "title": "NOW",
  "callout_type": "info",
  "date": "2026-05-09"
}
```

- `callout_id`: lowercase slug, `[a-z0-9_-]+`
- `content`: markdown body, max 10 KiB; each line is quoted as callout body
- `title`: optional, defaults to `callout_id.upper()`
- `callout_type`: `info`, `success`, `warning`, `note`, `tip`, `abstract`, or `example`
- `date`: optional `YYYY-MM-DD`, defaults to server-local today

The route returns `404` if the daily note does not exist. Daily-note creation remains Obsidian/Templater-owned.

## Active callout IDs

| ID | Owner | Schedule | Purpose |
| --- | --- | --- | --- |
| `now` | Token-API `now_widget.py` | APScheduler interval, 60s | Live timer / active instance / geofence / cascade snapshot |

## Adding a widget

1. Compose markdown body in a small module; do not write files by hand.
2. Call `dailynote_callout.apply_callout(note_path, callout_id, content, title, callout_type)`.
3. Register one scheduler job at startup. Prefer direct invocation of the pure writer over HTTP self-calls inside Token-API.
4. Add tests for body composition and marker replacement/idempotency.
5. Add the new `callout_id` to the table above.
