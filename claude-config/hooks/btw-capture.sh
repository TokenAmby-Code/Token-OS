#!/usr/bin/env bash
# The unsafe UserPromptSubmit reprompt worker was killed by decree. This file is
# retained only so already-installed hook trees fail loudly during convergence.
set -euo pipefail

mkdir -p "${HOME}/.claude/logs"
printf '[%s] ephemeral channel disabled by decree\n' "$(date '+%Y-%m-%d %H:%M:%S')" \
    >> "${HOME}/.claude/logs/btw-capture.log"
printf '%s\n' 'btw-capture: ephemeral channel disabled by decree' >&2
exit 1
