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
>   /Volumes/Imperium/Imperium-ENV/Terra/Sessions/needs-session-name-1137.md
>
> Unmet conditions: `extensively_validated`, `vault_searched`, `committed`, `pushed`, `pr_opened`, `coderabbit_passed`, `sanguinius_satisfied`.
> This session is not done. Either:
>   1. Address the unmet condition and flip its frontmatter flag, or
>   2. Escalate to Emperor via /api/notify if you are blocked, or
>   3. Mark inapplicable conditions in `victory_skip` (with justification in the doc body).
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

Reviewed the GT/harness vault corpus surfaced by `obsidian search`: `Terra/Ultramar/
Golden Throne Protocol.md` (canonical), `Mars/Logs/2026-06-05-gt-harness-proof-spec.md`
(this run's spec), and the titles `Golden Throne Completeness Checklist`, `Golden Throne
- Three-Stage Evolution`, `Golden Throne Enforcement Coordinator`, `Golden Throne
Implementation`, `Token API Golden Throne Recovery`.

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
