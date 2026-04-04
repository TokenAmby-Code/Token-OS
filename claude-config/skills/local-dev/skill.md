---
name: local-dev
description: "Launch local dev and test via browser. Usage: /local-dev [amendment]"
user_invocable: true
---

# Local Dev

Launch AskCivic local dev environment, open Helium browser, and run interactive tests via `window.__agentAPI`.

## Usage

- `/local-dev` — launch dev env, open browser, verify widget loaded, report state
- `/local-dev amendment` — full amendment generator test flow with pass/fail evaluation

## Phase 1: Check / Launch Local Dev

1. Check if already running:
   ```bash
   curl -sf http://localhost:8080/health && echo "Backend up" || echo "Backend down"
   curl -sf http://localhost:3000/ > /dev/null && echo "Frontend up" || echo "Frontend down"
   ```

2. If **both** are up, skip to Phase 2.

3. If **either** is down (partial state), kill both and restart cleanly.
   `make dev-web` launches backend (port 8080) then frontend (port 3000) as coupled processes
   with a shared trap — a partial state means a previous run died uncleanly:
   ```bash
   # Kill any orphaned processes on dev ports
   lsof -ti:8080 | xargs kill -9 2>/dev/null; lsof -ti:3000 | xargs kill -9 2>/dev/null
   ```

4. **Check Cloud SQL Proxy** before launching backend:
   ```bash
   pgrep -f cloud-sql-proxy > /dev/null && echo "proxy running" || echo "proxy NOT running"
   ```
   If not running, start it:
   ```bash
   cd ~/worktrees/askCivic/wt-amendment-generator && \
   GOOGLE_APPLICATION_CREDENTIALS=deploy/dev-service-account.json \
   ./cloud-sql-proxy pax-dev-469018:us-central1:pax-sql --port 5432 &
   ```
   Wait 3s for proxy to connect before launching backend.
   **Without the proxy, backend will crash on startup** with `psycopg_pool.PoolTimeout` after 30s.

5. Launch from the **current worktree** (not hardcoded):
   ```bash
   cd ~/worktrees/askCivic/wt-amendment-generator && make dev-web
   ```
   Run via `Bash` with `run_in_background: true`.

6. Poll until **both** are ready (max 90s, every 3s):
   ```bash
   for i in $(seq 1 30); do
     curl -sf http://localhost:8080/health > /dev/null && curl -sf http://localhost:3000/ > /dev/null && echo "ready" && break || sleep 3
   done
   ```
   **Note:** Frontend (Vite) can start even if backend crashed. Always verify BOTH endpoints.

## Phase 2: Open Helium + Connect

1. Request Helium access:
   - `mcp__computer-use__request_access` with application "Helium"

2. Open Helium:
   - `mcp__computer-use__open_application` with application_name "Helium"
   - Wait 2s for launch

3. Navigate to widget:
   - `mcp__chrome-devtools__navigate_page` to `https://dev.askcivic.com` (deployed dev)
   - **Why not localhost:** Clerk production keys reject `localhost` origin — the chat modal renders blank. Use the deployed dev site for any flow requiring authentication.
   - For frontend-only testing (no auth), `http://localhost:3000` works for the landing page and demo page.
   - Wait 3s for page load

4. Verify widget loaded:
   ```js
   // mcp__chrome-devtools__evaluate_script
   () => JSON.stringify(window.__agentAPI.describeUI())
   ```
   Confirm `__agentAPI.ready === true`. If `__agentAPI` is undefined, check console errors.

5. Take baseline screenshot:
   - `mcp__chrome-devtools__take_screenshot`

## Phase 3: AgentAPI Interaction Patterns

All widget interaction uses `mcp__chrome-devtools__evaluate_script` calling `window.__agentAPI`. These are the canonical patterns.

**Async note:** `press()` and `waitFor()` return Promises. `fill()`, `getState()`, `describeUI()` are synchronous. For async methods, `evaluate_script` must use `awaitPromise: true` or the expression must `.then()` to a serializable value. Use the `.then()` pattern below for maximum compatibility.

### Send a chat message
```js
// fill is sync, press is async — chain them
() => {
  window.__agentAPI.fill('message-input', 'YOUR MESSAGE HERE');
  return window.__agentAPI.press('send-message').then(r => JSON.stringify(r));
}
```

### Wait for bot response to complete
```js
// waitFor returns Promise<boolean> — must .then() for evaluate_script
() => window.__agentAPI.waitFor(
  () => !document.querySelector('.chat-message.bot.thinking'),
  45000
).then(ok => ok ? 'done' : 'timeout')
```

### Read the last bot response
```js
// Sync — no .then() needed
() => {
  const msgs = document.querySelectorAll('.chat-message.bot .message-content');
  return msgs.length ? msgs[msgs.length - 1].innerText : null;
}
```

