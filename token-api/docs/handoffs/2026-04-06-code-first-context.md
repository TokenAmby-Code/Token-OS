# Token-OS Code-First Context Handoff

## Scope

This note captures a code-first understanding of the current Token-OS stack from a WSL shell on `2026-04-06`.

Method:
- Treat repo code as authoritative.
- Treat vault slash-command docs as intent or operator doctrine unless they match code.
- Prefer observed execution paths and live API responses over naming or lore.

## Ground Truth

- This WSL machine runs `token-satellite`, not `token-api`.
- The primary `token-api` host is the Mac Mini (`100.95.109.23:7777` via Tailscale).
- The source tree is NAS-shared at `/mnt/imperium`, so the code here is the code `token-restart` should be using.
- `TOKEN_API_URL` in this shell points at the Mac, and `token-ping` works against that target when the env is present.

Important subtlety:
- `cli-tools/bin/token-ping` relies on inherited `TOKEN_API_URL`.
- It does not source `nas-path.sh` itself.
- If the caller environment is wrong, its localhost fallback can become misleading on WSL because localhost here is satellite territory, not the primary API.

## System Shape

The system has five practical layers under the 40k naming:

1. `tmux` is the process persistence and interaction surface.
2. `token-api` is the orchestration brainstem.
3. SQLite is the shared state store.
4. Obsidian is a second memory system, not just notes.
5. Discord is the human-facing event bus.

The machine topology is cleaner than the service decomposition. `cli-tools/lib/imperium_config.py` and `cli-tools/lib/nas-path.sh` encode a relatively clear distributed model. The service boundaries inside `token-api` do not.

## Token-API Opinion

`token-api/main.py` is a large monolith with roughly 14k lines and about 181 FastAPI routes.

It currently mixes:
- DB init and migrations
- instance registration and lifecycle
- hook ingestion
- scheduler and cron integration
- timer state and shift tracking
- TTS queueing and sound
- phone and desktop enforcement
- Discord ingress
- session-doc creation and linking
- aspirant and summarization flows
- evaluator and policy logic

This is a classic "and one" file. It is still understandable, but only if traced by execution path rather than by conceptual subsystem.

## Cleaner Seams

These are the least vibeslop parts of the stack:

- `token-api/timer.py`
  - relatively isolated domain logic
  - deterministic enough to reason about directly
- `token-api/token-satellite.py`
  - good boundary for WSL and Windows-adjacent behavior
  - owns TTS bridge, AHK execution, some enforcement, and remote tmux actions
- `cli-tools/bin/transplant`
  - more sophisticated and safety-conscious than expected
  - real process migration logic, not just a wrapper
- `cli-tools/bin/claude-cmd`
  - input-locking plus local/remote pane dispatch is a legitimate systems solution

## Critical Execution Paths

### 1. Restart path

`cli-tools/bin/token-restart` is Mac-authoritative even when invoked elsewhere.

Observed behavior:
- non-Mac devices proxy restart through SSH to the Mac
- Mac restarts local `token-api`
- Mac attempts WSL satellite restart through `/restart`, then SSH fallback
- it also respawns relevant TUI panes

So "run from anywhere" is the intent, but the implementation still centers the Mac.

### 2. Hook path

`claude-config/hooks/generic-hook.sh` is the operational backbone.

It does real work:
- resolves Claude ancestor PID
- captures tmux context
- injects extra env into hook payloads
- consumes transplant handoff state on `SessionStart`
- drains pending UI commands on `UserPromptSubmit`
- embeds transcript tail on remote `Stop`
- chooses sync vs async delivery depending on action type

If this hook path is unhealthy, the whole system loses lifecycle awareness.

### 3. Session start

`handle_session_start` in `token-api/main.py` is one of the most overloaded functions in the codebase.

It currently handles:
- local vs ssh vs cron origin detection
- subagent detection
- tmux pane capture
- device resolution from client IP
- transplant and supplant logic
- re-registration refresh
- voice and profile preservation
- session-doc creation and linking
- legion inference
- singleton enforcement behaviors
- UI color and tab refresh response

This is a prime extraction target later, but only after the lifecycle is fully documented.

### 4. Stop path

The stop path is not simple completion logging.

`handle_session_end` and `handle_stop` combine:
- instance shutdown state changes
- transcript tail extraction
- auto-naming
- stop evaluators
- notification routing
- Discord mirroring
- TTS and sound
- possible resync or retrigger behaviors

That means stop behavior is both lifecycle handling and policy engine.

## Live Runtime Findings

All of the following came from the live Mac API, not local lore:

- `GET $TOKEN_API_URL/health` succeeded.
- `tts_backend.satellite_available` was `false`, which matches this WSL host not currently serving satellite health.
- `tts_backend.current` was `null`.
- `tts_global_mode` was `verbose`.
- `GET /api/instances` returned about `1933` instance rows.
- `GET /api/instances?sort=recent_activity` showed `active = 0` at inspection time.
- `GET /api/session-docs?status=active` returned about `410` active session docs.
- Active session docs appear heavily duplicated by title.
- `"token-api 2026-04-05"` appeared about `173` times.
- Most active docs were linked, but a few were unlinked.
- `GET /api/state` showed:
  - `work_mode=clocked_in`
  - `timer_mode=idle`
  - `active_instances=0`
  - `break_time_remaining_min` around `91`
  - `work_time_earned_min` around `230`
- `GET /api/cron/status` showed a populated cron system with many jobs and most disabled.
- `GET /api/logs/recent` showed live 2026-04-06 activity, including repeated warnings for an unknown hook action: `StopValidate`.

## Concrete Drift / Breakage

### Broken hook contract: `StopValidate`

