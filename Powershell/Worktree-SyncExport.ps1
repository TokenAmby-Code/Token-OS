# Worktree-SyncExport.ps1 - Export all worktrees to NAS staging
# Called by Task Scheduler at logoff (before WSL terminates)
# NAS is still accessible at this point

$ErrorActionPreference = "SilentlyContinue"

# Run export via WSL
wsl.exe -d Ubuntu -e bash -lc "worktree-sync export --force 2>&1 | logger -t worktree-sync"

exit 0
