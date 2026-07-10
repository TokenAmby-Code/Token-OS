#!/usr/bin/env bash
# CodeRabbit PR gate wrapper.
#
# Responsibilities:
#   1. Fail closed if CodeRabbit is absent, still pending after timeout, failed, or
#      reports a disabled/free-trial/credit-skipped review.
#   2. When CodeRabbit has succeeded on the current head SHA and the latest
#      CodeRabbit review for that SHA is non-blocking, dismiss stale CodeRabbit
#      CHANGES_REQUESTED review objects left on older commits. This handles the
#      observed bot failure mode where the commit status is current and green
#      but GitHub reviewDecision remains CHANGES_REQUESTED from an already-
#      addressed assessment.

set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN is required}"
: "${REPO:?REPO is required, e.g. owner/name}"
: "${PR_NUMBER:?PR_NUMBER is required}"
: "${SHA:?SHA is required}"

TIMEOUT_SECONDS="${CODERABBIT_GATE_TIMEOUT_SECONDS:-1800}"
POLL_INTERVAL_SECONDS="${CODERABBIT_GATE_POLL_INTERVAL_SECONDS:-15}"
CODERABBIT_LOGIN="${CODERABBIT_LOGIN:-coderabbitai[bot]}"

is_disabled_or_skipped_review() {
  local description_lc
  description_lc="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"

  [[ "$description_lc" == *"review skipped: free tier disabled"* ]] \
    || [[ "$description_lc" == *"free tier disabled"* ]] \
    || [[ "$description_lc" == *"free trial"* && "$description_lc" == *"expired"* ]] \
    || [[ "$description_lc" == *"trial"* && "$description_lc" == *"expired"* ]] \
    || [[ "$description_lc" == *"no credits"* ]] \
    || [[ "$description_lc" == *"insufficient credits"* ]]
}

evaluate_coderabbit_signal() {
  local description="$1" state="$2" kind="$3"

  if is_disabled_or_skipped_review "$description"; then
    echo "::error::CodeRabbit reported a skipped/disabled review ('$description'). Failing PR Gate so a disabled/free-trial/credit-skipped review cannot merge silently."
    exit 1
  fi

  case "$state" in
    success|neutral)
      return 0
      ;;
    pending|queued|in_progress|"")
      return 2
      ;;
    skipped)
      echo "::error::CodeRabbit ${kind} was skipped: $description"
      exit 1
      ;;
    *)
      echo "::error::CodeRabbit ${kind} is $state: $description"
      exit 1
      ;;
  esac
}

wait_for_coderabbit_status() {
  local deadline line context state description updated_at check_line check_name check_status check_conclusion check_description check_url status_passed
  deadline=$((SECONDS + TIMEOUT_SECONDS))

  while true; do
    status_passed=false
    if ! line="$(gh api --paginate --method GET "repos/$REPO/commits/$SHA/status" \
      --jq '[.statuses[]? | select((((.context // "") | ascii_downcase) | startswith("coderabbit")))] | sort_by(.updated_at // .created_at) | if length == 0 then empty else last | [.context, .state, (.description // ""), (.updated_at // .created_at // "")] | @tsv end')"; then
      echo "::error::Failed to query CodeRabbit commit status for $SHA. Check GH_TOKEN, REPO, SHA, and statuses: read."
      exit 1
    fi

    if [[ -n "$line" ]]; then
      IFS=$'\t' read -r context state description updated_at <<< "$line"
      echo "CodeRabbit status: context=$context state=$state updated_at=$updated_at description=$description"

      if evaluate_coderabbit_signal "$description" "$state" "status"; then
        status_passed=true
      fi
    fi

    if ! check_line="$(gh api --paginate --method GET "repos/$REPO/commits/$SHA/check-runs" \
      --jq '[.check_runs[]? | select(((((.name // "") | ascii_downcase) | startswith("coderabbit")) or ((((.app.slug // "") | ascii_downcase) == "coderabbitai"))))] | sort_by(.completed_at // .started_at // .created_at // "") | if length == 0 then empty else last | [.name, .status, (.conclusion // ""), ((.output.summary // "") as $s | if ($s | length) > 0 then $s else (.output.title // "") end), (.details_url // "")] | @tsv end')"; then
      echo "::error::Failed to query CodeRabbit check runs for $SHA. Check GH_TOKEN, REPO, SHA, and checks: read."
      exit 1
    fi

    if [[ -n "$check_line" ]]; then
      IFS=$'\t' read -r check_name check_status check_conclusion check_description check_url <<< "$check_line"
      echo "CodeRabbit check run: name=$check_name status=$check_status conclusion=$check_conclusion url=$check_url"

      if [[ "$check_status" != "completed" ]]; then
        :
      else
        evaluate_coderabbit_signal "$check_description" "$check_conclusion" "check run" && return 0
      fi
    elif [[ "$status_passed" == "true" ]]; then
      return 0
    fi

    if [[ "$SECONDS" -ge "$deadline" ]]; then
      echo "::error::Timed out waiting for CodeRabbit commit status/check run on $SHA. Failing closed so skipped/disabled/missing reviews cannot merge silently."
      exit 1
    fi

    echo "Waiting for CodeRabbit commit status/check run on $SHA..."
    sleep "$POLL_INTERVAL_SECONDS"
  done
}

