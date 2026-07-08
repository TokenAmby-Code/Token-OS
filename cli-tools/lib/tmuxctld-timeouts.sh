# Shared tmuxctld long-hold ceilings and transport budgets.
: "${TMUXCTLD_SEND_HOLD_CEILING:=60}"
: "${TMUXCTLD_LIFECYCLE_HOLD_CEILING:=60}"
: "${TMUXCTLD_CLIENT_TIMEOUT_MARGIN:=15}"
tmuxctld_send_client_timeout() { awk -v c="$TMUXCTLD_SEND_HOLD_CEILING" -v m="$TMUXCTLD_CLIENT_TIMEOUT_MARGIN" 'BEGIN { printf "%g", c + m }'; }
tmuxctld_lifecycle_client_timeout() { awk -v c="$TMUXCTLD_LIFECYCLE_HOLD_CEILING" -v m="$TMUXCTLD_CLIENT_TIMEOUT_MARGIN" 'BEGIN { printf "%g", c + m }'; }
