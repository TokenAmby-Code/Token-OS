# Prod CI Test Triage Recommendation

Date: 2026-06-29

Scope read: `.github/workflows/README.md`, `prod-gate.yml`, `pr.yml`,
`push.yml`, `bounty-board.yml`, current `token-api/tests/**`,
`cli-tools/tests/**`, and recent test/workflow history through `dc10149`.

## Authority boundary

This is a recommendation artifact only. **Gate membership is an Emperor
decision.** No workflow membership, branch protection, or test marker behavior
is changed here.

The framing below applies the Emperor's authoritative direction. It is not a new
philosophy derivation.

## Emperor framing: exists vs intended

The dual-gate belongs inside the **5.3 spark wave**:

- **BLOCKING PROD GATE = tests asserting behavior that exists today.** If the
  tested behavior is real current behavior, it belongs in the hard prod gate.
  Cost, flake risk, external-system labels, or project ownership are secondary
  hardening concerns; they are not the primary lane-selection axis.
- **BOUNTY BOARD = tests derived from vault intent.** These assert features the
  vault says should exist but that may not be built yet. Failure is legitimate
  until implementation lands, so this lane is advisory and auto-`xfail`.
- **Per-test triage question:** does this test assert behavior that **exists
  today**, or behavior the vault **intends** but the repo may not implement yet?
  Existing behavior -> blocking. Vault-intended/unbuilt behavior -> bounty.
- **Ambiguity is rare.** Flag only cases where the test is genuinely unclear
  about whether it asserts current implemented behavior or future vault intent.
  The Emperor makes the final ruling.

W-A coherence-map vault-intent tests are natural bounty-lane feeders. In
particular, coherence-map's vault-derived feature gaps, including the 11
identified vault features without tests, should enter the Bounty Board when they
assert intended behavior that may not exist yet. W-A and W-B should stay
consistent: pin what exists, bounty what the vault intends.

## Current repo policy found

- `pr.yml`: blocking `quality` check for PRs to `main`; runs ruff format, ruff
  lint, mypy; **no pytest**.
- `push.yml`: advisory non-`main` push signal; ruff/mypy and chill CodeRabbit are
  non-blocking; **no pytest**.
- `prod-gate.yml`: `tests` job for PR/push to `prod`, nightly, and manual; runs
  both pytest suites with `-m "not bounty"`.
- `bounty-board.yml`: advisory lane; runs only `-m bounty`; never required.
- Bounty tests live under `*/tests/bounty_board/`, are auto-marked `bounty`, and
  auto-`xfail(strict=False)` until their feature ships.

## Recommendation summary

Recommended posture:

1. Keep **prod-gate as the required lane for all non-bounty pytest** unless the
   Emperor explicitly curates a narrower set.
2. Keep **bounty-board advisory and excluded from prod-gate** for vault-intent
   tests whose target behavior may not exist yet.
3. Keep **dev path quality-only**: format/lint/typecheck/CodeRabbit, not pytest.
4. Do **not** demote tests because they are costly, subprocess-heavy, mocked, or
   potentially flaky if they assert existing real behavior. First classify by
   exists-vs-intended; then harden, serialize, isolate, or fix the test mechanics.
5. If a currently non-bounty test actually asserts an unbuilt vault-intended
   behavior, move/rewrite it as bounty. If a bounty test starts XPASSing because
   the feature shipped, graduate it out of `bounty_board/` so it can protect the
   now-existing behavior.

## Classification by test family

### Blocking prod-gate candidates: existing behavior

These families appear to assert implemented behavior that exists today. Under
the Emperor framing, they should hard-block prod merge unless the Emperor rules a
specific test is actually vault-intent/unbuilt.