assert_latest_current_head_review_non_blocking() {
  local latest_review id state submitted_at url
  latest_review="$(gh api --paginate "repos/$REPO/pulls/$PR_NUMBER/reviews" \
    --jq '[.[] | select(.user.login == "'"$CODERABBIT_LOGIN"'" and .commit_id == "'"$SHA"'")] | sort_by(.submitted_at // "") | if length == 0 then empty else last | [.id, .state, .submitted_at, (.html_url // "")] | @tsv end')"

  if [[ -z "$latest_review" ]]; then
    echo "No CodeRabbit review object exists on current head; relying on successful current-head CodeRabbit status."
    return 0
  fi

  IFS=$'\t' read -r id state submitted_at url <<< "$latest_review"
  echo "Latest CodeRabbit review for current head: id=$id state=$state submitted_at=$submitted_at url=$url"

  if [[ "$state" == "CHANGES_REQUESTED" ]]; then
    echo "::error::CodeRabbit's latest review on current head ($SHA) requested changes. Address the current review; refusing to dismiss it."
    exit 1
  fi
}

dismiss_stale_changes_requested_reviews() {
  local stale_reviews failed id commit_id submitted_at url message short_sha
  stale_reviews="$(gh api --paginate "repos/$REPO/pulls/$PR_NUMBER/reviews" \
    --jq '.[] | select(.user.login == "'"$CODERABBIT_LOGIN"'" and .state == "CHANGES_REQUESTED" and .commit_id != "'"$SHA"'") | [.id, .commit_id, .submitted_at, (.html_url // "")] | @tsv')"

  if [[ -z "$stale_reviews" ]]; then
    echo "No stale CodeRabbit CHANGES_REQUESTED reviews to dismiss."
    return 0
  fi

  failed=0
  while IFS=$'\t' read -r id commit_id submitted_at url; do
    [[ -n "$id" ]] || continue
    short_sha="${commit_id:0:12}"
    message="Dismiss stale CodeRabbit CHANGES_REQUESTED from $short_sha: CodeRabbit commit status is success on current head ${SHA:0:12}, and the latest CodeRabbit review on current head is non-blocking."
    echo "Dismissing stale CodeRabbit review id=$id commit=$commit_id submitted_at=$submitted_at url=$url"
    if ! gh api --method PUT "repos/$REPO/pulls/$PR_NUMBER/reviews/$id/dismissals" -f "message=$message" >/dev/null; then
      echo "::error::Failed to dismiss stale CodeRabbit review id=$id. Grant this workflow pull-requests: write or dismiss the stale review manually."
      failed=1
    fi
  done <<< "$stale_reviews"

  if [[ "$failed" -ne 0 ]]; then
    exit 1
  fi
}

wait_for_coderabbit_status
assert_latest_current_head_review_non_blocking
dismiss_stale_changes_requested_reviews
