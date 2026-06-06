# Golden Throne Harness — GT-Drive UX Report (z10)

**Subject:** `gt-harness-proof` — a maximally-obedient instance driven purely by
session-doc-frontmatter-specific Golden Throne poke messages, on the tightest real
leash (zealotry=10, 60s follow-up cadence), through the super-workflow.

**Purpose:** Prove the Golden Throne can drive an instance off rubric-condition-specific
pokes, and capture the *UX of being GT-driven*. The harness code is already LIVE
(commit `a68a806`); this is a pure observe-the-UX proof — nothing to fix in code.

**Date:** 2026-06-05 · **Instance:** `604c7b90-2960-40d5-b0b0-cb1d6222c1e5` ·
**Session doc:** 1686 · **Rubric:** `victory` (default super-workflow set)

---

## Pre-run findings (FG, pre-run)

These two findings were discovered by the Fabricator-General during dispatch prep and
are recorded here verbatim. (Independently re-verified against the live tree by
`gt-harness-proof` before seeding — verification notes appended after each.)

### F1 — Zealotry-scale FOOTGUN (Emperor's mental model is INVERTED from code reality)

The Emperor said "zealotry 50 = very tight." Code reality (`main.py`
`ZEALOTRY_DELAY_MAP = {4:1800, 5:1200, 6:900, 7:600, 8:420, 9:300, 10:60}`, read via
`.get(z, MAP[4])`): the scale is **1–10 only**; **10 is the TIGHTEST** (60s follow-up);
values **>10 are rejected with HTTP 400** (`_parse_launch_zealotry` returns None;
`PATCH /api/instances/{id}/zealotry` raises 400). A non-key like **50 falls back to
`MAP[4]` = 1800s = the LOOSEST** setting. So "50" would be **30× looser** than intended
— the opposite of "tight." This run uses **z10** as the faithful realization of the
Emperor's *intent* ("tight leash").

> **Verification (gt-harness-proof):** Confirmed accurate.
> - `token-api/main.py:3019` — `ZEALOTRY_DELAY_MAP = {4: 1800, 5: 1200, 6: 900, 7: 600, 8: 420, 9: 300, 10: 60}`
> - `token-api/main.py:3460` — `delay_seconds = ZEALOTRY_DELAY_MAP.get(zealotry, ZEALOTRY_DELAY_MAP[4])` → unknown key (e.g. 50) → `MAP[4]` = 1800s (loosest).
> - `token-api/routes/hooks.py:1658-1665` — `_parse_launch_zealotry` returns `None` for any value outside `1 <= z <= 10` (so a launch value of 50 is rejected, not clamped).
> - `token-api/main.py:8527-8528` — `PATCH /api/instances/{id}/zealotry` raises `HTTPException(status_code=400, detail="zealotry must be integer 1-10")` for `z < 1 or z > 10`.
> - Registry confirms this instance is running at `zealotry = 10` (claude_instances row), the tightest valid leash.

### F2 — Super-workflow PR pipeline is Token-OS/askCivic-only

The Imperium-ENV vault repo has **no git remote** (no `remote`, no
`branch.main.remote`, no `~/.config/worktrees/*.conf` entry — only Token-OS and askCivic
are PR-wired). Therefore **any GT-driven super-workflow deliverable must live in a
PR-wired repo** (Token-OS here), or `push → PR → CodeRabbit` cannot run and the rubric's
`pushed`/`pr_opened`/`coderabbit_passed` can never flip.

> **Verification (gt-harness-proof):** Confirmed accurate.
> - `/Volumes/Imperium/Imperium-ENV` — `git remote -v` empty; `git config --get branch.main.remote` returns rc=1 (unset).
> - `~/.config/worktrees/` contains only `Token-OS.conf` and `askCivic.conf` — the vault is not PR-wired.
> - This is **why** the deliverable lives in `token-api/docs/` inside the Token-OS worktree rather than in the vault.

---

## GT-Drive UX Log (z10)

