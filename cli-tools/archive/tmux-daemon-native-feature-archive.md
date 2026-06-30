# Tmux daemon-native feature archive from cold CLI prototypes

Status: archival/reference only. Corrected doctrine after Custodes update: these cold CLI tools are not hard-deleted individually; they become loud 410-on-touch tombstones with original bodies retained dead below until full daemon-native replacement coverage exists and a later single atomic wipe is authorized.

Crater/restore manifest: `/Volumes/Imperium/Imperium-ENV/Mars/Reports/Dispatch-Specs/2026-06-30-cli-archive-crater-manifest.md`.

## Core daemon-native rebuild principles

- `tmuxctld` owns tmux truth as a long-running loopback daemon.
- Token-API owns registry/session/document state; tmuxctld owns pane send, liveness, lifecycle proxying, labels, focus-safe mechanics, and tmux event ingestion.
- Runtime callers should use stable Token-OS identity: `instance_id`, tmuxctl public pane ids such as `somnium:SE`, and singleton persona slugs such as `custodes` / `fabricator-general`.
- Raw tmux `%pane` ids are syscall return values only. If tmux returns `%NN`, decode immediately at the metal boundary into a stable id; do not persist, compare, route, or report `%NN` above that layer.
- Cold CLI code is prototype evidence, not target architecture. Preserve concepts as daemon feature requests; do not preserve PATH shims or compatibility wrappers.

## Priority feature requests extracted

| Priority | Feature family | Prototype sources | Daemon-native target |
|---|---|---|---|
| P0 | Runtime oracle identity resolution | `tmux`, `tmux-resolve-pane`, `pane-id.sh`, `tmuxctl resolver/public_ids/snapshot` | Single resolver accepting instance id, public pane id, singleton slug; `%` only inside raw tmux syscall adapter. |
| P0 | Gated pane writes and prompt delivery | `tmux-dictate`, `tmux-resume`, `tmux-multiprompt`, `tmux-guard*`, `send_gate.py`, `tmux_adapter.py`, `skill_invoke.py` | `/send`, `/draft`, `/prompt/insert`, `/prompt/submit`, `/prompt/invocation`, fanout endpoint with gate/typing/quiet-hour policy in daemon. |
| P0 | Lifecycle/teardown/respawn | `tmux-instance-exit`, `tmux-mark-for-close`, `tmux-pane-respawn`, `tmux-runtime-cleanup.sh`, `teardown.py`, `close.py` | `/lifecycle/close`, `/event/pane-died`, `/pane/clear-runtime`, `/pane/reseat`, class-gated PERPETUAL/SLOT/WORKER behavior. |
| P0 | Dispatch stack allocation and occupancy | `tmux-legion-prompt*`, `tmux-shuttle`, `stack.py`, `occupancy.py`, `liveness.py` | Daemon transaction for allocation/launch based on process-tree occupancy, boot grace, singleton identity, and stack class. |
| P1 | Plan/preplan/compact UX | `tmux-plan-menu`, `tmux-plan-approve-clear`, `tmux-mode-toggle`, `skill_invoke.py` | `/prompt/action` and `/approval/clear-context`; daemon owns cursor motion, engine-aware leader policy, and strict modal classifier. |
| P1 | Focus/zoom/navigation/audience/tombstone | `tmux-focus`, `tmux-grid-expand`, `tmux-shuttle`, `audience.py`, `focus.py`, `focus_guard.py`, `pane_select.py`, `tombstone.py` | `/focus`, `/zoom`, `/pane-select`, `/audience/toggle`, `/tombstone/jump`, preserving client focus and speaking stable ids. |
| P1 | Voice/TTS draft routing | `tmux-dictate`, `tmux-goto-spoken`, tmuxctld voice session routes | Semantic voice sessions; TTS jump resolves recent speaker identity via Token-API then tmuxctld oracle. |
| P1 | Inspection/status/audit | `tmux-audit`, `tmux-context`, `tmux-pane-status`, `tmux-status`, `tmux-typing-guard-status`, `inspect.py`, `invariants.py` | `/inspect/*`, `/status/*`, `/audit` read models without per-render forks or obsolete instance fields. |
| P2 | Emergency recovery | `tmux-reset`, `tmux-resurrect`, `metal_resolver.py`, `metal_restart.py`, `executor.py`, `planner.py` | Human-emergency-only daemon maintenance/recovery routes; not hot runtime unless explicitly promoted. |

## Concept snippets

### Resolve once, speak stable ids

```python
@dataclass(frozen=True)
class RuntimeTarget:
    instance_id: str | None
    public_pane_id: str
    singleton_slug: str | None
    raw_syscall_pane: str  # private to tmux syscall adapter


def resolve_target(raw: str) -> RuntimeTarget:
    # Accept instance UUID, singleton slug, public pane id, or last-chance %pane.
    # If raw starts with %, deref immediately via live tmux and return a stable id.
    ...
```

### One gated delivery API

```python
def deliver_text(target, text, *, submit: bool, clear_prompt: bool = False):
    pane = runtime_oracle.resolve(target)
    if gate.human_typing(pane.public_pane_id) or gate.quiet_hours(target):
        return {"ok": False, "reason": "gated", "target": pane.public_pane_id}
    if clear_prompt:
        prompt.clear(pane)
    prompt.insert_literal(pane, text)
    if submit:
        prompt.submit(pane)
    return {"ok": True, "target": pane.public_pane_id}
```

### Pane class drives teardown

```python
class PaneClass(Enum):
    PERPETUAL = "perpetual"  # singleton persona seat: revive/reseat
    SLOT = "slot"            # fixed palace/somnium slot: clear in place
    WORKER = "worker"        # dynamic worker: cull


def apply_teardown(pane):
    match classify_pane(pane):
        case PaneClass.PERPETUAL:
            return reseat_persona(pane)
        case PaneClass.SLOT:
            return clear_runtime_and_respawn_shell(pane)
        case PaneClass.WORKER:
            return kill_worker_husk(pane)
```

### Engine-aware invocation belongs in daemon

```python
def invocation_leader(agent, kind):
    if kind == "command":
        return "/"
    return "$" if agent == "codex" else "/"


def insert_invocation(target, name, *, kind, arguments=""):
    agent = resolve_agent_for_target(target)
    text = f"{invocation_leader(agent, kind)}{name} {arguments}".rstrip()
    prompt.move_to_start(target)
    prompt.insert_literal(target, text)
    prompt.move_to_end(target)
```

## Deleted-file crater list preserved elsewhere

The exact over-delete list and caller-risk matrix are in the Mars crater manifest named above. It lists all affected `cli-tools/bin/tmux*`, `cli-tools/lib/pane-id.sh`, `cli-tools/lib/tmux-*.sh`, and `cli-tools/lib/tmux_client_lease.py` paths, plus suspected silent-fail callers.
