# Token-API: FastAPI Local Server

## Overview

Replace mesh-pipe with **Token-API**, a general-purpose FastAPI local server that serves as the sole source of truth for:
- Claude instance registration
- Device identification (desktop vs SSH from phone)
- Notification routing
- Productivity gating
- Mode management (timer integration)

## Core Principle

**"I want a local server running on my computer that can accomplish tasks"**

Build the server around this purpose, then add functionality - not the other way around.

---

## Instance Registration

### Current State (to be replaced)
- `~/.claude/instance-registry.jsonl` - Claude Code CLI manages this
- `~/.claude/instance-profiles.json` - Static 4-profile system
- Separate `session_registry.json` in mesh-pipe for device association

### New State (centralized)
- FastAPI server is sole source of truth
- Claude instances register on startup, deregister on exit
- SSH detection happens at registration time via `SSH_CLIENT` env var

### Registration Flow

```
Claude Code starts
    │
    ├── Check SSH_CLIENT env var
    │   ├── Present: Parse source IP → identify device
    │   └── Absent: Local desktop session
    │
    ├── POST /api/instances/register
    │   {
    │     "instance_id": "claude-1736445600123",
    │     "tab_name": "Claude #1",  // if available
    │     "origin": {
    │       "type": "ssh" | "local",
    │       "source_ip": "100.102.92.24",  // from SSH_CLIENT
    │       "device_id": "pixel-phone"     // resolved from IP
    │     },
    │     "pid": 12345
    │   }
    │
    └── Server responds with assigned profile
        {
          "session_id": "uuid",
          "profile": {
            "name": "profile_1",
            "tts_voice": "Microsoft Zira Desktop",
            "notification_sound": "chimes.wav",
            "color": "#0099ff"
          }
        }
```

### SSH Detection Logic

```python
import os

def detect_origin():
    ssh_client = os.environ.get("SSH_CLIENT")

    if ssh_client:
        # Format: "source_ip source_port dest_port"
        source_ip = ssh_client.split()[0]
        device_id = resolve_device_from_ip(source_ip)
        return {
            "type": "ssh",
            "source_ip": source_ip,
            "device_id": device_id
        }
    else:
        return {
            "type": "local",
            "source_ip": None,
            "device_id": "desktop"
        }

def resolve_device_from_ip(ip: str) -> str:
    """Map Tailscale IPs to known devices"""
    DEVICE_IPS = {
        "100.102.92.24": "pixel-phone",
        "100.66.10.74": "desktop",
        # Add laptop later when needed
    }
    return DEVICE_IPS.get(ip, "unknown")
```

---

## Device Configuration

### Known Devices (server config)

```python
DEVICES = {
    "desktop": {
        "type": "local",
        "tailscale_ip": "100.66.10.74",
        "notification_method": "tts_sound",
        "tts_engine": "windows_sapi",  # via PowerShell
        "sound_player": "powershell"   # or paplay for Linux
    },
    "pixel-phone": {
        "type": "mobile",
        "tailscale_ip": "100.102.92.24",
        "notification_method": "webhook",
        "webhook_url": "http://100.102.92.24:7777/notify"
    }
    # laptop: future addition
}
```

### Notification Routing

When a Claude instance completes (or session ends):

1. Look up instance in registry
2. Get origin device from instance record
3. Route notification to that device:
   - **Desktop origin**: Play TTS + sound locally
   - **Phone origin**: POST to phone's webhook

---

## Profile Assignment

### Current Limitation
- 4 static profiles (profile_1 through profile_4)
- Limited by available TTS voices (Microsoft David, Zira)

### Improved Approach

```python
class ProfilePool:
    """Dynamic profile assignment with voice rotation"""

    def __init__(self):
        self.available_voices = self.detect_voices()
        self.available_sounds = self.scan_sounds()
        self.assignments = {}  # instance_id -> profile

    def detect_voices(self) -> list[str]:
        """Query available Windows SAPI voices"""
        # PowerShell: Get-ChildItem HKLM:\SOFTWARE\Microsoft\Speech\Voices\Tokens
        # Returns: ["Microsoft David Desktop", "Microsoft Zira Desktop", ...]
        pass

    def assign_profile(self, instance_id: str) -> dict:
        """Assign next available voice/sound combo"""
        used_voices = {p["voice"] for p in self.assignments.values()}

        # Find first unused voice, or cycle if all used
        for voice in self.available_voices:
            if voice not in used_voices:
                break

        # Assign unique sound from pool
        sound = self.pick_sound(len(self.assignments))

        profile = {
            "voice": voice,
            "sound": sound,
            "color": self.generate_color(len(self.assignments))
        }
        self.assignments[instance_id] = profile
        return profile
```

