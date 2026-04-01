#!/bin/bash
#
# deploy-executor-fleet.sh
# Deploys the executor fleet by adding cron jobs from the manifest
#

set -e

MANIFEST="/Users/tokenclaw/.openclaw/workspace/memory/data/executor_fleet_manifest.json"
PROMPTS_DIR="/Users/tokenclaw/.openclaw/workspace/memory/data/prompts"

echo "🚀 Deploying Executor Fleet..."
echo ""

# Stop gateway (required per Kaxo guide)
echo "⏸️  Stopping gateway..."
openclaw gateway stop
sleep 2

# Read executors from manifest
EXECUTORS=$(jq -r '.executors[] | @base64' "$MANIFEST")

for executor_b64 in $EXECUTORS; do
  executor=$(echo "$executor_b64" | base64 -d)

  name=$(echo "$executor" | jq -r '.name')
  schedule=$(echo "$executor" | jq -r '.schedule')
  model=$(echo "$executor" | jq -r '.model')
  prompt_template=$(echo "$executor" | jq -r '.prompt_template')

  echo "📦 Adding executor: $name"
  echo "   Schedule: $schedule"
  echo "   Model: $model"

  # Read prompt from template file
  prompt_file="$PROMPTS_DIR/$prompt_template"

  if [ ! -f "$prompt_file" ]; then
    echo "   ❌ ERROR: Prompt template not found: $prompt_file"
    continue
  fi

  prompt=$(cat "$prompt_file")

  # Add cron job
  openclaw cron add \
    --name "$name" \
    --agent isolated \
    --message "$prompt" \
    --cron "$schedule" \
    --session isolated

  echo "   ✅ Added"
  echo ""
done

# Start gateway
echo "▶️  Starting gateway..."
openclaw gateway start
sleep 2

# Verify
echo ""
echo "✅ Deployment complete!"
echo ""
echo "Cron jobs added:"
openclaw cron list | grep -E "code-writer|file-operator|validator|researcher|obsidian-improver|discord-improver"

echo ""
echo "📊 Budget allocation:"
jq -r '.executors | to_entries[] | "\(.key): \(.value.allocated)p"' "$MANIFEST"

echo ""
echo "🔍 Monitor logs:"
echo "  - Task logs: /Users/tokenclaw/.openclaw/workspace/memory/tasks/logs/"
echo "  - System logs: /Users/tokenclaw/.openclaw/workspace/memory/logs/"
echo "  - Budget: /Users/tokenclaw/.openclaw/workspace/memory/tasks/budget.json"

echo ""
echo "🎉 Executor fleet is live!"
