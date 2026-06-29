---
name: pr
description: "Canonical PR lifecycle. Usage: /pr or /pr step runs pr-step: commit, create/update PR, summarize CodeRabbit/checks, and auto-merge when green."
---

# PR — One-Step Pull Request Lifecycle

Use the unified PR lifecycle tool. The normal command is always:

```bash
pr-step
```

`pr-step` is the only agent-facing PR lifecycle command. It owns commit, push,
PR creation/update, CodeRabbit/check summarization, and merge/cleanup.

1. Detects whether the current branch/worktree already has a PR.
2. If no PR exists: stages and commits pending changes, pushes the branch, creates the PR, and summarizes review/check state.
3. If a PR exists: stages and commits pending changes, pushes, skips re-review when the current head is already green, otherwise requests CodeRabbit review.
4. Prints a concise PR summary: URL, commit/push status, checks, CodeRabbit state, and actionable findings.
5. If green, merges and performs cleanup automatically unless `--no-merge` is set.

## Usage

- `/pr` or `/pr step` — run `pr-step`.
- `/pr step --message "fix: address review"` — use a specific commit/re-review message when the wording matters.
- `/pr step --no-merge` — deliberate review-only or dogfood run; do not auto-merge even if green.
- `/pr step --show-raw-review` — include full CodeRabbit/GitHub output only when the concise summary is ambiguous or insufficient.

## Agent Rules

- Do not call `pr-create`, `pr-review-loop`, or `pr-merge` directly. They are deprecated shims.
- Do not manually spam CodeRabbit re-review requests. Let `pr-step` decide whether re-review is needed.
- Do not run PR flow from `main`, `master`, `prod`, a detached `HEAD`, or a remote that cannot support PRs.
- Trust the concise `pr-step` summary first: PR URL, commit/push status, checks, CodeRabbit state, and actionable findings.
- Inspect raw CodeRabbit/GitHub output only when the concise summary is ambiguous, contradictory, or reports failure.
- If `pr-step` says the current head is already green, do not force another review.

## Emergency Escape Hatches

Use forced modes only for jammed states or manual recovery. They are not normal workflow.

- `pr-step --force create ...`
- `pr-step --force review ...`
- `pr-step --force merge ...`

These map to the old create, review, and merge phases for recovery/debugging. Prefer plain `pr-step`.

## Notes

- `pr-step` marks the instance `reviewing` through Token-API while a PR review is active, and `victorious` after merge.
- CodeRabbit polling handles GitHub API rate-limit sleeps and avoids unnecessary re-review requests when the current head is already green.
- Raw CodeRabbit/GitHub output is opt-in with `--show-raw-review`.
