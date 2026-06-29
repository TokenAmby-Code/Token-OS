---
name: unpause
description: "Unpause the fleet. Re-enables exactly the jobs that were paused, restoring previous state. Usage: /unpause"
---

# Unpause

Unpause the cron fleet. Re-enables exactly the jobs that `$pause` disabled, restoring the previous fleet state deterministically.

## Usage

- `/unpause` — restore all paused jobs

## Process

```bash
curl -s -X POST "$TOKEN_API_URL/api/fleet/unpause"
```

Report the result: which jobs were unpaused, how many. If no pause state exists, say so.

## Constraint

This is live-side-effecting. Do not run it for syntax validation or dogfood unless the user explicitly asked to unpause the fleet.
