# /computer-use — Mac Automation & Browser Control

Load this skill when working with GUI automation, browser interaction, native app control, or any task requiring visual/physical interaction with the Mac Mini.

## MCP Stack

Two MCPs work in tandem for full machine control:

| MCP | Package | Scope | Tier System |
|-----|---------|-------|-------------|
| **computer-use** | Built-in (Anthropic) | Screenshots, mouse, keyboard, native apps | Browsers=read, Terminals=click, Everything else=full |
| **chrome-devtools** | `chrome-devtools-mcp` | DOM-level browser control via CDP | No tier restrictions — full navigate/click/type/fill |

### Why Both

- **computer-use** alone cannot interact with browsers (read-only tier) or type into terminals (click-only tier)
- **chrome-devtools** alone cannot control native apps (Finder, System Settings, Notes, etc.)
- Together: full GUI automation of any application on the machine

### Tool Selection Priority

1. **Dedicated MCP** (Slack, Gmail, Calendar, etc.) — API-backed, fastest
2. **chrome-devtools** — for anything in a browser. DOM-aware, no pixel guessing
3. **computer-use** — for native desktop apps and cross-app workflows

## Helium Browser

Helium is the browser on this machine. Chromium-based, privacy-first, open source (ungoogled-chromium fork by imputnet).

- **Website**: https://helium.computer
- **GitHub**: https://github.com/imputnet/helium
- **Engine**: Chromium 146+ (supports Chrome extensions natively)
- **Why not Chrome**: No Google telemetry. Full Chrome extension compat without the spyware.

### Launch Requirement

Helium must be launched with the remote debugging flag for chrome-devtools MCP to connect:

```bash
# Kill existing instance first — flag is ignored if Helium is already running
pkill -a Helium
/Applications/Helium.app/Contents/MacOS/Helium --remote-debugging-port=9222 &
```

The `--remote-debugging-port=9222` flag enables Chrome DevTools Protocol (CDP). Without it, port 9222 may listen but CDP endpoints return 404.

### MCP Config

Project-level in `.claude.json`:

```json
{
  "chrome-devtools": {
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "chrome-devtools-mcp", "--browserUrl=http://127.0.0.1:9222"]
  }
}
```

Key: use `--browserUrl=http://127.0.0.1:9222`, NOT `--autoConnect`. AutoConnect looks for Chrome's default DevTools port file at `~/Library/Application Support/Google/Chrome/DevToolsActivePort`, which doesn't exist for Helium.

### Verify CDP Is Working

```bash
curl -s http://127.0.0.1:9222/json/version | python3 -m json.tool
```

Should return Browser, Protocol-Version, webSocketDebuggerUrl. If 404 or empty, Helium wasn't launched with the flag.

## chrome-devtools MCP — Tool Reference

### Navigation
- `navigate_page` — go to URL, back, forward, reload
- `new_page` — open URL in new tab
- `list_pages` / `select_page` / `close_page` — tab management

