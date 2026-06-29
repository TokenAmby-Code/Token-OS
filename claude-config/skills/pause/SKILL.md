---
name: pause
description: "Pause the Mechanicus fleet. Disables all enabled cron jobs or specific factions and stores state for deterministic unpause. Usage: /pause [commanders]"
---

# Pause

Pause the cron fleet. Stores which jobs were enabled so `$unpause` restores exactly the previous state.

## Usage

- `/pause` — pause all factions (mechanicus, custodes, dorn, emperor)
- `/pause mechanicus` — pause only mechanicus jobs
- `/pause mechanicus custodes` — pause multiple factions

## Process

Parse optional commander arguments, call the fleet pause API, then report what was paused.

```bash
# If no arguments (pause all):
curl -s -X POST "$TOKEN_API_URL/api/fleet/pause" -H "Content-Type: application/json" -d '{}'

# If specific commanders:
curl -s -X POST "$TOKEN_API_URL/api/fleet/pause" -H "Content-Type: application/json" -d '{"commanders": ["mechanicus"]}'
```

Report the result: which jobs were paused, how many. If the fleet was already paused, say so.

## Constraint

This is live-side-effecting. Do not run it for syntax validation or dogfood unless the user explicitly asked to pause the fleet.