| Family | Representative tests | Exists-vs-intended read | Recommendation | Notes |
|---|---|---|---|---|
| Token API DB/schema/session identity | `test_instances_registry.py`, `test_claude_instances_exterminatus.py`, `test_db_timer_schema_split.py`, `test_session_start_*`, `test_instance_*`, `test_persona_*`, `test_session_doc_pool.py` | Existing behavior: current DB/session/persona contracts and identity persistence. | Blocking prod-gate. | Temp DBs and mocked edges are appropriate for deterministic hard gates. |
| Golden Throne / victory / supervision state | `test_gt_*`, `test_victory_*`, `test_work_state_*`, `test_work_action_*`, `test_questions_gate.py`, `test_planning_state_autoclear.py` | Existing behavior: current control-plane state machines and wake/stop semantics. | Blocking prod-gate. | Time/state complexity is a hardening issue, not a bounty reason. |
| Tmux/tmuxctld prompt-delivery and pane safety | `cli-tools/tests/test_tmuxctl_send_*`, `test_tmuxctld_*`, `test_tmux_typing_guard_*`, `test_tmuxctl_focus*`, `test_tmuxctl_occupancy.py`, `test_tmuxctl_freelist.py`, `token-api/tests/test_pane_write_queue_gate.py`, `test_send_path_tmuxctld_regression.py` | Existing behavior: prompt routing, daemon parity, pane occupancy, focus, and typing guards currently implemented in tmuxctl/tmuxctld. | Blocking prod-gate. | Fake/model tmux tests still assert current behavior. Separate live smoke can exist, but model coverage should not be demoted solely for being mocked. |
| Dispatch/worktree/wrapper lifecycle | `test_dispatch_*`, `test_worktree_*`, `test_agent_wrappers.py`, `test_generic_hook_sessionstart.py`, `test_agent_wrapper_hook_retry.py`, `test_close_wrapper_contracts.py`, `token-api/tests/test_dispatch_persona_clobber.py`, `test_aspirant_launch.py` | Existing behavior: command construction, wrapper hooks, worktree lifecycle, and dispatch handoff. | Blocking prod-gate. | Subprocess/path sensitivity should be isolated, not reclassified as advisory. |
| Deploy/restart/CD safety | `test_cd_restart.py`, `test_token_restart_*`, `test_tx_restart_*`, `test_launchd_socket.py`, `test_health_git_sha.py`, `test_dev_server_reaper.py`, `test_dev_worktree_side_effect_guard.py` | Existing behavior: restart/deploy guards, launchd/socket contracts, SHA reporting, and dev side-effect protection. | Blocking prod-gate. | Mocked launchd/Tailscale/CD boundaries still protect existing deploy behavior. |
| Enforcement, phone, TTS, notification routing | `test_enforcement_*`, `test_pavlok_routes.py`, `test_phone_*`, `test_tts_*`, `test_comms_router.py`, `test_voice_pool.py`, `test_wave1_media_telemetry.py`, `test_discord_fixer_routing.py` | Existing behavior when they assert current routing/contracts; possibly ambiguous only if a test asserts an unbuilt device integration promised by the vault. | Blocking prod-gate for current mocked contract tests. | Real-device/audio/Discord availability checks require per-test review: existing service contract -> blocking; future/unbuilt device capability -> bounty. |
| Runtime path, vault isolation, NAS broad-search guardrails | `test_runtime_path_config.py`, `test_runtime_write_protect.py`, `test_runtime_unlock_guard_hook.py`, `test_vault_isolation.py`, `test_vault_routing.py`, `test_broad_nas_search_guard.py`, `test_nas_grep.py` | Existing behavior: current guards against live runtime/vault mutation and unsafe search. | Blocking prod-gate. | Filesystem/NAS risk is why the guards matter; do not demote unless a specific test is only future-intent. |
| CI/PR automation correctness that affects merge/deploy loop | `test_pr_flag.py`, `test_pr_step_hardening.py`, `test_pr_gh_guard_hook.py`, `test_coderabbit_*`, `test_uv_python_policy_hook.py` | Existing behavior: current merge/deploy automation and policy guards. | Blocking prod-gate if prod health includes automation health. | Emperor may exclude automation from prod service health, but this is a policy ruling, not a flake/cost ruling. |
| Exact-rendering and CLI ergonomics tests | Output text snapshots, help/menu insertions, cosmetic statusline rendering. | Existing behavior if they pin current operator-facing output; bounty only if they assert a vault-intended UI that is not built. | Default blocking when current behavior; bounty only for unbuilt vault intent. | Low criticality does not by itself move a test to advisory under this framing. |

### Bounty/advisory candidates: vault intent that may not exist yet

These families should stay outside required prod-gate until the behavior ships
and the test graduates.

