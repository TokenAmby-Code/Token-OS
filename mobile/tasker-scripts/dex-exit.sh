#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
HOME=/data/data/com.termux/files/home
rm -f "$HOME/.dex-active"
"$HOME/.local/bin/taskbar-profile" mobile-nav
exec "$HOME/.termux/tasker/tx-reconnect-reconcile"
