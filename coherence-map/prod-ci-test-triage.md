# Prod CI Test Triage Recommendation

Date: 2026-06-29

Scope read: `.github/workflows/README.md`, `prod-gate.yml`, `pr.yml`, `push.yml`, `bounty-board.yml`, current `token-api/tests/**`, `cli-tools/tests/**`, and recent test/workflow history through `dc10149`.

## Authority boundary

This is a recommendation artifact only. **Gate membership is an Emperor decision.** No workflow membership, branch protection, or test marker behavior is changed here.

Current repo policy found:

- `pr.yml`: blocking `quality` check for PRs to `main`; runs ruff format, ruff lint, mypy; **no pytest**.
- `push.yml`: advisory non-`main` push signal; ruff/mypy and chill CodeRabbit are non-blocking; **no pytest**.
- `prod-gate.yml`: `tests` job for PR/push to `prod`, nightly, and manual; runs both pytest suites with `-m "not bounty"`.
- `bounty-board.yml`: advisory lane; runs only `-m bounty`; never required.
- Bounty tests live under `*/tests/bounty_board/`, are auto-marked `bounty`, and auto-`xfail(strict=False)` until their feature ships.

## Recommendation summary

Recommended posture:

1. Keep **prod-gate as the only heavyweight pytest lane**.
2. Keep **bounty-board advisory and excluded from prod-gate**.
3. Keep **dev path quality-only**: format/lint/typecheck/CodeRabbit, not pytest.
4. If the Emperor wants a leaner prod gate, split by risk rather than by project: put operator-safety, deploy/restart, DB/session, tmux/send, dispatch/worktree, and enforcement/notification regressions in required prod-gate; move speculative/live-environment probes and low-criticality exact-rendering checks to advisory/nightly.

## Classification by test family

### Required prod-gate candidates

These are recommended for required prod-gate membership because they protect live operator safety, production restart/deploy behavior, instance/session identity, or prompt delivery boundaries. Most are hermetic unit/model tests with mocked external edges; that gives high regression value without needing real live endpoints.