This is the cleanest concrete bug found during exploration.

Observed:
- `claude-config/hooks/stop-validator.sh` posts to `/api/hooks/StopValidate`
- `token-api/main.py` only recognizes action types such as:
  - `SessionStart`
  - `SessionEnd`
  - `UserPromptSubmit`
  - `PostToolUse`
  - `Stop`
  - `PreToolUse`
  - `Notification`
- live recent logs on the Mac repeatedly show `Hook: Unknown action type: StopValidate`

Interpretation:
- the validator hook wiring is partially migrated or stale
- this is not just documentation drift; it is a live mismatch between hook caller and API handler

### Docs lagging the monolith

Example:
- the vault `/token-api` operator doc still describes the service as roughly 5k lines
- the current repo `main.py` is far beyond that

This matters because mental models built from the docs will understate current coupling.

### Session-doc proliferation

The active session-doc set looks too eager and too duplicative.

This probably means at least one of:
- auto-create rules are too permissive
- link/unlink closure is incomplete
- top-level vs subagent distinctions are leaking into session-doc creation

### Duplicate schema ownership

There are at least two schema-definition paths:

- `token-api/main.py:init_db()`
- `token-api/init_db.py:init_database()`

They are not fully aligned.

Concrete example:
- the live `claude_instances` table on the Mac contains `is_subagent` and `spawner`
- `token-api/init_db.py` explicitly migrates those columns
- `token-api/main.py:init_db()` does not add them in the migration block I inspected
- `handle_session_start()` in `main.py` unconditionally inserts both columns

Implication:
- schema truth is currently split across two files
- bootstrap safety depends on which initialization path actually ran
- any decomposition that ignores schema ownership first is high risk

Detailed unification plan:
- `token-api/docs/plans/2026-04-06-db-schema-unification-design.md`

Implementation status:
- canonical schema owner now exists at `token-api/db_schema.py`
- `init_db.py` and `main.py:init_db()` both delegate to it
- fresh sync/async DBs matched on full `.schema` in local verification

## Working Mental Model

The best current way to reason about the system is:

- `token-api` is the stateful policy and routing core.
- `token-satellite` is a machine-side capability adapter.
- `tmux` is the durable embodiment of agents.
- the vault is a long-lived semantic memory layer.
- Discord is the ambient operator channel.

That model is much more useful than any lore taxonomy when tracing bugs.

## Recommended Next Investigation Order

1. Trace one instance end-to-end.
   - registration
   - activity updates
   - tmux identity
   - stop path
   - session-doc creation
   - Discord/TTS side effects

2. Reconcile the hook contract.
   - decide whether `StopValidate` should exist
   - or remove the extra hook and fold behavior into `Stop`

3. Audit session-doc creation rules.
   - explain why there are hundreds of active docs
   - explain the duplicate-title pattern

4. Only then design decomposition of `main.py`.
   - start with lifecycle, hooks, notifications, and session-docs
   - leave timer logic mostly intact unless evidence says otherwise

## Safest Decomposition Path

If decomposition starts, the first safe move is not "split the giant file by route count".

It is:

1. unify schema and migrations into one owner
2. extract repositories around core tables:
   - instances
   - session documents
   - events
3. split hook handlers into orchestrators that call those repositories and services
4. split the stop path by phase:
   - state transition
   - evaluator scheduling
   - lifecycle retrigger scheduling
   - transcript extraction
   - notification fanout

The stop path is too load-bearing to refactor by aesthetics.

## Session-Doc Direction

Current behavior is a compromise:
- every top-level instance gets a session doc
- the fallback is effectively per-session auto-create by worktree/date

Desired invariant:
- every top-level instance should have the most relevant existing session doc, or a new one

Recommendation:
- do not add more real-time session-doc decision logic to the stop hook
- keep stop focused on lifecycle completion, evaluators, and notifications
- move session-doc selection to session start

Best insertion point:
- replace or wrap the current auto-create block in `handle_session_start()`
- introduce a `resolve_or_create_session_doc(...)` path

Suggested resolution order:
1. preserve transplanted or supplanted doc linkage
2. preserve explicit primarch-linked docs
3. preserve explicit user-assigned docs
4. match recent active docs in same project/worktree
5. match recent docs with same title stem or topic heuristic
6. create a new doc only as fallback

First-pass implementation should stay deterministic:
- `working_dir`
- existing linked docs for recent instances in that dir
- active docs with same project/title stem
- stable instance/tab naming once available

If semantic similarity is desired later:
- run it as a background consolidation or merge suggester
- do not make it part of the stop-hook critical path

Longer-term missing concept:
- a stable idea/workstream key, e.g. `topic_key`
- current code has instance identity and doc identity, but not a first-class "these instances belong to the same idea" field

Detailed contract:
- `token-api/docs/plans/2026-04-06-session-doc-resolution-design.md`

## Agent Dispatch Recommendation

For this design stage, do not spawn implementation workers yet.

Reason:
- the next valuable work is boundary-setting, not parallel coding
- schema ownership and session-doc semantics still need one coherent decision thread
- parallel workers would likely encode different assumptions into a fragile area

Good point to dispatch workers later:
- after a concrete resolver contract exists
- after the schema unification contract exists
- after schema ownership is unified or at least explicitly assigned
- after the write scope is split cleanly, e.g. one worker for schema/repository extraction and one for session-start resolver wiring

## Context Recommendation

For future deep exploration sessions in this repo, cut investigation at about `20%` to `25%` remaining context.

Reason:
- below that threshold there is not enough room for both synthesis and a useful operator-facing write-up
- this codebase needs written handoffs more than it needs one extra file read at the margin