### Interaction
- `click` — click element by uid (from snapshot)
- `fill` — set input value (**caveat**: doesn't fire DOM input events — React/Vue send buttons may stay disabled)
- `type_text` — keystroke-by-keystroke typing into focused input (fires events correctly)
- `press_key` — keyboard shortcuts, Enter, Tab, etc.
- `hover` / `drag` — hover states, drag-and-drop
- `fill_form` — batch fill multiple form elements
- `upload_file` — file upload via input element
- `handle_dialog` — accept/dismiss browser dialogs

### Observation
- `take_snapshot` — a11y tree with uids (preferred over screenshots — faster, structured)
- `take_screenshot` — visual capture of page or element
- `evaluate_script` — run arbitrary JS in page context
- `list_network_requests` / `get_network_request` — inspect network traffic
- `list_console_messages` / `get_console_message` — read console output

### Performance & Audit
- `lighthouse_audit` — accessibility, SEO, best practices
- `performance_start_trace` / `performance_stop_trace` — Core Web Vitals, load perf
- `take_memory_snapshot` — heap snapshot for leak debugging

### Known Quirks

1. **`fill` vs `type_text`**: `fill` sets value directly (fast, good for selects). `type_text` fires keystrokes (triggers React state). For text inputs in reactive frameworks, prefer `type_text` or `fill` + manual keystroke to trigger the binding.
2. **Snapshot uids change on re-render**: always take a fresh snapshot before clicking. Stale uids will fail.
3. **`take_snapshot` > `take_screenshot`**: snapshots return structured a11y data with clickable uids. Screenshots are for visual verification only.

## computer-use MCP — Key Capabilities

### Native App Control
- `request_access` — must be called first with app names. User approves per-app.
- `open_application` — bring app to front or launch it
- `screenshot` — full desktop capture (app allowlist filters other apps)
- `left_click` / `right_click` / `double_click` / `triple_click` — pixel-coordinate clicks
- `type` / `key` — keyboard input (only works in full-tier apps)
- `scroll` — scroll at coordinates
- `computer_batch` — batch multiple actions in one round-trip (fast)
- `zoom` — high-res crop of screenshot region for reading small text

### Tier Restrictions

| Tier | Apps | Can Do | Cannot Do |
|------|------|--------|-----------|
| **full** | Finder, Notes, System Settings, most apps | Everything | — |
| **click** | Terminal, iTerm, VS Code, JetBrains | See, left-click, scroll | Type, right-click, modifier-clicks, drag |
| **read** | Safari, Helium, Chrome, Firefox | See only | Click, type, scroll |

For browsers: use chrome-devtools MCP instead. For terminals: use Bash tool instead.

## Automation Opportunities

### What This Unlocks

With computer-use + chrome-devtools + Helium, the Mac Mini becomes a fully automatable workstation:

- **Web app testing**: Navigate sites, fill forms, click through flows, verify UI state, run Lighthouse audits
- **Web scraping/research**: Navigate to pages, take snapshots, extract structured data from a11y trees
- **Native app automation**: Control Finder, System Settings, Notes, Obsidian, Discord — anything with a GUI
- **Cross-app workflows**: Copy data from a browser into a native app, screenshot results, verify state
- **Visual verification**: Take screenshots to confirm state before/after actions
- **Performance profiling**: Trace page loads, analyze Core Web Vitals, capture heap snapshots
- **Network inspection**: Monitor API calls, inspect request/response payloads from browser
- **Form automation**: Fill complex multi-step forms across pages
- **File management**: Upload/download files through browser interfaces

### Integration with Existing Fleet

| Pattern | How It Works |
|---------|-------------|
| **Cron + browser** | Scheduled agents can open Helium, navigate to dashboards, take screenshots, report status |
| **Session docs + web research** | Agents can research topics in-browser and write findings to session docs |
| **Obsidian + web** | Open Obsidian via computer-use, cross-reference with web content via chrome-devtools |
| **Discord + browser** | Monitor Discord via computer-use (read tier), interact via Discord MCP or bot |
| **askCivic testing** | Full end-to-end testing of the widget: auth, chat, file upload, mode switching |

### Startup Checklist

Before using browser automation in any session:

1. Ensure Helium is running with `--remote-debugging-port=9222`
2. Verify CDP: `curl -s http://127.0.0.1:9222/json/version`
3. chrome-devtools MCP should show "Connected" in `/mcp`
4. For native apps: call `request_access` with needed app names
5. For browsers: prefer chrome-devtools tools over computer-use

### Cron Automation Consideration

For unattended/cron automation, Helium must already be running with the debug flag. Consider:

```bash
# In a startup script or launchd plist
pgrep -x Helium || /Applications/Helium.app/Contents/MacOS/Helium --remote-debugging-port=9222 &
```

This ensures Helium is always available for automated browser tasks without manual intervention.
