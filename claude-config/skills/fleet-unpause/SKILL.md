---
name: unpause
description: "Unpause the fleet. Re-enables exactly the jobs that were paused, restoring previous state. Usage: /unpause"
user_invocable: true
---

# Fleet Unpause

Unpause the cron fleet. Re-enables exactly the jobs that were disabled by `/pause`, restoring the previous fleet state deterministically.

## Usage

- `/unpause` — restore all paused jobs

## Process

```bash
curl -s -X POST localhost:7777/api/fleet/unpause
```

Report the result: which jobs were unpaused, how many. If no pause state exists, say so.
---
name: unpause
description: "Unpause the fleet. Re-enables exactly the jobs that were paused, restoring previous state. Usage: /unpause"
user_invocable: true
---

# Fleet Unpause

Unpause the cron fleet. Re-enables exactly the jobs that were disabled by `/pause`, restoring the previous fleet state deterministically.

## Usage

- `/unpause` — restore all paused jobs

## Process

```bash
curl -s -X POST localhost:7777/api/fleet/unpause
```

Report the result: which jobs were unpaused, how many. If no pause state exists, say so.
