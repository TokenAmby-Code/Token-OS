# Managed Stack Dispatch

`tmuxctl` owns pane-backed dispatch for managed stack pages.

## Invariant

- `legion` has exactly one orchestrator pane: `@PANE_ID=legion:custodes`, `@PANE_TYPE=legion`.
- `mechanicus` has exactly one orchestrator pane: `@PANE_ID=mechanicus:fabricator-general`, `@PANE_TYPE=mechanicus`.
- Every additional pane in either page is a right-column worker: `@PANE_ID=<base>:worker`, `@PANE_TYPE=stack-worker`.
- Dispatch entry points must not call raw `tmux split-window` into `legion`/`mechanicus`.

## Commands

Create a managed worker pane only:

```bash
tmuxctl stack add legion --session main --cwd "$PWD"
```

Create a managed worker pane and launch a command:

```bash
tmuxctl stack dispatch legion --session main --cwd "$PWD" --command 'echo hello world'
```

Reassert invariants around an existing pane/window:

```bash
tmuxctl stack enforce --pane %123
tmuxctl stack enforce --focus --pane %123
tmuxctl stack enforce --window main:legion
```

Sweep every managed stack page in a session and prune dead/clear worker artifacts:

```bash
tmuxctl stack sweep --session main
```

Do **not** run `stack sweep` on a timer. Periodic sweeps caused observable focus snaps because tmux layout repair can briefly reselect panes even when wrapped in focus restore. Stack repair is event-driven: pane birth/death hooks, explicit human pane/window selection, dispatch/admit paths, and manual repair when debugging.

Dispatch wrappers call `tmuxctl stack dispatch ... --no-focus` for `:new` stack launches. Automated FG/worker dispatches must allocate `mechanicus:new` and must not select/focus the new pane unless a human explicitly asks to inspect it. Numbered stack panes (`mechanicus:1`, `mechanicus:2`, etc.) are identities for existing workers, not launch targets for new work.

Focus safety: `stack enforce`, explicit/manual `stack sweep`, and non-focusing dispatch are automation surfaces. They must preserve the operator's current window/pane even when tmux layout commands internally reselect panes. Human navigation into stack windows is handled separately by the focus guard / human client marker; see [`tmux-focus-guard.md`](tmux-focus-guard.md).

Zoom safety: native tmux zoom (`Prefix+e` / `tmux-grid-expand`, implemented as `resize-pane -Z`) is a human visual state. If a managed stack window is zoomed, `stack enforce` and `stack sweep` must defer structural layout normalization and return a no-op instead of running `select-layout`, `resize-pane`, `join-pane`, or secondary-persona `split-window` operations that would de-expand the pane. This complements focus safety: focus-preserving automation prevents camera snaps, while zoom-preserving automation prevents visual de-expansion. Normalization resumes after the human unzooms the window.

## Pane-bound assertion (`tmuxctl assert-instance`)

`tmuxctl assert-instance --pane <target>` is the public assertion primitive for pane-backed agents. It resolves the pane, reads its `@PANE_ID` / `@PANE_TYPE`, checks the live runtime plus Token-API registry row, and returns JSON:

```json
{"ok": true, "pane": "%26", "pane_label": "legion:custodes", "pane_type": "legion", "instance_id": "...", "action": "none", "reason": "live"}
```

Exit code is boolean: `0` only when `ok=true`.

Behavior is type-bound:

- Persona panes (`legion:custodes`, `legion:malcador`, `mechanicus:fabricator-general`, `mechanicus:admin`, `koronus:pax`, `koronus:orchestrator`):
  - blank/no live runtime: launch the configured persona in the same pane;
  - live runtime with a stopped coherent registry row: reactivate the row and return `ok=true`, `action=registry_reactivated`;
  - live runtime but no registry row at all: do not inject `/persona`; log the anomaly and return `ok=false`, `action=persona_unregistered_noted` (or `persona_unregistered_suppressed` during backoff);
  - live runtime with a registry row for the wrong identity: do not inject `/persona`; log the harness/SessionStart mismatch and return `ok=false`, `action=persona_mismatch_noted` (or `persona_mismatch_suppressed` during backoff);
  - stopped-row reactivation failure: return `ok=false`, `action=registry_reactivation_failed`;
  - live and coherent: return `ok=true`.
