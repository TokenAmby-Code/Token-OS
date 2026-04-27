---
name: pr
description: "Full PR lifecycle: commit, create PR, wait for reviews, address feedback, merge. Usage: /pr [action] [args]. Actions: create (default), review, merge, status."
user_invocable: true
---

# PR — Full Pull Request Lifecycle

Orchestrates the complete pull request workflow using three CLI tools: `pr-create`, `pr-review-loop`, and `pr-merge`.

## Usage

- `/pr` — commit + create PR + wait for reviews (most common)
- `/pr create` — same as above
- `/pr create --no-wait` — create PR without waiting for reviews
- `/pr review` — push fixes + request re-review + wait for results
- `/pr review --message "Fixed auth bug"` — re-review with description of fixes
- `/pr merge` — merge the current PR + full cleanup
- `/pr merge -y` — merge without confirmation
- `/pr status` — show current PR status and review comments

## Workflow

### `/pr` or `/pr create` (Default)

This is the standard flow for getting code reviewed:

1. **Prepare the commit** (if there are uncommitted changes):
   - Run `git status` and `git diff` to see what's changed
   - Stage relevant files and create a commit with a descriptive message
   - Follow the repo's commit format: `type: description`
   - Include `Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>`

2. **Push and create the PR:**
   ```bash
   # Ensure branch is pushed
   git push -u origin HEAD

   # Create PR with review polling
   pr-create --title "<type>: <description>" --body "$(cat <<'EOF'
   ## Summary
   <bullet points>

   ## Test plan
   - [ ] Unit tests pass
   - [ ] Manual verification

   Generated with [Claude Code](https://claude.com/claude-code)
   EOF
   )"
   ```

3. **Wait for CodeRabbit + GitHub Actions:**
   - `pr-create` polls for up to 8 minutes (CodeRabbit assertive on large diffs runs 1–5 min; 8 min gives headroom)
   - Stop signals (any one ends the wait):
     - A new comment from `coderabbitai[bot]` whose body matches `^## Summary by CodeRabbit`
     - The `coderabbit` commit status reaches `success` or `failure` on the PR head
     - A `coderabbitai[bot]` PR review with state `APPROVED` or `CHANGES_REQUESTED`
   - When reviews arrive, output the summary + any inline comments

4. **Address feedback in a loop (if `CHANGES_REQUESTED`):**
   - Read the inline comments and the summary
   - Fix the issues in the worktree
   - Run `/pr review --message "..."` to push + re-poll
   - Loop until review is `APPROVED` or no blockers remain

5. **Merge when clean:**
   - All required checks green (PR Gate workflow + `coderabbit` status)
   - No open `CHANGES_REQUESTED` reviews
   - Run `/pr merge -y` (or `/pr merge` to confirm first)

### `/pr review`

For iterating on review feedback after fixing issues:

```bash
# Push fixes and request re-review
pr-review-loop [PR_NUMBER] [--message "description of fixes"]
```

The tool will:
1. Push the current branch
2. Post a comment requesting re-review from @coderabbitai (`@coderabbitai full review`)
3. Poll for new review comments + commit status (distinguishes new vs baseline)
4. Output only the NEW comments from this review cycle and the current review state

Options:
- `--message "..."` — describe what was fixed (included in re-review request)
- `--no-push` — skip the git push (if already pushed)
- `--read` — just poll for existing comments, don't push or request re-review
- `--timeout <mins>` — override the 8-minute default

### `/pr merge`

After reviews are clean and PR is approved:

```bash
pr-merge [PR_NUMBER] [--squash|--merge|--rebase] [-y]
```

The tool will:
1. Verify PR is open and mergeable
2. Show merge plan (branch, method, cleanup targets)
3. Execute squash merge (default)
4. Clean up: delete remote branch, local branch, worktree (if applicable)
5. Pull latest main

Options:
- `--squash` (default), `--merge`, `--rebase` — merge method
- `-y` — skip confirmation prompt
- `--no-cleanup` — merge but skip branch/worktree deletion
- `--dry-run` — preview what would happen

### `/pr status`

Check current PR state without making changes:

```bash
# Get PR number for current branch
PR_NUM=$(gh pr view --json number -q '.number' 2>/dev/null)

if [ -n "$PR_NUM" ]; then
    # Show PR info
    gh pr view "$PR_NUM"

    # Show review comments
    pr-review-loop "$PR_NUM" --read
else
    echo "No PR found for current branch"
fi
```

## Pre-Flight Checks

Before creating a PR, verify:

1. **Not on main/master** — never create PRs from the default branch
2. **Local checks pass** — run `test` (the unified CI-mirror runner) so you catch
   format/lint/type/test failures before pushing. CI runs the same commands;
   "passes locally" means the same thing as "passes CI."
3. **No secrets staged** — check for `.env`, credentials, API keys in `git diff --cached`

## What the PR Gate Enforces

Branch protection on `main` requires:

- The `PR Gate (blocking) / quality` workflow run to be green (format, lint,
  typecheck, tests — see `.github/workflows/pr.yml`)
- The `coderabbit` commit status to be `success`
- Zero open `CHANGES_REQUESTED` reviews

The push-side workflow (`.github/workflows/push.yml`) is **advisory only** —
it surfaces issues as annotations on every push to a feature branch, but does
not block. PRs are for shipping, not for fishing for an AI review.

## GitHub Authentication

The repo uses GITHUB_TOKEN for auth:
```bash
source michael/00_configuration/.env.local
```
This is usually already in the environment. If `gh` commands fail with auth errors, source it.

## Common Patterns

**Quick fix PR (most common):**
```
/pr
```
Commits, creates PR, waits for reviews, reports results.

**Fix review comments and re-submit:**
```
# ... make fixes ...
/pr review --message "Fixed the null check and added test"
```

**Merge after approval:**
```
/pr merge -y
```

**Full cycle in one session:**
```
/pr create          # create + wait for reviews
# ... fix issues found by reviewers ...
/pr review          # push fixes + wait for new reviews
/pr merge -y        # merge when clean
```

## Error Handling

- If `pr-create` fails: check `gh auth status` and branch state
- If reviews time out: use `/pr status` to check manually, or `pr-review-loop --read`
- If merge fails: check for merge conflicts, required status checks, or branch protection rules
- If merge conflicts exist: resolve locally, commit, then `/pr review` before merging
- If CodeRabbit never posts a summary: verify the CodeRabbit GitHub App is
  installed for the repo and `CODERABBIT_API_KEY` is set in repo Actions
  secrets (see vault: `Terra/Meta/coderabbit-template.yaml`)
