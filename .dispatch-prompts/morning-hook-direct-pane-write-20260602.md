# Task: Fix the morning-hook delivery lie — `_direct_pane_write` hardcodes "sent"

You are a legion (astartes) worker. Stay in your isolated worktree; never touch `main` directly. Deliver a PR.

## The bug (root cause of the morning state-hook NOT firing into the pane)
`token-api/routes/hooks.py` → `_direct_pane_write(tmux_pane, payload)` (~lines 1177-1199) discards the return value of `send_text_then_submit` and **hardcodes `{"status": "sent"}`**, and its `except Exception` fallback does a bare `tmux send-keys ... Enter` and reports `"sent"` on returncode 0 — both UNVERIFIED. So when the gate suppresses or the write silently fails, the caller still reports success → the morning prompt never lands in the pane but the system believes it did. THIS is the live regression behind "state hook fired but nothing arrived."

```python
async def _direct_pane_write(tmux_pane: str, payload: str) -> dict:
    try:
        from tmuxctl.tmux_adapter import TmuxAdapter, TmuxError
        adapter = TmuxAdapter()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(adapter.send_text_then_submit, tmux_pane, payload),
                timeout=10,
            )
            return {"status": "sent", "operation": "tmuxctl.send_text_then_submit"}  # <-- IGNORES return value
        except TmuxError as exc:
            return {"status": "failed", "error": str(exc)}
    except Exception:
        proc = await _run_subprocess_offloop(("tmux", "send-keys", "-t", tmux_pane, payload, "Enter"), ...)
        if proc.returncode == 0:
            return {"status": "sent", "operation": "tmux.send-keys"}  # <-- UNVERIFIED
        return {"status": "failed", ...}
```

## Fix
The send-gate LAYER (`send_text_then_submit` in `cli-tools/lib/tmuxctl/`) was already hardened by PR #40 (commit dfec67c) to return a verification result / raise `TmuxSendGated`. This CALLER was missed — it throws the result away.
1. **Capture** `send_text_then_submit`'s return value and **propagate** its `verification_status`. Return `"sent"` ONLY when the gate verifies the bytes landed (pane-scrape confirmed). If gated → `"gated"` (zero bytes — safe to re-queue). If byte-issued-but-unconfirmed → `"unverified"`. NEVER default to `"sent"`.
2. **Re-queue on gated/unverified**: a morning-hook payload that didn't verify must be re-queued (reuse the pane-write pending queue / `process_pane_write_queue_once` path) so it flushes when the gate clears — do not drop it and do not lie.
3. The `except Exception` raw `send-keys` fallback must NOT report `"sent"` on returncode 0 alone — that only proves tmux accepted bytes, not that the agent received a clean submit. Mark it `"unverified"` and re-queue, OR remove the unverified fallback entirely if the gated path already covers it (prefer routing everything through the verified gate).

## Verify
- Add a regression test: simulate a gated/suppressed write and assert `_direct_pane_write` returns a non-"sent" status AND the payload is re-queued (never silently dropped, never reported sent).
- Run token-api hooks tests + send-gate tests.
- Confirm a verified write still returns `"sent"`.

## Deliverable
PR onto clean `main`. Title: `fix(hooks): _direct_pane_write must not hardcode "sent" — propagate send-gate verification (morning-hook delivery)`. Body: show before/after, note this completes the PR #40 delivery-proof work at the caller, list the new re-queue path, confirm tests. Report the PR number.

## Context for the dispatcher (Custodes)
Invocation (mirror-coated dispatch, #60): `dispatch legion --worktree morning-hook-delivery-proof --repo token-os --prompt-file .dispatch-prompts/morning-hook-direct-pane-write-20260602.md`. Deadline: BEFORE tomorrow's live morning fire. NOTE: `hooks.py` currently has uncommitted WIP in the working tree on main — the worker branches from committed main (clean), so coordinate the eventual merge with that WIP.
