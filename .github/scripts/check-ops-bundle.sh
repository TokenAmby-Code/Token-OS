#!/usr/bin/env bash
set -euo pipefail

# Ensure the committed /ui/ops Vite bundle is regenerated whenever the cockpit
# source or committed bundle changes. Token-API serves token-api/ui/ops directly
# at runtime; CI cannot leave that build stale.
base_ref="${1:-}"
if [ -z "$base_ref" ]; then
  if git rev-parse --verify origin/main >/dev/null 2>&1; then
    base_ref="$(git merge-base origin/main HEAD)"
  else
    base_ref="HEAD~1"
  fi
fi

changed="$(
  {
    git diff --name-only "$base_ref"...HEAD -- token-api/web/ops token-api/ui/ops || true
    git diff --name-only -- token-api/web/ops token-api/ui/ops || true
    git ls-files --others --exclude-standard -- token-api/web/ops token-api/ui/ops || true
  } | sort -u
)"
if [ -z "$changed" ]; then
  echo "Ops cockpit files unchanged; skipping bundle check."
  exit 0
fi

printf 'Ops cockpit files changed; verifying committed bundle is current:\n%s\n' "$changed"

before="$(mktemp -d)"
trap 'rm -rf "$before"' EXIT
cp -R token-api/ui/ops/. "$before/"

(
  cd token-api/web/ops
  npm ci
  npm run build
)

if ! diff -qr "$before" token-api/ui/ops >/tmp/ops-bundle.diff; then
  echo "::error::token-api/ui/ops is stale. Run 'cd token-api/web/ops && npm run build' and commit the generated bundle."
  cat /tmp/ops-bundle.diff
  git status --short -- token-api/ui/ops
  git diff --stat -- token-api/ui/ops
  exit 1
fi

echo "Ops cockpit committed bundle is current."