| Family | Representative tests | Exists-vs-intended read | Recommendation | Notes |
|---|---|---|---|---|
| Bounty board / unbuilt vault-intent tests | `token-api/tests/bounty_board/**`, `cli-tools/tests/bounty_board/**` | Vault-intended behavior that may not be implemented. | Advisory/bounty only; auto-`xfail(strict=False)`. | `xfail` means the bounty remains open; `xpass` means graduate or ask the Emperor whether the feature is now real. |
| W-A coherence-map vault-intent feeders | Future tests generated from `coherence-map/*`, including the identified vault features without tests. | Vault-derived intended architecture, unless paired with a pin of current behavior. | Bounty for intended/unbuilt side; blocking for any pin asserting current behavior. | Use pin/bounty pairs when current implementation contradicts vault intent. |
| Future live integrations not yet implemented | Future real Mac Token API, Tailscale, Discord daemon, Pavlok, phone, audio, Stream Deck, NAS checks when they assert capability not yet built or stabilized. | Vault-intended/unbuilt if the repo cannot currently provide the behavior. | Advisory/bounty until implementation exists. | Once a real integration is implemented and supported, its regression tests become blocking unless Emperor says otherwise. |
| Reliability probes / flake hunts / randomized repeat runs | Nightly order-dependence sweeps, repeated xdist/serial comparisons, long timeout probes. | Ambiguous: often tests test-suite/environment health rather than product behavior. | Advisory/nightly unless rewritten as a deterministic assertion of existing behavior. | This is one of the few genuine ambiguity zones requiring Emperor ruling if proposed for prod gate. |

### Dev quality-only lane

These remain the dev `main` hot path. They are not a reason to add pytest back to
`pr.yml` or `push.yml`.

| Family | Current location | Exists-vs-intended read | Recommendation |
|---|---|---|---|
| Formatting | `pr.yml` / `push.yml`: `ruff format --check` | Static quality gate, not pytest. | Dev quality-only; blocking on PR to `main`, advisory on push. |
| Lint | `pr.yml` / `push.yml`: `ruff check` | Static quality gate, not pytest. | Dev quality-only; blocking on PR to `main`, advisory on push. |
| Typecheck | `pr.yml` / `push.yml`: `mypy --ignore-missing-imports` | Static quality gate, not pytest. | Dev quality-only; blocking on PR to `main`, advisory on push. |
| CodeRabbit review signal | CodeRabbit app / push-side chill review | Review signal, not pytest. | Dev quality/review lane per current branch protection. |

## Needs Emperor decision

These are policy rulings or genuinely ambiguous cases. They should be surfaced
to Custodes for Emperor sign-off before any workflow or marker change.

| Decision needed | Why it matters | Default recommendation |
|---|---|---|
| Keep current `prod-gate.yml` as **all non-bounty pytest**, or curate a narrower required subset? | Current workflow already matches the simple exists-vs-intended split: non-bounty blocks, bounty advises. A curated subset creates marker drift and authority burden. | Keep all non-bounty tests required on prod unless the Emperor explicitly curates. |
| How to classify live endpoint checks that fail because the environment is absent rather than because product behavior regressed? | Some live checks assert current supported behavior; others only probe local machine state or future integration reachability. | Existing supported behavior -> blocking after hardening. Environment/probe-only or future capability -> advisory/bounty pending Emperor ruling. |
| Should automation-health tests (`pr-step`, CodeRabbit reconciliation, GitHub guards) be part of prod health? | They protect the merge/deploy machinery rather than the runtime service itself. | Treat as blocking existing behavior unless Emperor defines prod gate as runtime-only. |
| What is the graduation rule for XPASS bounty tests? | An XPASS means intended behavior may have shipped; leaving it bounty hides a now-real regression contract. | Require an explicit graduation PR: move test out of `bounty_board/`, remove bounty framing, and let it enter prod-gate after Emperor/Custodes sign-off if it changes gate surface. |
| Is branch protection on `prod` requiring `tests` already authoritative? | Repo docs say blocking requires GitHub settings outside the repo. | Custodes should verify/own the external branch-protection setting; do not encode assumptions in workflows. |

## Rationale notes

- The primary triage axis is **exists today vs vault intends**.
- Runtime cost, flake risk, mocked/live boundary, subprocess use, and external
  dependency labels are operational follow-ups. They can justify hardening,
  serialization, retries, quarantine, or Emperor review, but they do not decide
  bounty vs blocking by themselves.
- Current non-bounty tests mostly assert implemented behavior and therefore
  remain natural prod-gate material.
- Current bounty-board tests are the right place for vault-derived features that
  may not be built yet.
- Pin/bounty pairs preserve both sides of a coherence conflict: pin the current
  implementation in the blocking suite; bounty the vault-intended replacement
  until it ships.

## No-change verification

No workflow behavior is changed by this artifact. The only intended repo change
is this markdown recommendation.
