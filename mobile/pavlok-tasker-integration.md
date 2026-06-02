# Pavlok Tasker Integration

## Problem

The Pavlok cloud API (`api.pavlok.com/api/v5/stimulus/send`) returns 200 but stimuli (zap, beep, vibe) are not reliably delivered to the watch. The delivery chain is: Token-API server → Pavlok cloud → phone app → BLE → watch. The cloud-to-phone or phone-to-BLE hop appears to silently drop stimuli.

## Goal

Bypass the cloud API. Have Token-API send a request to the phone (via Tasker HTTP server or MacroDroid), and have the phone trigger the Pavlok stimulus locally — going straight phone → BLE → watch.

## Current Infrastructure

- **Token-API** runs on desktop at `localhost:7777`
- **Phone** (Samsung S24) reachable via Tailscale at `100.102.92.24`
- **MacroDroid** already runs an HTTP server on port 7777 for app enforcement (`/enforce?action=disable&app=youtube`)
- **Tasker** newly installed on phone
- **Pavlok app** installed on phone, handles BLE connection to watch

## Approach Options

### Option A: Tasker HTTP endpoint
- Tasker listens on a port (e.g., 7778) for HTTP requests
- Token-API sends `POST http://100.102.92.24:7778/pavlok?type=zap&value=50`
- Tasker profile triggers Pavlok app intent or Tasker Pavlok plugin action

### Option B: MacroDroid webhook
- Add a new MacroDroid macro triggered by HTTP `/pavlok?type=zap&value=50`
- Macro sends intent to Pavlok app
- Reuses existing MacroDroid HTTP server on port 7777

### Option C: Tasker Pavlok plugin
- Check if there's a Tasker plugin for Pavlok that can trigger stimuli directly
- Would be the cleanest integration

## Token-API Side (already done)

Endpoints exist in `main.py`:
- `POST /api/pavlok/zap` — manual trigger
- `POST /api/pavlok/toggle` — enable/disable
- `GET /api/pavlok/status` — current state

Helper function `send_pavlok_stimulus()` currently calls the cloud API. Once phone-side is working, update it to call the phone endpoint instead.

Three enforcement hooks already wired:
1. Desktop distraction blocked → zap
2. Phone distraction blocked → zap
3. Break time exhausted → zap

## TODO

- [ ] Investigate Tasker Pavlok plugin / intent options
- [ ] Set up HTTP listener on phone (Tasker or MacroDroid)
- [ ] Test local phone → Pavlok BLE delivery
- [ ] Update `send_pavlok_stimulus()` in main.py to call phone instead of cloud API