### Switch mode
```js
// press() is async
// Modes: 'switch-mode-chat', 'switch-mode-plan', 'switch-mode-amendment', 'switch-mode-sow'
() => window.__agentAPI.press('switch-mode-amendment').then(r => JSON.stringify(r))
```

### Upload a file (amendment mode)
Take a snapshot, find the upload button (e.g. `uid=1_46` "Upload a contract file for amendment"), then use `mcp__chrome-devtools__upload_file` with the button's uid and the file path. CDP intercepts the file chooser event — no Finder dialog opens.
```js
// Do NOT click the button first — that opens a native Finder dialog that blocks computer-use screenshots.
// Instead, use upload_file directly on the button uid:
mcp__chrome-devtools__upload_file(uid="1_46", filePath="/path/to/file.txt")
```
**Warning:** If you click the upload button via `click` or `press` before using `upload_file`, a native Finder dialog opens. This dialog blocks `computer-use` screenshots (30s timeout). Press Escape via chrome-devtools to dismiss it, then use `upload_file` on the uid instead.

### Get all available actions and inputs
```js
() => JSON.stringify({
  actions: window.__agentAPI.getActions(),
  inputs: window.__agentAPI.getInputs(),
  state: window.__agentAPI.getState()
})
```

### Screenshot
Use `mcp__chrome-devtools__take_screenshot` at each significant step. Save key screenshots to `/tmp/local-dev-test-*.png`.

## Phase 4: Amendment Test Flow

**Only when `/local-dev amendment` is invoked.**

### Step 1: Switch to Amendment Mode
```js
() => window.__agentAPI.press('switch-mode-amendment').then(r => JSON.stringify(r))
```
Screenshot after mode switch.

### Step 2: Upload Test Contract
File: `~/worktrees/askCivic/wt-amendment-generator/tests/fixtures/contracts/sample_3_amendments.txt`

This is Cook County Contract No. 2028-18170-A3 with 3 prior amendments (Consolidated Building Services Inc.).

**Upload via CDP** (not click — see upload pattern above):
1. Take snapshot to find the upload button uid (look for "Upload a contract file for amendment")
2. Use `mcp__chrome-devtools__upload_file` with that uid and the file path
3. Wait for bot response (60s timeout — file parsing takes time)

### Step 3: Walk Through Chat Conversation
The amendment flow is a 2-exchange conversation after upload:

1. **Bot confirms extraction, asks about intent:**
   Bot identifies vendor, contract #, prior amendments, and the next amendment number.
   Send: `Extend the contract term by 12 months and increase compensation by $75,000 for additional maintenance scope`

2. **Bot asks about authority / who approves:**
   Send: `Board of Commissioners of Cook County`

Wait for bot response after each message (use the waitFor pattern above, 90s timeout for final generation).
Screenshot after each exchange.

**Note:** The bot auto-detects the next amendment number from prior amendments. For sample_3_amendments.txt with 3 prior amendments, the bot may say Amendment No. 4 or 5 depending on how it counts. Do not hardcode the expected number.

### Step 4: Read Draft Amendment
After the final bot response, read the draft Whereas clauses:
```js
() => {
  const msgs = document.querySelectorAll('.chat-message.bot .message-content');
  return msgs.length ? msgs[msgs.length - 1].innerText : null;
}
```

The bot generates a **draft review** with Whereas clauses and presents "Approved" / "Request changes" buttons.
This is a two-stage flow: draft → final document.

### Step 5: Evaluate Draft Against Checklist

**Draft stage** (Whereas clauses only — headers/signatures come after approval):

| # | Check | How to Verify | Stage |
|---|-------|---------------|-------|
| 1 | Vendor name in extraction | Bot mentions actual vendor name when confirming upload | Draft |
| 2 | Scope from user input | Whereas clause reflects user's stated scope, not boilerplate | Draft |
| 3 | Amount correct | Whereas clause states the correct dollar increase | Draft |
| 4 | End date auto-calculated | Extended date = previous end date + requested extension | Draft |
| 5 | Contract number identified | Bot references correct contract number from document | Draft |
| 6 | Amendment number auto-detected | Bot determines next amendment number from prior amendments | Draft |
| 7 | Professional Whereas formatting | Proper "Whereas," clause structure | Draft |
| 8 | Review buttons present | "Approved" and "Request changes" buttons appear | Draft |

**Post-approval stage** (click "Approved" to evaluate):

| # | Check | How to Verify | Stage |
|---|-------|---------------|-------|
| 9 | Header with contract#/amendment#/vendor | Formal header block in final document | Final |
| 10 | All prior amendments listed | References prior amendments with details | Final |
| 11 | Signature blocks present | Document ends with signature blocks | Final |
| 12 | Legal boilerplate present | "All other terms remain in effect" language | Final |
| 13 | Original contract referenced | References original contract terms | Final |

