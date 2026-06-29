---
name: pr
description: "Canonical PR lifecycle. Usage: /pr or /pr step runs pr-step: commit, create/update PR, summarize CodeRabbit/checks, and auto-merge when green."
user_invocable: true
---

# PR — One-Step Pull Request Lifecycle

Default to the unified tool:

```bash
pr-step
```

`pr-step` is the agent-facing PR lifecycle command:

1. Detects whether the current branch/worktree already has a PR.
2. If no PR exists: stages and commits pending changes, pushes the branch, creates the PR, and summarizes review/check state.
3. If a PR exists: stages and commits pending changes, pushes, skips re-review when the current head is already green, otherwise requests CodeRabbit review.
4. Prints a concise PR summary: URL, commit/push status, checks, CodeRabbit state, and actionable findings.
5. If green, merges and performs cleanup automatically unless `--no-merge` is set.

## Usage

- `/pr` or `/pr step` — run `pr-step`.
- `/pr step --message "fix: address review"` — use a specific commit/re-review message.
- `/pr step --no-merge` — update/review only; do not auto-merge even if green.
- `/pr step --show-raw-review` — include full CodeRabbit/GitHub output when the concise summary is insufficient.

## Emergency Escape Hatches

Use forced modes only for jammed states or manual control:

- `pr-step --force create ...`
- `pr-step --force review ...`
- `pr-step --force merge ...`

## Notes

- `pr-step` marks the instance `reviewing` through Token-API while a PR review is active, and `victorious` after merge.
- CodeRabbit polling handles GitHub API rate-limit sleeps and avoids unnecessary re-review requests when the current head is already green.
- Raw CodeRabbit/GitHub output is opt-in with `--show-raw-review`.
- Do not create PRs from `main`, `master`, or `prod`.
