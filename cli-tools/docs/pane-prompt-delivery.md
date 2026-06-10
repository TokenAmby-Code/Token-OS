# Pane Prompt Delivery

`agent-cmd` is the canonical primitive for injecting prompts into an already-running Claude/Codex-style TUI pane.

This exists because raw `tmux send-keys ... Enter` can leave text queued in live agent prompt bars. The hardened path is now centralized in `tmuxctl` and exposed through `agent-cmd`.

## Commands

```bash
agent-cmd --pane %123 "run the tests"
agent-cmd --instance <instance-id> "////resume"
tmux select-pane -P bg=#ff9900
```

`agent-cmd` is retained as a compatibility wrapper and delegates directly to `agent-cmd`.

Supported target/options mirror the old `agent-cmd` interface:

- `--self`
- `--pane <pane-id>`
- `--instance <instance-id>`
- `--detach`
- `--resolve-only`
- `--no-escape` currently accepted for caller compatibility

## Delivery invariant

All live agent prompt injections must use this path unless they are launching a new shell command rather than submitting text to an existing agent prompt.

The canonical sequence is:

1. normalize prompt payload:
   - collapse CR/LF runs to single spaces
   - strip trailing whitespace / accidental submit-newlines
   - reject empty payloads
2. `tmux send-keys -l <payload>`
3. wait 1 second
4. `C-m`
5. wait 1 second
6. `C-m`

The second delayed submit is intentional. In live Codex/Claude repros, immediate submit could be swallowed as a prompt newline or leave the prompt queued; the delayed standalone submit drains that queued prompt. On normal submissions, the second `C-m` lands on an empty prompt and is harmless in the target TUI.

## Current routed entry points

- `agent-cmd` direct use
- `agent-cmd` compatibility wrapper
- `tmuxctl send-text`
- Python callers using `TmuxAdapter.send_text_then_submit`
- Token-API pane-write queue / local pane-write path
- `tmux-dictate --submit` and voice/Discord dictation paths that call it
- `tmux-context` context-threshold nudge

## Observability

`agent-cmd` prints JSON delivery metadata on foreground sends:

```json
{
  "dispatch_id": "...",
  "payload_hash": "sha256(normalized-payload)",
  "pane": "%123",
  "instance_id": "...",
  "verification_status": "sent",
  "verified_by": "tmuxctl"
}
```

Token-API logs `hook_user_prompt_submit` when it receives a Claude/Codex `UserPromptSubmit` hook. This confirms the target application observed a submitted user prompt when the instance is registered with Token-API.

## Live validation: 2026-05-14

Validated after restarting `ai.openclaw.tokenapi`:

- fake tmux read-loop pane received normalized `hello world` from an input containing newlines/trailing whitespace;
- throwaway live Codex TUI pane received and answered `Reply with exactly AGENT_CMD_LIVE_OK and nothing else.`;
- Token-API recent events showed `hook_user_prompt_submit` for the live submission.