### Voice Pack Expansion (future)

When you install additional voice packs, the server auto-detects them:
- Windows: Query registry for SAPI voices
- Linux fallback: Check for espeak voices, festival voices

---

## API Endpoints

### Instance Management

```
POST   /api/instances/register     # Register new instance
DELETE /api/instances/{id}         # Deregister instance
GET    /api/instances              # List active instances
GET    /api/instances/{id}         # Get instance details
POST   /api/instances/{id}/heartbeat  # Keep-alive (optional)
```

### Notifications

```
POST   /api/notify                 # Send notification
POST   /api/notify/tts             # TTS only
POST   /api/notify/sound           # Sound only
```

### Productivity & Mode (migrated from mesh-pipe)

```
GET    /api/productivity           # Check if productive
GET    /api/mode                   # Current work mode
POST   /api/mode                   # Set work mode
GET    /api/state                  # Full state
POST   /api/state                  # Update state vars
```

### Device Management

```
GET    /api/devices                # List known devices
POST   /api/devices/register       # Register new device
```

### App Blocking (phone integration)

```
GET    /api/check?app={app}        # Is app allowed?
GET    /?app={app}                 # Legacy endpoint (MacroDroid compat)
```

### Obsidian Integration

```
POST   /api/obsidian/command       # Execute command
POST   /api/obsidian/eval          # Eval JS
GET    /api/vault/*                # Vault queries
```

---

## Project Structure

```
/mnt/imperium/Token-OS/token-api/
├── main.py                 # FastAPI app entry point
├── config.py               # Configuration & device definitions
├── models/
│   ├── instance.py         # Instance registration models
│   ├── device.py           # Device models
│   └── notification.py     # Notification models
├── routers/
│   ├── instances.py        # Instance registration endpoints
│   ├── notifications.py    # Notification endpoints
│   ├── productivity.py     # Productivity/mode endpoints
│   ├── devices.py          # Device management
│   ├── blocking.py         # App blocking (MacroDroid)
│   └── obsidian.py         # Obsidian integration
├── services/
│   ├── tts.py              # TTS engine abstraction
│   ├── sound.py            # Sound playback
│   ├── profiles.py         # Profile pool management
│   └── webhook.py          # Webhook notifications
├── state/
│   ├── instances.json      # Persisted instance registry
│   ├── state.json          # Runtime state (mode, focus, etc)
│   └── config.json         # Device configs, rules
└── token-api.service       # systemd unit file
```

---

## Migration Path

### Phase 1: Parallel Operation
1. Build Token-API with instance registration
2. Run alongside existing mesh-pipe on different port (e.g., 7778)
3. Claude hooks register with both (temporarily)
4. Validate Token-API works correctly

### Phase 2: Feature Migration
1. Port productivity checking to Token-API
2. Port mode management
3. Port app blocking endpoints
4. Port Obsidian integration

### Phase 3: Cutover
1. Move Token-API to port 7777
2. Update all clients (MacroDroid, AHK) to use Token-API
3. Update Claude hooks to only register with Token-API
4. Retire mesh-pipe
5. Remove ~/.claude registry files

### Phase 4: Enhancement
1. Add more TTS voices
2. Dynamic profile assignment
3. Better device auto-discovery

---

## Claude Code Integration

### Hook Scripts (or direct integration)

**On instance start:**
```bash
#!/bin/bash
# ~/.claude/hooks/instance-start.sh

# Detect SSH origin
if [ -n "$SSH_CLIENT" ]; then
    SOURCE_IP=$(echo $SSH_CLIENT | cut -d' ' -f1)
    ORIGIN_TYPE="ssh"
else
    SOURCE_IP=""
    ORIGIN_TYPE="local"
fi

# Register with local server
curl -s -X POST "http://localhost:7777/api/instances/register" \
    -H "Content-Type: application/json" \
    -d "{
        \"instance_id\": \"$CLAUDE_INSTANCE_ID\",
        \"origin_type\": \"$ORIGIN_TYPE\",
        \"source_ip\": \"$SOURCE_IP\",
        \"pid\": $$,
        \"working_dir\": \"$(pwd)\"
    }"
```

**On instance stop:**
```bash
#!/bin/bash
# ~/.claude/hooks/instance-stop.sh

curl -s -X DELETE "http://localhost:7777/api/instances/$CLAUDE_INSTANCE_ID"
```

