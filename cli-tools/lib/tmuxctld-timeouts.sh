# Shared tmuxctld long-hold ceilings and transport budgets.
: "${TMUXCTLD_SEND_HOLD_CEILING:=60}"
: "${TMUXCTLD_CLIENT_TIMEOUT_MARGIN:=15}"
# Emperor decree (no-timeout-under-5min): floor the client send budget at 300s.
# Parity with cli-tools/lib/tmuxctld_timeouts.py; max() keeps the budget strictly
# above the daemon hold ceiling even if the ceiling ever rises past the floor.
: "${TMUXCTLD_DECREE_MIN_COMMS_TIMEOUT:=300}"
tmuxctld_send_client_timeout() { awk -v c="$TMUXCTLD_SEND_HOLD_CEILING" -v m="$TMUXCTLD_CLIENT_TIMEOUT_MARGIN" -v f="$TMUXCTLD_DECREE_MIN_COMMS_TIMEOUT" 'BEGIN { d = c + m; printf "%g", (d > f ? d : f) }'; }
