#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
HOME=/data/data/com.termux/files/home
printf 'true\n' > "$HOME/.dex-active"
"$HOME/.local/bin/taskbar-profile" dex
exec "$HOME/.termux/tasker/tx-reconnect-reconcile"
