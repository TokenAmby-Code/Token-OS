#!/usr/bin/env bash
set -u

REPO="${REPO:-}"
SHA="${SHA:-}"

status_line=""
check_line=""
if [[ -n "$REPO" && -n "$SHA" ]]; then
  status_line="$(gh api --paginate --method GET "repos/$REPO/commits/$SHA/status" \
    --jq '[.statuses[]? | select((((.context // "") | ascii_downcase) | startswith("coderabbit")))] | sort_by(.updated_at // .created_at) | if length == 0 then empty else last | [.state, (.description // "")] | @tsv end' 2>/dev/null || true)"
  check_line="$(gh api --paginate --method GET "repos/$REPO/commits/$SHA/check-runs" \
    --jq '[.check_runs[]? | select((((.name // "") | ascii_downcase) | startswith("coderabbit")) or (((.app.slug // "") | ascii_downcase) == "coderabbitai"))] | sort_by(.completed_at // .started_at // .created_at // "") | if length == 0 then empty else last | [if .status == "completed" then (.conclusion // "completed") else (.status // "pending") end, ((.output.summary // "") as $s | if ($s | length) > 0 then $s else (.output.title // "") end)] | @tsv end' 2>/dev/null || true)"
fi

if [[ -n "$check_line" ]]; then
  IFS=$'\t' read -r state description <<<"$check_line"
elif [[ -n "$status_line" ]]; then
  IFS=$'\t' read -r state description <<<"$status_line"
else
  state="absent"
  description="No current CodeRabbit status or check run was found."
fi

description="${description//'%'/'%25'}"
description="${description//$'\r'/'%0D'}"
description="${description//$'\n'/'%0A'}"
echo "::notice title=CodeRabbit advisory::state=${state:-unknown}; ${description:-No description provided.}"
exit 0