- Stack workers (`@PANE_TYPE=stack-worker`):
  - no live runtime: mark stale Token-API rows stopped, clear stale pane identity, and prune the pane;
  - workers are never restarted automatically.
- Other structured panes (palace/somnium/etc.):
  - assertion is truth-only; no launch/restart;
  - stale Token-API rows may be marked stopped as coherence cleanup.

`send-text` runs this assertion opportunistically before injecting payloads. If assertion returns any `ok=false` persona action, `send-text` refuses the real payload; callers must settle and retry assertion before delivery.

Operational note: after plan-mode or any mode transition that can drop persona registration, run `tmuxctl assert-instance --pane <persona-pane>`. `persona_unregistered_noted` means the pane is live but has no registry row to reactivate; restart that persona pane so SessionStart can create the row. `persona_unregistered_suppressed` means the same condition is still in its diagnostic backoff window.

## Current entry points

- `dispatch --target legion:new|mechanicus:new` allocates stack panes via `tmuxctl stack add`.
- Prefix+Space (`tmux-legion-prompt`) launches via `tmuxctl stack dispatch legion`.
- Claude print-mode redirection (`agent-wrapper.sh claude`) launches via `tmuxctl stack dispatch`.
- Golden Throne resume fallback allocates managed legion workers via `tmuxctl stack add legion`; legacy side-window naming has been retired.
- `work-loop dispatch` allocates managed legion workers via `tmuxctl stack add legion` and marks them with `@WORK_LOOP=true`.
- Pane demotion (`tmux-shuttle`) moves panes into legion as `legion:worker` / `stack-worker`, then calls `tmuxctl stack enforce --focus`.
- Aspirant full-session launch (`dispatch --aspirant --aspirant-kind dispatch`) creates the aspirant note/session doc, then re-enters `dispatch --target legion:new` with an aspirant system prompt, generated launch prompt, linked session doc, and Golden Throne metadata. Use `--intake-only` for the old note/session-only behavior.

If a new tool needs a legion/mechanicus pane, wire it to `tmuxctl stack add` or `tmuxctl stack dispatch`. Do not duplicate layout, split, tagging, or focus behavior in shell.

## Wrapper lifecycle cleanup

Agent launches route through `cli-tools/scripts/agent-wrapper.sh`. The wrapper owns
the `WrapperStart`/`WrapperEnd` boundary around the native agent process; Token-API
continues to own process/session lifecycle and native agent `SessionStart` /
`SessionStop`.

On wrapper exit, the contract is:

1. Preserve the native agent's real exit code.
2. Forward `INT`, `TERM`, and `HUP` to the child agent while the wrapper remains
   alive.
3. Run one guarded cleanup path exactly once, even under repeated Ctrl+C spam.
4. Emit Token-API `WrapperEnd` asynchronously first as telemetry / lifecycle
   confirmation only.
5. Emit tmuxctld `POST /hooks/wrapperend` last. This is the authoritative
   "drop the bomb on my head" ping: as soon as tmuxctld receives it, tmuxctld
   immediately clears wrapper-owned pane visual/runtime state.

tmuxctld owns wrapper visual cleanup:

- clear persona/tint/statusline/runtime pane options for the pane whose
  `@TOKEN_API_WRAPPER_LAUNCH_ID` matches the payload;
- resolve the pane by wrapper id if the payload has no live pane target;
- treat duplicate, already-cleared, or missing panes as idempotent success;
- reject a live pane owned by a different wrapper as an ownership error.

Token-API `WrapperEnd` must never be on the synchronous shell-return path. Slow
Token-API orchestration, mechanicus sync, or session cleanup may continue in the
background, but visible tmux pane cleanup must not wait for it.

See also: [`aspirant-dispatch.md`](aspirant-dispatch.md) for the full aspirant launch contract and real validation walk.