| Family | Representative tests | Runtime cost | Flake risk | External dependency risk | Regression criticality | Live-endpoint vs mock value | Recommendation |
|---|---|---:|---:|---:|---:|---|---|
| Token API DB/schema/session identity | `test_instances_registry.py`, `test_claude_instances_exterminatus.py`, `test_db_timer_schema_split.py`, `test_session_start_*`, `test_instance_*`, `test_persona_*`, `test_session_doc_pool.py` | Medium | Low-med: SQLite/tempdir heavy | Low: mostly temp DB | High | Mock/temp DB is the right value; catches schema/identity regressions before prod restart | Required prod-gate candidate |
| Golden Throne / victory / supervision state | `test_gt_*`, `test_victory_*`, `test_work_state_*`, `test_work_action_*`, `test_questions_gate.py`, `test_planning_state_autoclear.py` | Medium | Med: time/state machines | Low | High | Mocked DB/time gives deterministic coverage of control-plane failures | Required prod-gate candidate |
| Tmux/tmuxctld prompt-delivery and pane safety | `cli-tools/tests/test_tmuxctl_send_*`, `test_tmuxctld_*`, `test_tmux_typing_guard_*`, `test_tmuxctl_focus*`, `test_tmuxctl_occupancy.py`, `test_tmuxctl_freelist.py`, `token-api/tests/test_pane_write_queue_gate.py`, `test_send_path_tmuxctld_regression.py` | High | Med-high: shell/subprocess/fake tmux models | Low-med: fake tmux, local subprocess | Critical | Mock/fake-tmux is valuable; live tmux belongs in separate smoke/advisory unless made hermetic | Required prod-gate candidate, with serial CI where needed |
| Dispatch/worktree/wrapper lifecycle | `test_dispatch_*`, `test_worktree_*`, `test_agent_wrappers.py`, `test_generic_hook_sessionstart.py`, `test_agent_wrapper_hook_retry.py`, `test_close_wrapper_contracts.py`, `token-api/tests/test_dispatch_persona_clobber.py`, `test_aspirant_launch.py` | High | Med: subprocess and path-sensitive | Low-med: temp paths, fake commands | Critical | Model tests catch command construction and state handoff without touching live fleet | Required prod-gate candidate |
| Deploy/restart/CD safety | `test_cd_restart.py`, `test_token_restart_*`, `test_tx_restart_*`, `test_launchd_socket.py`, `test_health_git_sha.py`, `test_dev_server_reaper.py`, `test_dev_worktree_side_effect_guard.py` | Medium-high | Med: platform/path assumptions | Med: launchd/Tailscale/CD are mocked or isolated | High | Mock value is high; true Tailscale/Mac live smoke should stay separate | Required prod-gate candidate for hermetic tests |
| Enforcement, phone, TTS, notification routing | `test_enforcement_*`, `test_pavlok_routes.py`, `test_phone_*`, `test_tts_*`, `test_comms_router.py`, `test_voice_pool.py`, `test_wave1_media_telemetry.py`, `test_discord_fixer_routing.py` | Medium-high | Med: queues/time/network mocks | Med-high if unmocked; current tests mostly patch requests/services | High for operator-facing side effects | Mocked endpoint contract tests belong in prod-gate; real-device/audio/Discord checks should not hard-gate until hardened | Required prod-gate candidate for mocked contract layer |
| Runtime path, vault isolation, NAS broad-search guardrails | `test_runtime_path_config.py`, `test_runtime_write_protect.py`, `test_runtime_unlock_guard_hook.py`, `test_vault_isolation.py`, `test_vault_routing.py`, `test_broad_nas_search_guard.py`, `test_nas_grep.py` | Medium | Low-med: filesystem assumptions | Med if pointed at real NAS; current tests isolate/guard | High | Mock/temp filesystem is valuable to prevent live vault/runtime writes | Required prod-gate candidate |
| CI/PR automation correctness that affects merge/deploy loop | `test_pr_flag.py`, `test_pr_step_hardening.py`, `test_pr_gh_guard_hook.py`, `test_coderabbit_*`, `test_uv_python_policy_hook.py` | Low-med | Low-med | Med: GitHub/CodeRabbit usually mocked | Med-high | Mocked CLI/API behavior protects the automation lane; live CodeRabbit result remains separate status | Required prod-gate candidate if prod branch represents deployable repo health |

### Advisory / bounty lane

These should stay outside required prod-gate unless graduated by explicit sign-off.

| Family | Representative tests | Runtime cost | Flake risk | External dependency risk | Regression criticality | Live-endpoint vs mock value | Recommendation |
|---|---|---:|---:|---:|---:|---|---|
| Bounty board / unbuilt vault-intent tests | `token-api/tests/bounty_board/**`, `cli-tools/tests/bounty_board/**` | Low now; can grow | Low by design: xfail non-strict | Low unless authors add live probes | Not regression-critical until built | Value is advisory signal: xfail=open, xpass=graduate | Advisory/bounty only; never required as-is |
| Live endpoint smoke tests | Future real Mac Token API, Tailscale, Discord daemon, Pavlok, phone, audio, Stream Deck, NAS checks | High/variable | High | High | High if they pass, but failures often environment-caused | Live value is real, but poor hard-gate material without retries/quarantine | Advisory/nightly/manual until environment is stable and Emperor signs off |
| Reliability probes / flake hunts / randomized repeat runs | Nightly order-dependence sweeps, repeated xdist/serial comparisons, long timeout probes | High | High by purpose | Low-med | Medium | Good at finding drift; noisy for merge gating | Advisory/nightly, not merge-blocking |
| Coherence-wave future-intent tests | Any tests asserting desired architecture before implementation, especially pin/bounty pairs from `coherence-map/*` | Low-med | Low | Low | Low until implementation exists | Bounty value is roadmap pressure, not regression blocking | Advisory/bounty until implementation ships, then graduate |

### Dev quality-only lane

These belong on the dev `main` hot path and should remain fast/static. They are not a reason to add pytest back to `pr.yml` or `push.yml`.