---

## Phone Setup (Simplified)

With SSH_CLIENT detection, the phone's bashrc becomes simpler:

```bash
# No special SSH wrapper needed!
# Just SSH normally:
alias sshd="ssh token@100.66.10.74"

# Claude will detect SSH_CLIENT and register with phone origin automatically
```

The explicit `POST /session/start` is no longer needed.

---

## Finalized Decisions

1. **Port number**: Keep 7777 for compatibility with existing clients (MacroDroid, AHK)
2. **State persistence**: SQLite - aligns with existing `~/.claude/agents.db` infrastructure
3. **Heartbeat mechanism**:
   - **Primary**: Rely on SessionStart/SessionEnd hooks (fail loudly to validate hook system)
   - **Fallback**: 3-hour expiry for edge cases (crashed instances, lost connections)
4. **Claude hook mechanism**: Modify existing `~/.claude/hooks/session-start.sh` to POST to Token-API with SSH_CLIENT detection

---

## Observability Dashboard

### Requirements
- View all running Claude instances and their origin devices
- See which instances are actively working vs stopped
- Simple TUI initially, floating widget later

### TUI Dashboard (`token-api-tui`)

```
┌─────────────────────────────────────────────────────────────┐
│  TOKEN-API STATUS                          [q]uit [r]efresh │
├─────────────────────────────────────────────────────────────┤
│  INSTANCES (3 active)                                        │
│  ─────────────────────────────────────────────────────────── │
│  ● Claude #1  │ desktop     │ profile_1 │ working  │ 2h 15m │
│  ● Claude #2  │ pixel-phone │ profile_2 │ working  │ 45m    │
│  ○ Claude #3  │ desktop     │ profile_3 │ stopped  │ 10m    │
├─────────────────────────────────────────────────────────────┤
│  MODE: work_silence │ PRODUCTIVITY: active │ FOCUS: off     │
├─────────────────────────────────────────────────────────────┤
│  RECENT EVENTS                                               │
│  17:45:32  Instance registered: Claude #2 from pixel-phone  │
│  17:30:15  Mode changed: work_music → work_silence          │
│  17:15:00  Instance stopped: Claude #3                      │
└─────────────────────────────────────────────────────────────┘
```

### Implementation
- Use `textual` or `rich` Python library for TUI
- Poll SQLite database for instance state
- WebSocket endpoint for real-time updates (future)
- Separate process from main API server

### API Endpoint for Dashboard Data
```
GET /api/dashboard
{
  "instances": [...],
  "mode": "work_silence",
  "productivity_active": true,
  "focus_enabled": false,
  "recent_events": [...]
}
```

---

## SQLite Schema

Extends existing `~/.claude/agents.db` with new tables:

### claude_instances
```sql
CREATE TABLE claude_instances (
    id TEXT PRIMARY KEY,
    session_id TEXT UNIQUE NOT NULL,
    tab_name TEXT,
    origin_type TEXT NOT NULL,     -- 'local' or 'ssh'
    source_ip TEXT,
    device_id TEXT NOT NULL,       -- 'desktop', 'pixel-phone', etc.
    profile_name TEXT,
    tts_voice TEXT,
    notification_sound TEXT,
    pid INTEGER,
    status TEXT DEFAULT 'active',  -- 'active', 'stopped'
    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    stopped_at TIMESTAMP
);

CREATE INDEX idx_instances_status ON claude_instances(status);
CREATE INDEX idx_instances_device ON claude_instances(device_id);
```

### devices
```sql
CREATE TABLE devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,            -- 'local', 'mobile'
    tailscale_ip TEXT UNIQUE,
    notification_method TEXT,       -- 'tts_sound', 'webhook'
    webhook_url TEXT,
    tts_engine TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### events
```sql
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    instance_id TEXT,
    device_id TEXT,
    details TEXT,                   -- JSON
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_events_time ON events(created_at DESC);
```

---

## Next Steps

1. [ ] Create FastAPI project structure in `/mnt/imperium/Token-OS/token-api/`
2. [ ] Initialize SQLite schema (extend agents.db or create token-api.db)
3. [ ] Implement instance registration endpoints with SSH detection
4. [ ] Implement profile assignment from pool
5. [ ] Create TUI dashboard
6. [ ] Update Claude Code hooks to POST to Token-API
7. [ ] Port notification system from mesh-pipe
8. [ ] Port productivity/mode endpoints
9. [ ] Test with desktop + phone
10. [ ] Migrate remaining mesh-pipe features
