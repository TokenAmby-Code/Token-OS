# Task: Sand the `dispatch` CLI flow to a mirror coat

You are a legion (astartes) worker. Stay in your isolated worktree; never touch `main` directly. Deliver a PR.

## Target
The `dispatch` CLI lives at `cli-tools/bin/dispatch` (it may source helpers under `cli-tools/lib/`). It is the canonical launcher for new/resumed agent sessions. The common intent — "spawn a legion worker on a scoped fix in its own worktree, one-shot" — currently takes too many flags and trips a real bug. Fix that. **Preserve all existing flags and behavior (backward compatible); add tests.**

## MUST-FIX (highest leverage)

### 1. Locale bug — `sed: RE error: illegal byte sequence`
Repro: run `dispatch` with a `--prompt-file` whose contents contain non-ASCII (e.g. `→`, `✓`) and `--dry-run`. The internal `sed` (used when printing/handling the prompt or building `dispatch_command`) errors `sed: RE error: illegal byte sequence` under the default macOS/C locale. The user currently has to manually `export LC_ALL=en_US.UTF-8` to work around it.
**Fix:** harden the script's locale internally (e.g. set a UTF-8 `LC_ALL`/`LC_CTYPE` for the script, or stop running `sed`/byte-sensitive tools over arbitrary prompt content — prefer not piping prompt text through `sed` at all). A user must NEVER need to set a locale env var. Add a regression test that dispatches (dry-run) a unicode prompt under `LC_ALL=C` and asserts no `illegal byte sequence`.

### 2. `--worktree` should imply `one_off` lane by default
Currently `dispatch --worktree X` defaults to `instance_type=golden_throne` (persistent keepalive), so a scoped fix leaves a lingering GT instance — the user must add `--no-gt --instance-type one_off` to get the obviously-correct bounded worker.
**Fix:** when `--worktree` is passed AND the user gave no explicit lane flag (`--gt`/`--no-gt`/`--instance-type`/`--instance-type golden_throne`), default to `--no-gt` + `instance_type=one_off`. Explicit overrides must still win (`dispatch --worktree X --gt` must still yield golden_throne). Document the new default in `--help`.

### 3. `legion` / `mechanicus` / `civic` target shorthand
`--target legion:new` is a magic string. Add positional/short forms so the common path is terse:
- `dispatch legion <...flags>` ≡ `--target legion:new`
- (same for `mechanicus`, `civic` → their `:new` targets)
Keep `--target` working. The goal invocation should be: `dispatch legion --worktree <branch> --repo token-os --prompt-file <file>`.

## NICE-TO-HAVE (do if clean)

### 4. `--repo <name>` for worktree repo inference
Today `--worktree` needs `--dir <path-inside-repo>` so it knows which repo/worktree-config to branch from. Add `--repo <name>` (resolves the worktree config in `~/.config/worktrees/<name>.conf`, e.g. `token-os`) so the user need not pass a full `--dir`. If neither `--repo` nor `--dir` given and CWD is inside a known project, infer it.

### 5. Derive metadata
When `--title`/`--objective` are omitted but `--worktree` and a prompt are present, derive `--title` from the branch name and a short objective from the prompt's first heading/line. Don't override explicit values.

## Verify
After changes, these must all succeed with NO manual locale env and NO extra lane flags:
```
dispatch legion --worktree test-mirror-coat --repo token-os --prompt-file <unicode-file> --dry-run
# expect: persona=astartes, instance_type=one_off, worktree resolved, no sed error
```
Run existing dispatch/cli-tools tests. Add tests for #1 and #2 at minimum.

## Deliverable
PR onto clean `main`, title: `feat(dispatch): mirror-coat the common path (locale fix, worktree⇒one_off, legion shorthand)`. Body: list each fix with before/after invocation, and confirm backward compatibility + tests. Report the PR number.
