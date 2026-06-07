# Discord Daemon Source
Deployed to: ~/.discord-cli/node/
After editing: run 'discord-daemon restart'

CD: merging a change under `discord-daemon/` to `main` auto-deploys it — the CD
webhook runs the git-aware `token-restart`, which ff-pulls the live checkout and
restarts only the Discord daemon (`launchctl kickstart` of `ai.tokenclaw.discord`).