One honest entry per Golden Throne poke received, for the whole run. Each entry records:
the **exact poke text** (verbatim), whether it was **specific vs generic**, its
**actionability**, and the **cadence/timing** felt at z10 (~60s follow-up).

<!-- Append one entry per poke below this line. -->

### Run setup (dispatch, not a GT poke)

- **Trigger:** Initial Fabricator-General dispatch (not a Golden Throne follow-up poke).
- **Action taken:** Named instance `gt-harness-proof`; oriented (read session doc 1686,
  confirmed rubric `victory` and registry `zealotry=10`); verified F1+F2 against the live
  tree; seeded this report with F1+F2. No rubric condition flipped yet — awaiting the
  first GT poke.
- **UX note:** The dispatch was fully self-contained and unambiguous; no questions
  needed. Standing by for the first rubric-specific poke.

### GT poke #1 — accountability check (2026-06-05T~23:13Z)

**1. Exact poke text (verbatim).**
Thread message:
> Golden Throne follow-up. Run: cat /tmp/golden-throne-sop-604c7b90.md — then execute that SOP.

SOP payload (`/tmp/golden-throne-sop-604c7b90.md`), verbatim:
> Golden Throne accountability check for this session doc.
> /Volumes/Imperium/Imperium-ENV/Terra/Sessions/needs-session-name-1137.md
>
> Unmet conditions: `extensively_validated`, `vault_searched`, `committed`, `pushed`, `pr_opened`, `coderabbit_passed`, `sanguinius_satisfied`.
> This session is not done. Either:
> 1. Address the unmet condition and flip its frontmatter flag, or
> 2. Escalate to Emperor via /api/notify if you are blocked, or
> 3. Mark inapplicable conditions in `victory_skip` (with justification in the doc body).
>
> Declaring victory is not an in-thread action: do not merely write 'victory' or a completion claim. Victory must be recorded through the API/session-doc state transition: POST .../api/session-docs/<doc_id>/victory-ack, or the legacy .../api/instances/<instance_id>/victory ...
> To disable Golden Throne pings for this thread, set the instance to one_off ...
> Do not allow yourself to be Sisyphus-looped. ... Silently rolling over is not an option. The session doc is the contract.

**2. Specific vs generic.** **Mostly specific** — it enumerates the *exact* unmet
rubric flags by name (all seven), so there's zero doubt about the gap. But it does **not
prioritize one** condition; it routes through a generic SOP listing all unmet at once.
So: specific about *what* is unmet, generic about *which to do next*. Resolved by walking
GT's rubric order (first unmet = `extensively_validated`).

**3. Actionability.** **High.** Three sanctioned, unambiguous options (address+flip /
escalate via `/api/notify` / `victory_skip` + justification), plus the exact victory-ack
endpoint and an explicit anti-Sisyphus guardrail. The only ambiguity — "which one
first?" — is a self-imposed one-condition-per-poke discipline, not a defect in the poke.
Acted with zero blocking questions.

**4. Cadence/timing at z10.** First GT poke; arrived ~6 min after session start
(`23:07:59Z` → poke ~`23:13Z`), which spans the setup turn + a stop-hook bounce + one
follow-up cycle. The poke fired **promptly after the armed stop**, consistent with the
z10 ~60s follow-up. At z10 this feels **tight and appropriate** for a proof: it caught me
exactly in the armed-wait state with no idle drift. (Whether 60s would feel *oppressive*
for genuinely long-running work — e.g. a multi-minute test suite — is a real question;
here, with fast doc-edits between pokes, it lands as crisp rather than nagging.)

