#!/usr/bin/env bash
# Behavioral-pin regression: dispatch task bytes stay off staged engine argv.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PROMPT="$TMP/frontmatter.md"
printf '%s' $'---\ntitle: x\n---' >"$PROMPT"

for engine in codex claude; do
  out="$TMP/$engine.out"
  TMPDIR="$TMP" DISPATCH_WORKTREE_DUP_CHECK=0 \
    "$ROOT/cli-tools/bin/dispatch" \
      --engine "$engine" \
      --dir "$ROOT" \
      --no-worktree \
      --prompt-file "$PROMPT" \
      --dry-run >"$out"

  final="$(sed -n '/^  final_command:/{n;p;}' "$out")"
  [[ "$final" == *"TOKEN_API_DISPATCH_PROMPT_FILE="* ]] || {
    echo "$engine final command did not carry a prompt-file reference" >&2
    exit 1
  }
  [[ "$final" != *"title: x"* && "$final" != *"---"* ]] || {
    echo "$engine final command leaked prompt bytes onto argv" >&2
    exit 1
  }
done

# Capture the real engine argument vector behind the wrapper. Engine binaries are
# fakes and curl is stubbed, so this never registers or launches a live agent.
mkdir -p "$TMP/bin" "$TMP/home"
cat >"$TMP/bin/curl" <<'STUB'
#!/usr/bin/env bash
printf '200'
STUB
cat >"$TMP/bin/fake-engine" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$ENGINE_ARGV_LOG"
STUB
chmod +x "$TMP/bin/curl" "$TMP/bin/fake-engine"

for engine in codex claude; do
  prompt="$TMP/wrapper-$engine-prompt.md"
  argv_log="$TMP/wrapper-$engine-argv"
  printf '%s' $'---\ntitle: x\n---' >"$prompt"
  engine_env=(CODEX_BIN="$TMP/bin/fake-engine")
  [[ "$engine" == "claude" ]] && engine_env=(CLAUDE_BIN="$TMP/bin/fake-engine")
  env \
    PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    HOME="$TMP/home" \
    TMUX_PANE= \
    TOKEN_API_URL=http://unused \
    TMUXCTLD_URL=http://unused \
    TOKEN_API_INTERNAL_DISPATCH=1 \
    TOKEN_API_TARGET_WORKING_DIR="$ROOT" \
    TOKEN_API_DISPATCH_PROMPT_FILE="$prompt" \
    ENGINE_ARGV_LOG="$argv_log" \
    "${engine_env[@]}" \
    "$ROOT/cli-tools/scripts/agent-wrapper.sh" "$engine" >/dev/null 2>&1

  [[ "$(cat "$argv_log")" == *"Read and follow the complete task brief in "* ]] || {
    echo "$engine engine argv omitted the prompt-file reference" >&2
    exit 1
  }
  if grep -q -- 'title: x\|^---$' "$argv_log"; then
    echo "$engine engine argv leaked prompt bytes" >&2
    exit 1
  fi
done

echo "dispatch prompt file delivery tests passed"
