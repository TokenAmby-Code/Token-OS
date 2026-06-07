# Task: Reconcile delivery-proof send-gate semantics into current main

You are a legion (astartes) worker. This is the **pilot of the enforced worktree workflow** — stay entirely in your isolated worktree, never touch `main` directly.

## Background
Commit `965bb610` (on stale branch `send-gate-delivery-proof`, ~6391 lines behind main) fixed a real bug: the brief→FG delivery failure of 2026-05-30, where a gated pane write returned silently and the caller hardcoded `verification_status="sent"`, so a brief that never reached the pane reported success three times.

**Do NOT merge or rebase `send-gate-delivery-proof`** — it is too stale and will revert main. Instead, read its single commit and **re-apply its semantics onto the CURRENT files on main**:

```
git show 965bb610
```

## What to reconcile (into current `cli-tools/lib/tmuxctl/send_gate.py` + `tmux_adapter.py`)
main has ALREADY evolved these files via overnight merges (#52/#53). For EACH fix below, first check whether it is already present, partially present, or missing on current main — only add what's missing; do not duplicate or regress existing behavior.

1. **Never default to "sent".** A gate-suppressed write returns `verification_status="gated"` (zero bytes issued → safe to re-queue). A byte-issued-but-unconfirmed write is `"unverified"`, never a default `"sent"`. Introduce/restore `TmuxSendGated(TmuxError)` carrying the gate result so `send_text_then_submit` checks `last_send_gate_result` right after the byte-bearing literal send and **aborts atomically** (no bare `C-m` at an empty prompt) when suppressed.
2. **Queue, don't bounce.** `process_pane_write_queue_once` keeps a gated item `PANE_WRITE_PENDING` (reason `send_gated:<reason>`) so the periodic worker flushes it once the gate clears.

## Verify
- Run `cli-tools/tests/test_tmuxctl_send_gate.py` and the token-api send-gate / delivery tests. Add a test proving a gated write never reports `"sent"` if one doesn't already exist.

## Deliverable
Open a PR onto clean `main`:
- Title: `fix(send-gate): prove pane-write delivery, never default to "sent" (reconcile 965bb61)`
- Body: note this reconciles 965bb61 onto evolved main, lists which of the 2 fixes were already present vs added, and confirms tests pass.

Report the PR number when done.
