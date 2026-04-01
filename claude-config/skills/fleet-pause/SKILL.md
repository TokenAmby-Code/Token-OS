---
name: pause
description: "Pause the Mechanicus fleet. Disables all enabled cron jobs (or specific factions) and stores state for deterministic unpause. Usage: /pause [commanders]"
user_invocable: true
---

# Fleet Pause

Pause the cron fleet. Stores which jobs were enabled so `/unpause` restores exactly the previous state.

## Usage

- `/pause` — pause all factions (mechanicus, custodes, dorn, emperor)
- `/pause mechanicus` — pause only mechanicus jobs
- `/pause mechanicus custodes` — pause multiple factions

## Process

1. Parse the optional commander arguments from the user's input
2. Call the fleet pause API
3. Report what was paused

```bash
# If no arguments (pause all):
curl -s -X POST localhost:7777/api/fleet/pause -H "Content-Type: application/json" -d '{}'

# If specific commanders:
curl -s -X POST localhost:7777/api/fleet/pause -H "Content-Type: application/json" -d '{"commanders": ["mechanicus"]}'
```

Report the result: which jobs were paused, how many. If the fleet was already paused, say so.
---
name: pause
description: "Pause the Mechanicus fleet. Disables all enabled cron jobs (or specific factions) and stores state for deterministic unpause. Usage: /pause [commanders]"
user_invocable: true
---

# Fleet Pause

Pause the cron fleet. Stores which jobs were enabled so `/unpause` restores exactly the previous state.

## Usage

- `/pause` — pause all factions (mechanicus, custodes, dorn, emperor)
- `/pause mechanicus` — pause only mechanicus jobs
- `/pause mechanicus custodes` — pause multiple factions

## Process

1. Parse the optional commander arguments from the user's input
2. Call the fleet pause API
3. Report what was paused

```bash
# If no arguments (pause all):
curl -s -X POST localhost:7777/api/fleet/pause -H "Content-Type: application/json" -d '{}'

# If specific commanders:
curl -s -X POST localhost:7777/api/fleet/pause -H "Content-Type: application/json" -d '{"commanders": ["mechanicus"]}'
```

Report the result: which jobs were paused, how many. If the fleet was already paused, say so.
