---
name: discord
description: Discord daemon and CLI shorthand for Token-OS operator messaging. Use when sending, reading, asking, declaring, checking, or managing Discord channels, threads, DMs, subscriptions, daemon status, or fragile voice transcription/routing through tmuxctld.
---

# Discord

Discord is the local Token-OS operator messaging bridge. The daemon runs from `discord-daemon/` and exposes the `discord` CLI for channel, thread, DM, ask/poll, declare, subscription, and voice workflows.

## Surfaces

- CLI: `discord send|read|ask|poll|declare|dm|subscribe|status|channels|thread|voice`.
- Daemon: `discord-daemon status|logs|start|restart|stop`.
- Runtime/config: `~/runtimes/Token-OS/live/discord-daemon/`, `~/.discord-cli/config.json`, `~/.discord-cli/logs/`, `~/.discord-cli/pending/`.
- Token-API ingest: `POST $TOKEN_API_URL/api/discord/message` and `events.event_type=discord_message`.
- Voice path: bot joins voice, records/transcribes, then routes text through Token-API/tmuxctld to the addressed pane/worker.

## Voice Caveat

Voice chat routing is fragile and should be treated as an operator-input transport, not a reliable command bus. Verify transcript text and target routing before acting on high-risk instructions.

## Safe Checks

```bash
discord status
discord channels
discord read fleet --limit 10
discord-daemon status
```

## Do Not

- Do not post to humans for dogfood unless the task asks for external messaging.
- Do not hardcode guild/channel IDs; use channel aliases or `~/.discord-cli/config.json`.
- Do not bypass the daemon by scripting Discord tokens directly; the bot token belongs in macOS Keychain.
- Do not restart/stop the daemon unless the task is daemon maintenance or recovery.
- Do not trust voice transcripts for destructive actions without confirmation/proof.
