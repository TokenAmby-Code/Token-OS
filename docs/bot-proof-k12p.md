# Bot proof: k12-personal

2026-07-12 — Phase-2 bot-PR proof executed from k12-personal as TokenAmby-Fleet. This file is the toy diff; the workflow it proves:

1. **Identity**: global git identity set on the box (`TokenAmby-Fleet` / `304255886+TokenAmby-Fleet@users.noreply.github.com`), `gh auth setup-git` wired so git HTTPS rides the gh credential helper + PAT. Verified via `git config --global --list` and `gh auth status`.
2. **Clone**: fresh shallow clone of Token-OS over HTTPS+PAT into `~/scratch/toy-pr-proof` (read-transport proof; live checkout untouched).
3. **Commit**: authored by the bot, carrying the ruled trailer `Co-authored-by: Colby Lanier <colbymlanier@gmail.com>` (Emperor contribution-graph credit, ruled 2026-07-12). Verified via `git log --format=%B`.
4. **Push**: HTTPS+PAT push of `bot-proof/toy-pr-k12p` (write-transport proof — the ruled bot push lane; no ssh deploy-key push).
5. **PR + gates**: PR created with `-R TokenAmby-Code/Token-OS`; merge gated on head-SHA workflow run conclusions via `gh run view/list --json conclusion` (never `gh pr checks`), CodeRabbit findings resolved. Gates are advisory (no branch protection at this plan tier) — verify-green-then-merge is discipline, not platform guarantee.
6. **Merge**: squash merge (repo convention), confirmed with `gh pr view --json state,mergeCommit` (state=MERGED), remote branch deleted.
7. **CD verification**: merge to main auto-deploys the mac live runtime; verified by polling `/health` until `git_sha` advances to the merge commit.
