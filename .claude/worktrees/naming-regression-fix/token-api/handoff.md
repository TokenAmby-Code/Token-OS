# TTS Debugging Handoff

## Current State (2026-01-26)

**Status**: Fix applied, testing sanitization.

## Fix Applied

Added sanitization in `main.py` lines 3379-3399 (in stop hook handler):
- Strips markdown headers (`#`, `##`, `###`, etc.)
- Strips `**bold**`, `*italic*`, `__bold__`, `_italic_`
- Strips `` `inline code` `` and code blocks
- Strips bullet points (`-`, `*`, `+`) and numbered lists
- Converts newlines to spaces
- Normalizes multiple spaces

## Max Length

Default max length is 500 characters. Configure via `~/.claude/.tts-config.json`:
```json
{"enabled": true, "maxLength": 1000}
```

## Testing

Verify stop hook TTS correctly strips markdown formatting.
