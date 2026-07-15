# k12-era agent registry rebuild — architecture rulings (rung 0)

**Status:** ratified 2026-07-15 (same-day sitting) — **R1/R2/R4/R5 as recommended; R3 as
amended** (the canonical shape binds surviving/new boxes; the Mac is exempted — its
deviation recorded as known and rider-mitigated, no corpse churn). **R6 remains OPEN**:
the Emperor flagged follow-up discussion owed on the DB write-door before closing; rung 2
(writers) stays gated on it. Rebuild rungs gated only on R1–R5 may proceed.
**Contract:** device-doctrine reconciliation rulings **#2** (Token-OS home = k12-personal,
no work-box copy), **#3** (k12-personal = inter-agent comms hub AND the only persistent
tmux estate; work-box agents run inside mini tmux envelopes echoing up), **#9** (WSL =
human contact surface; every box has a known ingress/egress proxy), **#10** (development
echo: repo-borne convergence is the default path) — vault docket
`Mars/Tasks/device-doctrine-reconciliation.md`, ratified 2026-07-14. Prerequisite already
landed: the agents.db vaporize lane (#730, `03ee6c5b`) erased every `~/.claude/agents.db`
reference, leaving one canonical resolution chain ending at
`~/runtimes/database/agents.db` — the registry can now be rebuilt without a shadow-path
tail.

## The problem

The agent registry (agents.db: instances, session-doc links, events, enforcement state)
grew up on the Mac as the accidental center of the estate. The Mac is dying (device
doctrine #4: inventory → vault → secrets → Token-OS/daemons → archive, GUI dissolves) and
its successor topology is *two* boxes with asymmetric roles: **k12-personal** (Token-OS
home, comms hub, only persistent tmux estate) and **k12-work** (civic true-vault host,
agents in envelopes, no persistent estate). The registry was never designed for that
shape. Before any rebuild code, six architecture decisions need ruling: where the
registry lives (R1), how the cutover sequences (R2), what git shape each box carries
(R3), how branches join to session docs (R4), what schema era the rebuild starts from
(R5), and who is allowed to write (R6).

## Ground truth

Repo-verified facts (every citation re-checked against `main` @ `03ee6c5b`, 2026-07-15):

| Fact | Where |
|---|---|
| Hub host is one constant: `_IMPERIUM_TOKEN_API_HOST = "100.95.109.23"` feeds every `token_api_url` | `cli-tools/lib/imperium_config.py:55` |
| Cutover flip is that one line + deploy; consumers re-source config, no other code change | `docs/cd-offline-node-propagation.md:150-152` |
| Branch→session-doc mapping is a heuristic: slugified PR `headRefName` LIKE-matched against doc frontmatter (docs carry no `branch:` field) | `cli-tools/src/cli_tools/daily_build/session_doc_resolver.py:3-7,70-76` |
| Deprecated `claude_instances` table still read/updated alongside `instances` v2 | `token-api/db_schema.py:338-410` |
| Sanctioned-write layer for instance rows already exists (runtime-writable field allowlist, forbidden-field guard) | `token-api/instance_mutation.py` |
| Stop hook writes agents.db directly via sqlite3 (`shared.AGENTS_DB_PATH`), not through the API | `token-api/stop_hook.py:30,49,85` |
| Session-end resume script writes directly too; DB chain `TOKEN_API_AGENTS_DB` > `TOKEN_API_DB` > `~/runtimes/database/agents.db` | `cli-tools/scripts/agent-session-end-resume.sh:82` |
| daily-build hard-enforces the Token-OS repo via the `cli-tools/pyproject.toml` sentinel | `cli-tools/src/cli_tools/daily_build/cli.py:54-60` |

Box-probe facts (live probes 2026-07-15, not derivable from the repo):

- **k12-personal already runs a WAL-hot `~/runtimes/database/agents.db` with NO token-api
  service on the box.** Explained: `stop_hook.py` and `agent-session-end-resume.sh` write
  directly via sqlite through the canonical env chain — the DB is alive without its API.
- **k12-personal's bare repo fetches straight from GitHub** (`origin` =
  `git@github.com:TokenAmby-Code/Token-OS.git`), matching the target-structure §2 shape.
- **Mac bare** carries `origin` + `github` remotes, both → GitHub (the duplicate named
  "an accident" in target-structure §2). **Mac runtime checkout** (`live/`) has exactly
  one remote: `origin` → the local CD bare *path* — no GitHub remote at all, which is why
  `gh` invoked from the runtime resolves no repo and attribution silently returns 0 rows
  (the rider below).
- **k12_daemon healthy** behind edge_proxy at `100.113.115.32:7780/k12/health`
  (Tailscale-interface bind only; token-api-to-be inherits the same single-door shape).

## R1 — Topology: where does the registry live?

**Decision needed:** one authoritative registry on the hub, or symmetric per-box
registries that sync?

**Recommendation: hub-authoritative on k12-personal.** Ruling #3 already makes
k12-personal the sole persistent estate and comms hub — every dispatch, wrapper event,
and SessionStart already routes through it. Work-box envelope agents echo attestations
*up* (ruling #3's explicit mechanic); they never need a local registry to consult, only a
network call (target-structure §7: "network call ≠ mount"). k12-work and WSL never grow
an agents.db. The Mac registry dies at cutover (R5 says what, if anything, is carried).

**Alternatives:** (a) symmetric per-box DBs with sync — rejected: recreates split-brain
across the exact boundary rulings #2/#3 just collapsed, and there is no consumer on
k12-work that can't be served by the hub API; (b) registry-in-GitHub (repo-borne, echo
#1) — rejected: instance state is runtime-hot mutable data, not code; echo #1 governs
*configuration*, not live state.

**Consequences:** all registry reads/writes are hub API calls (or hub-local sqlite for
the R6 residue); k12-work bring-up needs zero DB provisioning; a hub outage means no
registration — accepted, because ruling #3 already makes a hub outage mean no estate at
all.

## R2 — Cutover order

**Decision needed:** when does the k12-era schema land relative to the Mac→k12 cutover,
and what moves?

**Recommendation: adopt target-structure §6's migration order verbatim, amended in one
place — the k12-era schema lands on k12-personal BEFORE cutover** (§6 step 5 initializes
the data layer empty; under this ruling it initializes to the *new* schema), **so cutover
imports only what R5 carries instead of rsyncing the Mac DB wholesale.** §6 step 7's
"final rsync of DBs" narrows, for agents.db, to the R5 one-shot import. The consumer flip
stays the one-line `_IMPERIUM_TOKEN_API_HOST` constant (`cli-tools/lib/imperium_config.py:55`);
`docs/cd-offline-node-propagation.md:150-152` confirms no other code change is needed —
the hub URL follows the constant everywhere.

**Alternatives:** (a) rsync the Mac DB then migrate in place on k12 — rejected: imports
every accident the vaporize lane just paid to be rid of, and makes the new schema's first
state a converted old one; (b) big-bang schema + cutover same evening — rejected: couples
two rollbacks; §6 explicitly keeps DBs unmoved until step 7 so every earlier step can
back out by restarting Mac daemons.

**Consequences:** k12-personal runs the new schema in satellite mode first (real traffic
from the already-live direct writers, per the WAL-hot probe fact); cutover evening
touches data once, small and known.

## R3 — Git shape per box

**Decision needed:** is the k12 git topology (target-structure §2) canonical for every
box, including the Mac for its remaining lifetime?

**Ratified verdict (amended from the posed recommendation): the k12 shape is canonical
for surviving and future boxes; the Mac is EXEMPT.** The shape: bare with `origin` =
GitHub, detached CD `live/` checkout, single `~/worktrees/<repo>/` root. `battlefield/`
(dev-branch deploy checkout) and `config/` (0700 machine-local secrets) already-canon
per §2.

The posed recommendation included conforming the Mac, because its deviation is a live
bug, not mere asymmetry: the runtime checkout's only remote is the local CD bare path,
so `gh` run from the runtime resolves no repo and daily-build attribution silently
returned 0 rows. The sitting weighed it and exempted the Mac: the rider (this PR's
companion commit) fixed that one live symptom deterministically, no other `gh` consumer
runs from the runtime checkout (verified — pr-step/worktree-delete run from worktrees,
which carry GitHub remotes), collapsing the bare's duplicate `origin`+`github` remotes
risks breaking worktree pushes that name the `github` remote, and the box retires within
about a week. The Mac's deviation is recorded here as known and rider-mitigated;
corpse-polish only.

**Alternatives considered:** (a) conform the Mac anyway — rejected at the sitting per
the weighing above; (b) additive `git remote add github` on `live/` as insurance —
unnecessary once the rider landed and no other consumer exists.

**Consequences:** one documented git shape to verify on any surviving box; `gh --repo
TokenAmby-Code/Token-OS` stays PR truth everywhere (§2); tooling never infers the repo
from whichever checkout it happens to run in.

## R4 — Branch→session-doc linkage

**Decision needed:** keep the slug heuristic, or make the link first-class?

**Recommendation: registry-owned join, stamped at dispatch time.** The k12-era schema
carries a first-class `branch` column (or join table) on the session-doc link; dispatch
stamps it when it creates/links the doc and worktree — the one moment the mapping is
known exactly. Doc frontmatter gets a mirrored `branch:` field for human readability, but
**the DB row is truth**. `session_doc_resolver.py`'s slugify-and-LIKE heuristic
(`session_doc_resolver.py:29,70-76`) is replaced by an exact join; docs that never went
through dispatch get an honest `NULL` instead of a fuzzy match.

**Alternatives:** (a) keep the heuristic — rejected: it already needs a ported copy of
`slugify_branch` (dispatch:951) to stay bug-compatible, and near-miss slugs silently
attach the wrong doc to a build note; (b) frontmatter as truth, DB as cache — rejected:
frontmatter is hand-editable and unvalidated; the registry is the component that was
*present* at dispatch.

**Consequences:** daily-build/prod-report attribution becomes exact; off-dispatch docs
are visibly unlinked (a feature: it surfaces undispatched work); one more column in the
R5 schema, zero migration cost since R5 starts from zero.

## R5 — Schema era: migrate or rebuild?

**Decision needed:** does the k12 registry start as a migration of the Mac DB or from
zero?

**Recommendation: rebuild from zero for runtime state; one-shot import of
`session_documents` only.** Instances are ephemeral — every live row is re-created by the
next SessionStart, so carrying them buys nothing. Mutation/event history stays queryable
in the archived Mac DB snapshot (cold, read-only — consistent with the NAS
gold-and-backup ruling, docket #6); it does not contaminate the new era. Session-doc
links are the one durable asset (vault pointers survive any number of seat cycles) —
they import once, gaining the R4 `branch` column (`NULL` where unknowable). The seed
schema is `instances` v2 plus the `instance_mutation.py` sanctioned-write layer; the
deprecated `claude_instances` table (`db_schema.py:338-410`) dies with the era — **no
compatibility shim, fail loud** so any surviving reader identifies itself immediately.

**Alternatives:** (a) full migration — rejected: pays conversion cost to preserve rows
whose lifetime is one session; (b) rebuild with a `claude_instances` view for stragglers
— rejected: shims are how the vaporize lane's shadow paths were born.

**Consequences:** the k12 registry's first byte is k12-era; history questions go to the
archive snapshot by design; any tool still reading `claude_instances` breaks loudly at
cutover and gets fixed, not shimmed.

## R6 — Sanctioned writers — **OPEN, not ratified 2026-07-15**

At the sitting the Emperor held this ruling open: the DB write-door discussion warrants
follow-ups before closing. The recommendation below stands as posed; rung 2 (writers) is
gated on resolving it.

**Decision needed:** who may write the registry, through what door?

**Recommendation: the hub API is the write door; direct sqlite is transitional debt.**
Today's probe shows the debt precisely: k12-personal's DB is WAL-hot with *no API on the
box*, because `stop_hook.py:49,85` and `agent-session-end-resume.sh:82` write directly
through the canonical env chain. That is correct *for now* (the vaporize lane made the
chain safe) and named transitional. After the hub token-api service is running on
k12-personal (§6 step 6), hooks POST to the box-local API — the existing durable-retry
outbox already covers down-API windows — and any residual direct-sqlite writer goes
through the `instance_mutation` layer so the field allowlist/forbidden-field guard
applies to every write path, not just HTTP.

**Open sub-question, flagged honestly:** attribute the current WAL traffic on-box —
confirm the two known direct writers account for *all* of it before cutover, so no
unsanctioned writer rides into the new era unnoticed.

**Alternatives:** (a) keep direct sqlite as a permanent sanctioned path — rejected: two
write doors means the allowlist guard is advisory; (b) API-only immediately (before the
hub moves) — rejected: would route k12-local hook writes through the *Mac* over the
network for the remaining pre-cutover weeks, adding a WAN dependency to session teardown
for nothing.

**Consequences:** one guarded write path at steady state; hook writes become observable
API traffic; the outbox pattern already proven for comms covers the availability gap.

## Riding along in this PR

One already-ruled code rider ships with this doc: daily-build gh attribution silently
returned 0 rows when run from the runtime checkout, because its only remote is the local
CD bare and `gh` cannot resolve a repo (`git_activity.py` `merged_prs`/`open_prs`). The
fix resolves an explicit owner/name slug (`GH_REPO` env > remote-derived > the
`TokenAmby-Code/Token-OS` default that `cli.py:54-60`'s repo sentinel makes safe) and
passes it as `--repo` on every `gh` call, so attribution is deterministic on every box
regardless of checkout shape.

## Sequencing after ratification (rung ladder sketch)

- **Rung 1 — schema:** k12-era schema module (instances v2 + R4 branch column +
  session_documents import script), landing behind the existing init_db path; no consumer
  flips.
- **Rung 2 — writers (GATED on R6, still open):** hook writers gain the API-first path
  with sqlite fallback removed on the hub box; `instance_mutation` becomes the sole
  mutation surface; WAL attribution audit (R6 sub-question) closes.
- **Rung 3 — satellite live:** k12-personal token-api serves the new schema in satellite
  mode (§6 step 6), registration door + envelope echo verified end-to-end.
- **Rung 4 — cutover:** §6 step 7 as amended by R2 (import, flip
  `_IMPERIUM_TOKEN_API_HOST`, verify); `claude_instances` readers fail loud and get
  fixed.
- **Rung 5 — Mac registry retirement:** archive snapshot taken, Mac DB frozen read-only.

Each rung is its own PR lane with its own live proof. R1–R5 are ratified (2026-07-15
sitting); rungs gated only on them may start. Rung 2 waits for the R6 follow-ups.

## Follow-ons (not this lane)

- **Ultramar canonization ceremony:** R1/R3 (ratified) join the four device-doctrine
  canon notes (Hub-and-Envelope Estate Topology, etc.) as k12-era registry canon; R6
  follows once its follow-ups close.
- **R6 follow-up sitting:** the write-door ruling plus the WAL-attribution sub-question,
  held open by the Emperor at the 2026-07-15 sitting.
- **FleetView:** the hub-authoritative registry is the natural read-model source for a
  fleet dashboard; out of scope until rung 3 exists.
- **Wedged NAS read path** (ops, tracked separately) and the k12-work civic-side layout
  (designed in Pax-ENV per standing rule, target-structure §7).
