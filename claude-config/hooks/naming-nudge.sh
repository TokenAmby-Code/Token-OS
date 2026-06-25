#!/bin/bash
set -euo pipefail
# naming-nudge.sh - no-op compatibility stub.
#
# Naming is Token-API-owned: normal delivery happens after the first real
# UserPromptSubmit, with the live-pane reconciler as a backstop. This file remains
# only so stale Stop-hook configs and manual installs exit successfully without
# sending post-exit rename prompts.

cat >/dev/null 2>&1 || true
exit 0