### Step 6: (Optional) Click Approved for Final Document
```js
// Find and click the "Approved" button in the snapshot
// Then wait for final document generation (90s timeout)
```

### Step 7: Report Results
Output a pass/fail table for whichever stage was reached. Include the full generated text in a collapsible details block for review.

## Key Selectors Reference

### Actions (`__agentAPI.press()`)
| Name | Purpose |
|------|---------|
| `send-message` | Send current message |
| `new-chat` | Start new conversation |
| `expand-chat` | Expand chat window |
| `toggle-chat` | Show/hide chat |
| `toggle-chat-files` | Toggle file panel |
| `chat-files-upload` | Upload button in files panel |
| `chat-files-file-input` | Hidden file input element |
| `toggle-partner-kb` | Toggle knowledge base panel |
| `switch-mode-chat` | Switch to chat mode |
| `switch-mode-amendment` | Switch to amendment mode |
| `switch-mode-sow` | Switch to SOW mode |
| `switch-mode-plan` | Switch to plan mode |

### Inputs (`__agentAPI.fill()`)
| Name | Purpose |
|------|---------|
| `message-input` | Chat message text input |

### CSS Selectors (for DOM queries)
| Selector | Purpose |
|----------|---------|
| `.chat-message.bot .message-content` | Bot response text |
| `.chat-message.user .message-content` | User message text |
| `.chat-message.bot.thinking` | Bot is still generating |
| `.chat-window.expanded` | Chat in expanded state |

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| localhost:3000 no response | Check `make dev-web` output in background task; verify `.env` exists in worktree |
| `__agentAPI` undefined | Widget JS not loaded — check `mcp__chrome-devtools__list_console_messages` for errors |
| `list_pages` returns empty | Helium not open or chrome-devtools MCP disconnected — reopen Helium |
| Upload 401 | Auth required — user must log in manually via Clerk |
| Chat toggle shows blank/nothing | Clerk auth modal failing — production keys reject `localhost` origin. See auth notes below |
| Backend crashes on startup | Cloud SQL Proxy not running — `psycopg_pool.PoolTimeout` after 30s. Start proxy first |
| Frontend up but backend down | `make dev-web` starts Vite even if backend crashed. Check backend health separately |
| Mode switch does nothing | Action not registered yet — wait for widget to fully initialize, retry |
| `take_snapshot` huge | Use `evaluate_script` with `describeUI()` instead for lighter introspection |

## Key Files

| File | Role |
|------|------|
| `widget/src/agentAPI/index.js` | Full agentAPI: press, fill, getState, waitFor, sequence, describeUI |
| `Makefile` | `dev-web` target: backend (8080) + frontend (3000) |
| `tests/fixtures/contracts/sample_3_amendments.txt` | Test contract: Cook County 2028-18170-A3, 3 prior amendments |
| `tests/e2e/helpers/selectors.js` | Canonical data-agent-action and CSS selector names |

Paths are relative to whichever worktree is active. The skill runs from `~/worktrees/askCivic/wt-amendment-generator/` by default.

## Design Notes & Assumptions

- **agentAPI-first:** All widget interaction through `evaluate_script` → `window.__agentAPI.*`. Only fall back to raw chrome-devtools click/fill for elements outside the widget.
- **No cleanup on exit:** `make dev-web` uses trap for cleanup. Skill leaves dev running across invocations. Re-invoking `/local-dev` while dev is already running skips straight to Phase 2.
- **Auth on localhost:** Clerk production keys reject `localhost` origin — the modal opens but shows nothing. The widget `.env.dev` uses keys scoped to `dev.askcivic.com`. To work around this, either: (a) use Clerk development keys in a `.env.local`, or (b) access via `dev.askcivic.com` instead of `localhost`. For unauthenticated testing (amendment mode doesn't require login to generate), the widget still functions — just the login modal is broken.
- **Amendment worktree:** Prefer `~/worktrees/askCivic/wt-amendment-generator/` for amendment testing — has the latest flow.
- **Async contract:** `press()`, `waitFor()`, `waitForElement()`, `sequence()` return Promises. `fill()`, `getState()`, `getActions()`, `getInputs()`, `describeUI()` are synchronous. All `evaluate_script` calls for async methods must use `.then()` to resolve to a serializable return value.
- **Lifecycle:** `make dev-web` starts backend as a background process, waits for health check, then starts frontend in foreground. The shell trap kills the backend when the frontend exits. A partial state (one up, one down) means a previous run died uncleanly — kill both ports and restart.
- **Port assumptions:** Backend always on 8080, frontend always on 3000. These are hardcoded in the Makefile and Vite config.
- **Helium + CDP:** Helium must already be running with `--remote-debugging-port=9222`. The skill does NOT launch Helium with this flag — it opens Helium via computer-use (which brings an existing instance to front). If CDP isn't responding, user must restart Helium manually with the flag.