| Family | Current location | Runtime cost | Flake risk | External dependency risk | Regression criticality | Live-endpoint vs mock value | Recommendation |
|---|---|---:|---:|---:|---:|---|---|
| Formatting | `pr.yml` / `push.yml`: `ruff format --check` | Low | Low | Low | Medium | N/A | Dev quality-only; blocking on PR to `main`, advisory on push |
| Lint | `pr.yml` / `push.yml`: `ruff check` | Low | Low | Low | Medium | N/A | Dev quality-only; blocking on PR to `main`, advisory on push |
| Typecheck | `pr.yml` / `push.yml`: `mypy --ignore-missing-imports` | Medium | Low | Low | Medium | N/A | Dev quality-only; blocking on PR to `main`, advisory on push |
| CodeRabbit review signal | CodeRabbit app / push-side chill review | Variable | Low-med | Med: external service | Medium | External reviewer value; not pytest | Dev quality/review lane per current branch protection |
| Exact-rendering and CLI ergonomics tests, if split later | Examples: output text snapshots, help/menu insertions, cosmetic statusline rendering | Low-med | Low | Low | Low-med | Mock value only; prod-critical only when operator safety depends on it | Candidate dev/advisory if Emperor wants a smaller prod-gate |

### Needs Emperor decision

These are policy decisions, not implementation facts. They should be surfaced to Custodes for Emperor sign-off before any workflow or marker change.

| Decision needed | Why it matters | Default recommendation |
|---|---|---|
| Keep current `prod-gate.yml` as **all non-bounty pytest**, or split to a curated required subset? | Current workflow is simple and avoids marker drift; curated subset can save minutes but creates maintenance and authority burden. | Keep all non-bounty tests required on prod unless runtime cost becomes unacceptable. |
| Should mocked contract tests for TTS/Pavlok/Discord/phone/CD be required prod-gate? | They protect live-impacting behavior but mention external systems. Current tests mostly mock calls and are deterministic. | Require mocked contract tests; keep real-device/live-endpoint tests advisory/manual. |
| Should tmux/tmuxctld model tests remain required even without a real tmux server? | They guard prompt delivery and human-input safety, but fake tmux can miss integration faults. | Require model tests; add optional live smoke separately rather than replacing them. |
| Should CI tooling tests (`pr-step`, CodeRabbit reconciliation, GitHub guards) be prod-gate required? | They do not test runtime service behavior, but they protect the merge/deploy machinery itself. | Require while prod health includes automation health; demote only if prod-gate minutes become a problem. |
| What is the graduation rule for XPASS bounty tests? | An XPASS means intended behavior shipped; keeping it bounty hides a now-real regression contract. | Require explicit graduation PR: move test out of `bounty_board/`, remove bounty framing, and let it enter prod-gate after Emperor/Custodes sign-off if it changes gate surface. |
| Is branch protection on `prod` requiring `tests` already authoritative? | Repo docs say blocking requires GitHub settings outside the repo. | Custodes should verify/own the external branch-protection setting; do not encode assumptions in workflows. |

## Rationale notes

- **Runtime cost:** the expensive part is not a single family; it is aggregate pytest plus subprocess-heavy tmux/dispatch/restart tests. `prod-gate.yml` already mitigates this with token-api xdist and cli-tools serial CI.
- **Flake risk:** highest risk comes from shell/subprocess models, time/queue tests, and anything accidentally touching live tmux/NAS/launchd. Existing conftests intentionally isolate Token API URLs, DB paths, vault roots, and tmux observability; preserve that isolation.
- **External dependency risk:** current required tests should stay at the mocked-contract layer. Tests requiring real Discord, Tailscale, Pavlok, phone, Stream Deck, audio, launchd mutation, or NAS availability should be advisory/manual unless hardened.
- **Regression criticality:** prod-gate should prioritize failures that can misroute prompts, mutate live runtime/vault state, break restart/deploy, orphan instances, corrupt DB state, or misfire operator-facing enforcement/notification.
- **Live-endpoint vs mock value:** mock tests are best for deterministic merge gates; live endpoint checks are best for nightly/manual smoke because they catch environment drift but can fail for reasons unrelated to the commit.

## No-change verification

No workflow behavior is changed by this artifact. The only intended repo change is this markdown recommendation.
