---
name: pr
description: "One-step PR lifecycle. Usage: /pr or /pr step runs pr-step: commit, create/update PR, wait for CodeRabbit, auto-merge when green. Legacy actions: create, review, merge, status."
user_invocable: true
---

# PR — One-Step Pull Request Lifecycle

Default to the unified tool:

```bash
pr-step
```

`pr-step` is the smooth path for agents:

1. Detects whether the current branch/worktree already has a PR.
2. If no PR exists: stages and commits pending changes, pushes the branch, creates the PR, and returns the CodeRabbit review.
3. If a PR exists: stages and commits pending changes, pushes, requests a deterministic CodeRabbit full re-review, and returns the current-head review.
4. At the end of every step, checks whether CodeRabbit and required checks are green.
5. If green, auto-runs `pr-merge -y` and performs cleanup.

## Usage

- `/pr` or `/pr step` — run `pr-step`.
- `/pr step --message "fix: address review"` — use a specific commit/re-review message.
- `/pr step --no-merge` — update/review only; do not auto-merge even if green.

## Legacy Escape Hatches

Use these only when you need manual control:

- `pr-create --title "..." --body "..."`
- `pr-review-loop [PR_NUMBER] --message "..."`
- `pr-merge [PR_NUMBER] -y`

`pr-create` and `pr-merge` now also arm a follow-up `/plan` injection for the pane when Token-API/tmux context is available.

## Notes

- `pr-step` marks the instance `reviewing` through Token-API while a PR review is active, and `victorious` after auto-merge.
- CodeRabbit polling handles GitHub API rate-limit sleeps and avoids spamming `@coderabbitai full review`; callers should not add their own retry loops.
- Do not create PRs from `main`, `master`, or `prod`.