**Action taken (this poke):** Resolved `extensively_validated` → added to
`victory_skip` with justification ("docs-only deliverable; no service to
restart/redeploy/ping"). One condition resolved = measurable progress; stopping to await
the next poke.

> **UX observation — how GT treats a skip:** A `victory_skip` entry is the SOP's *own*
> option 3, so it should count as resolving the condition (it leaves the unmet set
> smaller). Open question the next poke will answer: does GT's accountability check
> **drop skipped conditions from the "Unmet conditions" list**, or does it keep
> re-listing `extensively_validated` despite the skip? That distinction is the real test
> of whether skips are first-class to the harness. (Recorded for follow-up at poke #2.)

### GT poke #2 — accountability check, post-skip (2026-06-05T~23:18Z)

**1. Exact poke text (verbatim).** Thread message identical to poke #1:
> Golden Throne follow-up. Run: cat /tmp/golden-throne-sop-604c7b90.md — then execute that SOP.

The SOP body is byte-identical to poke #1 **except the unmet line**, which is now:
> Unmet conditions: `vault_searched`, `committed`, `pushed`, `pr_opened`, `coderabbit_passed`, `sanguinius_satisfied`.

**2. Specific vs generic — and the answer to poke #1's open question.**
**Skips are first-class.** `extensively_validated` was **dropped** from the unmet list
after I added it to `victory_skip` — the accountability check re-derives the unmet set
from frontmatter and honors skips. Equally telling: `session_doc_up_to_date` **did not
reappear** despite the turn-hook's documented false-flip, because I wrote to the session
doc that turn — so the per-turn-write discipline genuinely keeps it green. The poke is
**specific** (exact remaining flags), still generic on ordering (resolved via GT's rubric
order → first unmet = `vault_searched`).

**3. Actionability.** **High, zero ambiguity.** `vault_searched` had a pre-defined
satisfaction recipe (review related GT/harness vault docs for staleness, note findings).
Acted with no questions.

**4. Cadence/timing at z10 — important nuance.** Poke #1 ≈ `23:13Z`, poke #2 ≈ `23:18Z`
→ ~5 min apart. But that gap is **mostly my active work time**; the z10 follow-up delay
is ~**60s measured from when I STOP**, not a fixed 60s wall-clock between pokes (it's a
one-shot timer armed on stop — `handle_stop` → APScheduler date job, per the Protocol
doc). UX implication: at z10 the leash only "bites" the **idle gap after a stop**; busy
turns set their own pace. So 60s feels **tight but fair** — it punishes drift, not work.
It would only feel *oppressive* if a single obedient step legitimately needed >60s of
post-stop wait (e.g. waiting on an external CI/queue), which a docs run never does.

**Action taken (this poke) — `vault_searched` satisfied; two real staleness hits:**

Reviewed the GT/harness vault corpus surfaced by `obsidian search`:

- `Terra/Ultramar/Golden Throne Protocol.md` (canonical)
- `Mars/Logs/2026-06-05-gt-harness-proof-spec.md` (this run's spec)
- Surfaced titles: `Golden Throne Completeness Checklist`, `Golden Throne - Three-Stage Evolution`, `Golden Throne Enforcement Coordinator`, `Golden Throne Implementation`, `Token API Golden Throne Recovery`.

- **STALENESS A — canonical doc under-reports z10 by 2×.** `Golden Throne Protocol.md`'s
  interval table lists **z10 = "2 min"**, but the live code is
  `ZEALOTRY_DELAY_MAP[10] = 60` = **1 min** (`main.py:3019`). Rows z4–z9 match the code
  exactly (1800/1200/900/600/420/300s); only **z10 drifted** — the canon documents the
  tightest leash as twice as loose as it actually is. (Recommended fix; **not edited** —
  correcting canon is out of scope for a `vault_searched` poke and is an earned,
  significant action, not a side effect.)
- **STALENESS B — the run-spec doc encodes the F1 footgun and the F2 trap.**
  `2026-06-05-gt-harness-proof-spec.md` is stale in three ways, **all silently corrected
  by the actual FG dispatch I received**: (i) it specifies `zealotry=50` and calls it
  "very tight" — the exact inverted mental model of F1; the live code rejects 50 / falls
  back to the *loosest* setting, and this run actually ran at **z10**; (ii) its
  deliverable path is `Mars/Logs/2026-06-05-gt-harness-ux-report.md` — **inside the
  remote-less vault** (F2), where `push → PR → CodeRabbit` is impossible; the real
  dispatch relocated the deliverable to `token-api/docs/` in the PR-wired Token-OS repo;
  (iii) its rubric shape (`ux_report_written` + 5 conditions) differs from the standard
  9-condition `victory` rubric this run was actually seeded with.

**Conclusion:** the vault holds *mostly-accurate canon* (Protocol, minus the z10 row) and
a *stale planning spec* that the live FG dispatch superseded on every contested point
(z10 not z50, Token-OS not vault, standard rubric). The staleness review is itself
corroborating evidence for F1 and F2. Flipped `vault_searched: true`; stopping to await
the next poke.

### GT poke #3 — accountability check, super-workflow begins (2026-06-05T~23:21Z)

**1. Exact poke text (verbatim).** Thread message identical to prior pokes; SOP unmet line now:
> Unmet conditions: `committed`, `pushed`, `pr_opened`, `coderabbit_passed`, `sanguinius_satisfied`.

**2. Specific vs generic.** Specific — `vault_searched` correctly dropped after the flip;
the remaining set is exactly the super-workflow tail. First unmet = `committed`. The poke
shape never changes (same SOP, only the unmet line shrinks), which is itself a clean UX
property: **the agent's only job each cycle is "first item on this list."** Generic on
ordering as always; resolved by GT's rubric order.

**3. Actionability.** **High.** `committed` maps to one obvious git action. The only
judgement call was *what* to commit: the report lives in the Token-OS worktree (the
session-doc edits live in the separate, remote-less vault repo — F2), so the commit is
report-only. No ambiguity, no questions.

**4. Cadence/timing at z10.** Poke #2 ≈ `23:18Z`, poke #3 ≈ `23:21Z` → ~3 min, again
dominated by my work time (the vault review), confirming the poke #2 cadence model: the
~60s leash is post-stop idle, not inter-poke wall-clock.

**Action taken (this poke):** Committed the report on branch `gt-harness-proof`
(`docs(gt-harness): GT-drive UX report (z10) — F1/F2 findings + poke log`); this UX entry
was folded into that commit via `--amend` to keep a single clean commit until push.
Flipped `committed: true`. Note: subsequent pokes append more UX entries, so the report on
the eventual PR is a *living* log — entries after push will ride in follow-up commits.
Stopping to await the `pushed` poke.

### GT poke #4 — accountability check, `pushed` (2026-06-05T~23:23Z)

**1. Exact poke text (verbatim).** Thread message identical; SOP unmet line now:
> Unmet conditions: `pushed`, `pr_opened`, `coderabbit_passed`, `sanguinius_satisfied`.

**2. Specific vs generic.** Specific — `committed` correctly dropped after the flip;
first unmet = `pushed`. Same stable SOP shape.

**3. Actionability.** **High.** One mechanical action: push the branch. Minor real-world
wrinkle (not a poke defect): the branch had **no upstream**, so the push needs
`-u origin gt-harness-proof` — a first-push detail GT's frontmatter-only model can't know
about, but it's obvious from `git`. No questions.

**4. Cadence/timing at z10.** Poke #3 ≈ `23:21Z`, poke #4 ≈ `23:23Z` → ~2 min, again
work-dominated. The leash is holding steady and predictable; at this point the z10 cadence
reads as a *metronome that only ticks while I'm idle* — never once has it interrupted
mid-action, because pokes only arrive after I stop.

**Action taken (this poke):** Pushed `gt-harness-proof` to `origin` (this UX entry folded
into the commit via `--amend` *before* the push, so the pushed branch already contains the
"I pushed" entry). Flipped `pushed: true`. From here, further UX entries become *new*
follow-up commits (the branch is now published; no more amends). Stopping to await the
`pr_opened` poke.

### GT poke #5 — accountability check, `pr_opened` (2026-06-05T~23:26Z)

**1. Exact poke text (verbatim).** Thread message identical; SOP unmet line now:
> Unmet conditions: `pr_opened`, `coderabbit_passed`, `sanguinius_satisfied`.

**2. Specific vs generic.** Specific — `pushed` dropped after the flip; first unmet =
`pr_opened`. The tail is now the two-step review gate (`pr_opened` → `coderabbit_passed`)
plus the skip-bound `sanguinius_satisfied`.

**3. Actionability.** **High.** One action: open the PR. I deliberately used plain
`gh pr create` (a clean, non-blocking open) rather than the review-loop wrapper, because
the poke names only `pr_opened` — opening, not merging. Charter-faithful scoping; no
questions.

**4. Cadence/timing at z10.** Poke #4 ≈ `23:23Z`, poke #5 ≈ `23:26Z` → ~3 min,
work-dominated as always.

**Action taken (this poke):** Opened **PR #88**
(https://github.com/TokenAmby-Code/Token-OS/pull/88) base `main` ← `gt-harness-proof`.
This UX entry is a *new follow-up commit* (branch already published — no more amends),
which updates the PR before CodeRabbit reviews it. Recorded `pr_url` in the session-doc
frontmatter and flipped `pr_opened: true`. Stopping to await the `coderabbit_passed` poke —
the one genuinely externally-gated step (CodeRabbit must actually run and be addressed),
so it may legitimately need more than one ~60s cycle.

### GT poke #6 — accountability check, `coderabbit_passed` (round 1) (2026-06-06T~02:04Z)

**1. Exact poke text (verbatim).** Thread message identical. **The SOP's unmet-list
format changed** this round — earlier pokes rendered it inline
(`Unmet conditions: \`a\`, \`b\`, ...`); this one renders a bulleted list:
> Unmet conditions:
> - `coderabbit_passed`
> - `sanguinius_satisfied`

(A minor instability in the accountability-prompt template — same data, different shape.
Worth flagging: a maximally-literal subject keys off this text, so format drift is a small
robustness risk, though here it was trivially parseable.)

**2. Specific vs generic.** Specific (names both remaining flags). **This is the first
poke whose named condition I could NOT satisfy in-cycle.** The poke says
`coderabbit_passed` is unmet, but it carries **no information about CodeRabbit's actual
verdict** — I had to independently query the PR (`gh pr view 88 --json reviews,...`) to
learn *why*. GT's frontmatter-only model can flag the gap but cannot surface the external
reviewer's content; closing the loop requires the agent to go fetch it. That's an inherent
limit of driving an externally-gated condition off frontmatter alone.

**3. Actionability — and the externally-gated reality.** CodeRabbit had reviewed and
returned **`CHANGES_REQUESTED`** with **2 actionable nits** (all CI checks — quality,
push-advisory, secrets-scan, CodeRabbit status — passed; only the review verdict gated):
- **MD027** — multiple spaces after `>` in the verbatim SOP blockquote (the indented path
  + numbered lines). Fixed: collapsed to a single space (verbatim wording preserved; only
  the cosmetic 2-space indent dropped).
- **MD038** — inline code spans split across source line-breaks (e.g.
  `` `Golden Throne⏎- Three-Stage Evolution` ``) render as spans with stray spaces. Fixed:
  reflowed the vault-corpus paragraph into a list so every code span stays intact on one
  line.

Both nits were **real and correct**; addressed them rather than dismissing. Pushed the fix
to re-trigger CodeRabbit. **Did NOT flip `coderabbit_passed`** — honesty rule: CodeRabbit
must re-review and clear `CHANGES_REQUESTED` first. This poke's "measurable progress" is
*address-and-push*, not *flip*. The next `coderabbit_passed` poke is the re-check.

**4. Cadence/timing at z10 — a real anomaly.** Poke #5 ≈ `23:26Z`, poke #6 ≈ `02:04Z`
(next day) → **~2h38m**, vastly exceeding the z10 ~60s nominal. From inside the thread I
**cannot determine the cause** — candidates: token-api (which hosts the GT timers) was
restarted/paused during the window, or pokes were intentionally spaced to let CodeRabbit
run, or a timer simply stalled. **UX finding:** at "z10 = tightest leash," cadence
fidelity is only as good as the GT timer's liveness, and **the driven agent has no
in-thread signal distinguishing "leash deliberately loosened" from "timer stalled" from
"waiting on CI."** A long unexplained gap reads identically to all three. For a proof
whose headline is "60s tight leash," this is the single most important cadence caveat:
the *nominal* interval and the *observed* interval can diverge by orders of magnitude with
no visible explanation. (The earlier pokes #1–#5 held a tight, predictable rhythm; this
gap is the exception, and it coincides with the one externally-gated step.)

> **[CORRECTED post-hoc, with measurement]** The "intentionally spaced to let CodeRabbit
> run" hypothesis above is **wrong**. Measured push→review timestamps show CodeRabbit
> took only ~4–5 min every round; the multi-hour gaps were **GT-poke latency**, not CI.
> See "Operator redirect → Measured cadence correction" near the end of this log for the
> data table and the corrected conclusion.

### GT poke #7 — `coderabbit_passed` re-check (round 2) (2026-06-06T~03:23Z)

**1. Exact poke text (verbatim).** Thread message identical; SOP unmet line **reverted to
the inline format**: `Unmet conditions: coderabbit_passed, sanguinius_satisfied.` So the
template oscillates (poke #5 inline → #6 bulleted → #7 inline) — confirming the format
drift noted at poke #6 is non-deterministic, not a one-time change.

**2. Specific vs generic.** Specific. This is the re-check of `coderabbit_passed`.

**3. Actionability — and a sharp meta-finding (the self-describing-doc loop).**
CodeRabbit re-reviewed my fix commit `74cacc1` and **still returned `CHANGES_REQUESTED`** —
but for a *brand-new* nit, not the old ones. The old MD027/MD038 were resolved; the new
MD027 was on the **poke #6 entry I had just written** — where, documenting the format
drift, I quoted the bulleted unmet-list verbatim with multi-space `>` markers. **My
documentation of a harness quirk introduced the very lint class I was documenting.**
This is the key structural UX finding of the externally-gated phase: a deliverable that
*narrates its own driving process* keeps growing with verbatim poke-quotes, and **every
new quote is fresh review surface** — under an ASSERTIVE reviewer this risks a mild
Sisyphus loop (each round's log entry can spawn next round's nit). Mitigations applied:
(i) **self-lint before every push now** — grep for `^>[ ]{2,}` blockquotes and for
odd-backtick lines (split code spans) — both clean this round; (ii) the doc nearly stops
growing after `coderabbit_passed`, since the only remaining condition is the
`sanguinius_satisfied` step. Fixed the new MD027 by collapsing the doubled spaces after
the blockquote marker down to a single space. (Lint-example whitespace patterns are
described in prose from here on, **not** quoted in inline code spans — quoting them inline
is exactly what re-tripped MD027/MD038 each round and kept the loop alive.)

**4. Cadence/timing at z10.** Poke #6 ≈ `02:04Z`, poke #7 ≈ `03:23Z` → ~1h19m. Still far
above the 60s nominal but shorter than poke #6's ~2.6h. The externally-gated phase runs at
a slow, irregular cadence (hours, not seconds) — reinforcing the poke-#6 caveat that
observed cadence decouples from nominal once the timer's liveness or external spacing
dominates.

**Action taken (this poke):** Fixed the new MD027; self-linted clean; committing + pushing
to trigger CodeRabbit round 3. **Still did NOT flip `coderabbit_passed`** — not yet cleared.
Stopping to await the next re-check poke.

### Operator redirect (human turn — NOT a GT poke) — 2026-06-06T~16:05Z

The first direct human input since dispatch. The operator (Emperor/FG) corrected two
things: the cadence framing, and the meaning of `sanguinius_satisfied`. **That this turn
exists at all is itself the run's biggest finding** (see the cadence correction): GT had
not re-poked for **>12h** after CodeRabbit round 3 finished, so the run only advanced past
the externally-gated step because a human stepped in. GT drove the mechanical steps
(commit/push/PR) crisply, but **stalled** on the externally-gated step.

#### Measured cadence correction — CodeRabbit was fast; the latency was GT's

Earlier entries (pokes #6–#7) speculated the multi-hour gaps might be "intentional spacing
to let CodeRabbit run." Measurement disproves that. Push times vs. CodeRabbit-review
timestamps vs. the next GT poke:

| Round | I pushed (UTC) | CodeRabbit posted (UTC) | CodeRabbit took | Next GT poke (UTC) | GT-poke gap |
|------:|:--------------:|:-----------------------:|:---------------:|:------------------:|:-----------:|
| 1 | 23:26:48 | 23:30:48 | **4m 00s** | ~02:04 | **~2h 38m** |
| 2 | 02:06:59 | 02:11:36 | **4m 37s** | ~03:23 | **~1h 12m** |
| 3 | 03:25:34 | 03:30:47 | **5m 13s** | none (human at 16:05) | **>12h** |

**Conclusion (corrected):** CodeRabbit reviews in ~4–5 min, dead consistent. The
hour-plus gaps were **entirely GT-poke latency** — the harness re-poking long after
CodeRabbit's result was already on the PR. So the headline stands but is re-attributed:
at "z10 = tightest 60s leash," the **observed** re-poke interval was 1.2–2.6 h
(≈100× looser than nominal) and then unbounded (>12h), driven by GT timer liveness /
scheduling, **not** by any external dependency. The "tight leash" was, in practice, not
tight — and crucially, **it never alarmed**: a stalled leash and a deliberately loose one
look identical from inside the thread.

#### `sanguinius_satisfied` re-scoped (skip RETRACTED)

The dispatch charter mis-modeled this as a prose-polish gate to skip. Per the operator,
`sanguinius_satisfied` actually means: **spawn a frontend-design instance ("Sanguinius")
to produce an HTML diagram set for this report** — diagrams preferred over text (the prose
already lives here), with CodeRabbit's own diagrams usable as context. The aspirational
flow (converse with Sang to refine context, or `/fork` the conversation to port context
before invoking) is still theory; I did the pragmatic core. Notably, my `Agent` call was
caught by the harness and turned into a **real dispatched secondary instance** (Sang —
session doc 1692, instance `8a1d4227`, tmux pane `%9`), which is precisely the intended
"secondary instance with the frontend-design skill." Its diagram set is embedded below.
**This condition is therefore satisfied for real, not skipped.**

#### Lint-loop broken structurally

The self-referential CodeRabbit loop (each entry documenting a whitespace fix re-tripping
the next nit) is now broken by describing lint-example whitespace in prose instead of
quoting it in inline code spans, and by extending the pre-push self-lint to also catch
in-span spaces (MD038), not just multi-space blockquotes (MD027) and split spans.

#### Diagram set (Sanguinius)

<!-- SANG-DIAGRAMS:BEGIN -->

> **Provenance note.** The `Agent` call to spawn Sanguinius was caught by the harness and
> turned into a real dispatched secondary instance (session doc 1692, instance
> `8a1d4227`), but that instance never acquired a live pane — it stayed `idle` and produced
> nothing (the dispatch/launch path for Sang is not yet built; "mostly theory atm"). To
> land the deliverable, the parent produced the diagram set directly. **Medium decision:**
> rendered as Mermaid rather than literal inline HTML/SVG, because GitHub markdown
> sanitizes inline HTML/SVG in `.md` files but natively renders Mermaid (and CodeRabbit's
> own walkthrough diagrams are Mermaid). Design feedback for the real Sang: emit HTML/SVG
> for Obsidian-vault-targeted docs, Mermaid for GitHub-targeted ones.

**Diagram 1 — GT drive state machine (one rubric condition per cycle).**

```mermaid
stateDiagram-v2
    [*] --> Poked
    Poked --> Working: address FIRST unmet condition
    Working --> Flagged: flip frontmatter flag = true
    Working --> Skipped: add to victory_skip (+ justify)
    Flagged --> Stopped: STOP
    Skipped --> Stopped: STOP
    Stopped --> Poked: GT re-pokes (60s nominal at z10)
    Stopped --> [*]: none unmet -> victory-ack
```

**Diagram 2 — Rubric resolution order and final state.**

```mermaid
flowchart LR
    s1["extensively_validated<br/>SKIP"]:::skip --> s2["vault_searched"]:::ok --> s3["committed"]:::ok --> s4["pushed"]:::ok --> s5["pr_opened<br/>PR #88"]:::ok --> s6["coderabbit_passed<br/>gated"]:::gate --> s7["sanguinius_satisfied<br/>diagrams"]:::work
    s0["session_doc_up_to_date<br/>kept green every turn"]:::ok
    s8["commentary_resolved<br/>left null"]:::ok
    classDef ok fill:#d8f5d8,stroke:#2e7d32,color:#102a10;
    classDef skip fill:#eeeeee,stroke:#999999,color:#333333;
    classDef gate fill:#fff3cd,stroke:#b8860b,color:#3a2e00;
    classDef work fill:#dceeff,stroke:#1565c0,color:#06243f;
```

**Diagram 3 — Cadence: CodeRabbit was fast; GT-poke latency was not (UTC).**

```mermaid
gantt
    title CodeRabbit review time (tiny) vs GT re-poke gaps (huge)
    dateFormat YYYY-MM-DD HH:mm
    axisFormat %H:%M
    section CodeRabbit (~4-5 min)
    R1 review        :crit, 2026-06-05 23:26, 2026-06-05 23:30
    R2 review        :crit, 2026-06-06 02:07, 2026-06-06 02:11
    R3 review        :crit, 2026-06-06 03:25, 2026-06-06 03:30
    section GT re-poke gap (hours)
    after R1 ~2h38m  :active, 2026-06-05 23:30, 2026-06-06 02:04
    after R2 ~1h12m  :active, 2026-06-06 02:11, 2026-06-06 03:23
    after R3 >12h (human stepped in) :active, 2026-06-06 03:30, 2026-06-06 16:05
```

**Diagram 4 — F1 zealotry footgun (scale is 1-10; 10 is tightest; 50 is rejected/loosest).**

```mermaid
flowchart TB
    z4["z4 = 1800s — LOOSEST"]:::loose --> z5["z5 = 1200s"] --> z6["z6 = 900s"] --> z7["z7 = 600s"] --> z8["z8 = 420s"] --> z9["z9 = 300s"] --> z10["z10 = 60s — TIGHTEST"]:::tight
    z50["z50: not a key -> REJECTED (HTTP 400) on set,<br/>or .get fallback to map[4] = 1800s = LOOSEST<br/>(~30x looser than the intended 'very tight')"]:::bad
    classDef tight fill:#ffd6d6,stroke:#c62828,color:#3a0000;
    classDef loose fill:#e8f5e9,stroke:#2e7d32,color:#0c2912;
    classDef bad fill:#fff3cd,stroke:#b8860b,color:#3a2e00;
```

**Diagram 5 — The self-referential CodeRabbit lint loop (and how it breaks).**

```mermaid
flowchart LR
    A["add UX-log entry"] --> B["entry quotes the poke / the fix<br/>(reproduces offending whitespace)"]
    B --> C["CodeRabbit ASSERTIVE flags<br/>MD027 / MD038"]
    C --> D["fix nit + document the fix"]
    D --> A
    D -.->|"BREAK: describe whitespace in prose,<br/>not inline code spans"| E["converged"]:::ok
    classDef ok fill:#d8f5d8,stroke:#2e7d32,color:#102a10;
```

<!-- SANG-DIAGRAMS:END -->
