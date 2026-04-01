#!/bin/bash
# Deployment wrapper script for autonomous deployment
# This script is launched in a new terminal window by trigger-deploy.js

# Source bash profile FIRST to get gcloud and other tools in PATH
if [ -f ~/.bashrc ]; then
    source ~/.bashrc
fi
if [ -f ~/.bash_profile ]; then
    source ~/.bash_profile
fi

# Add common tool paths that might not be in non-interactive shell PATH
# These are typical locations for gcloud, docker, and other deployment tools
export PATH="/snap/bin:$HOME/.local/bin:$HOME/google-cloud-sdk/bin:$PATH"

set -e  # Exit on error

ENVIRONMENT=${1:-development}
FLAG=${2:-}
PROJECT_DIR=${3:-$(pwd)}
DEPLOY_DIR="/mnt/imperium/Scripts/cli-tools/src/deploy"
LOG_FILE="$PROJECT_DIR/.claude-deploy.log"
SIGNAL_FILE="$PROJECT_DIR/.claude-deploy-signal"
STATE_FILE="$PROJECT_DIR/.claude-local-server-state.json"

# Check if this is a local deployment
IS_LOCAL_DEPLOYMENT=false
if [ "$FLAG" = "-l" ] || [ "$FLAG" = "-d" ]; then
    IS_LOCAL_DEPLOYMENT=true
fi

# Change to project directory
cd "$PROJECT_DIR"
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
fi

# Highly visible ASCII banner
echo ""
echo "  ╔═══════════════════════════════════════════════════════════╗"
echo "  ║                                                           ║"
echo "  ║   ██████╗██╗      █████╗ ██╗   ██╗██████╗ ███████╗        ║"
echo "  ║  ██╔════╝██║     ██╔══██╗██║   ██║██╔══██╗██╔════╝        ║"
echo "  ║  ██║     ██║     ███████║██║   ██║██║  ██║█████╗          ║"
echo "  ║  ██║     ██║     ██╔══██║██║   ██║██║  ██║██╔══╝          ║"
echo "  ║  ╚██████╗███████╗██║  ██║╚██████╔╝██████╔╝███████╗        ║"
echo "  ║   ╚═════╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝        ║"
echo "  ║                                                           ║"
echo "  ║              ⚡ DEPLOYMENT SYSTEM v2.0 ⚡                  ║"
echo "  ║                                                           ║"
echo "  ╚═══════════════════════════════════════════════════════════╝"
echo ""
echo "  Environment: $ENVIRONMENT"
echo "  Project:     $(basename $PROJECT_DIR)"
echo "  Flags:       ${FLAG:-none}"
echo ""

# Verify tools are available
echo "Checking tool availability..."
if command -v gcloud &> /dev/null; then
    echo "gcloud found: $(which gcloud)"
else
    echo "Warning: gcloud not found in PATH"
    echo "PATH: $PATH"
fi

if command -v docker &> /dev/null; then
    echo "docker found: $(which docker)"
else
    echo "Warning: docker not found in PATH"
fi

if command -v make &> /dev/null; then
    echo "make found: $(which make)"
else
    echo "Warning: make not found in PATH"
fi
echo ""

# Build make command
if [ -n "$FLAG" ]; then
    MAKE_CMD="make deploy ENVIRONMENT=$ENVIRONMENT FLAG=$FLAG"
else
    MAKE_CMD="make deploy ENVIRONMENT=$ENVIRONMENT"
fi

echo "Running: $MAKE_CMD"
echo ""

# Always set up cleanup trap for mutex (cloud deployments too)
cleanup_mutex() {
    echo ""
    echo "  Cleaning up..."
    rm -f "$SIGNAL_FILE" 2>/dev/null || true
}
trap cleanup_mutex EXIT INT TERM

# For non-local cloud deployments, try to use Listr2 runner for rich output
CLI_TOOLS_DIR="/mnt/imperium/Scripts/cli-tools"
if [ "$IS_LOCAL_DEPLOYMENT" = false ] && [ -t 1 ] && command -v node >/dev/null 2>&1; then
    # Check if listr2 is available (check in cli-tools directory)
    if (cd "$CLI_TOOLS_DIR" && node -e "require('listr2')" 2>/dev/null); then
        echo "  ⚡ Using rich terminal display (Listr2)"
        echo ""
        (cd "$CLI_TOOLS_DIR" && node "$DEPLOY_DIR/deploy-runner.js" "$ENVIRONMENT" "$FLAG" "$PROJECT_DIR")
        EXIT_CODE=$?

        if [ $EXIT_CODE -eq 0 ]; then
            echo ""
            echo "Deployment completed successfully!"
        else
            echo ""
            echo "Deployment failed with exit code $EXIT_CODE"
            echo "Check the log file for details: $LOG_FILE"
        fi

        # Cleanup mutex
        rm -f "$SIGNAL_FILE"

        exit $EXIT_CODE
    fi
fi

# Fall back to plain output if Listr2 not available
echo "(Using plain output mode)"
echo ""

# Setup signal handlers for local deployments
cleanup_local_server() {
    # Prevent re-entry (infinite loop protection)
    if [ -n "$CLEANUP_IN_PROGRESS" ]; then
        # Force exit if already cleaning up
        trap - INT TERM
        exit 130
    fi
    export CLEANUP_IN_PROGRESS=1

    if [ "$IS_LOCAL_DEPLOYMENT" = true ]; then
        # Disable trap IMMEDIATELY to prevent re-entry
        trap - INT TERM

        echo ""
        echo "Cleaning up local server..."

        # Use the Node.js stop utility for proper cleanup (runs synchronously)
        if [ -f "$STATE_FILE" ] && command -v node >/dev/null 2>&1; then
            # Call stop utility synchronously (don't background it)
            node "$DEPLOY_DIR/stop-local-server.js" --force >/dev/null 2>&1 || {
                # Fallback: manual cleanup if Node script fails
                node -e "
                    const fs = require('fs');
                    const { execSync } = require('child_process');
                    try {
                        const state = JSON.parse(fs.readFileSync('$STATE_FILE', 'utf8'));
                        const pids = state.pids || {};

                        // Kill app process tree
                        if (pids.app) {
                            try { execSync('pkill -P ' + pids.app + ' 2>/dev/null'); } catch(e) {}
                            try { execSync('kill -TERM ' + pids.app + ' 2>/dev/null'); } catch(e) {}
                        }

                        // Kill ngrok process
                        if (pids.ngrok) {
                            try { execSync('kill -TERM ' + pids.ngrok + ' 2>/dev/null'); } catch(e) {}
                        }

                        // Clean up by process name (more reliable)
                        try { execSync('pkill -f \"ngrok http 8080\" 2>/dev/null'); } catch(e) {}
                        try { execSync('pkill -f \"python.*app.main\" 2>/dev/null'); } catch(e) {}

                        // Update state
                        state.status = 'stopped';
                        state.stoppedAt = new Date().toISOString();
                        fs.writeFileSync('$STATE_FILE', JSON.stringify(state, null, 2));
                    } catch(e) {}
                " 2>/dev/null || true
            }
        fi

        # Kill make process and its children (if still running)
        # Do this carefully to avoid triggering more signals
        if [ -n "$MAKE_PID" ]; then
            # Kill the process group of make, but not our own
            kill -TERM -"$MAKE_PID" 2>/dev/null || kill -TERM "$MAKE_PID" 2>/dev/null || true
            sleep 0.5
            # Force kill if still running
            kill -KILL -"$MAKE_PID" 2>/dev/null || kill -KILL "$MAKE_PID" 2>/dev/null || true
        fi

        # Clean up mutex
        rm -f "$SIGNAL_FILE" 2>/dev/null || true

        echo "Cleanup complete"
    fi

    # Exit cleanly - trap is already disabled, so this won't trigger again
    exit 130  # Exit code 130 typically means killed by SIGINT
}

if [ "$IS_LOCAL_DEPLOYMENT" = true ]; then
    # Set up trap with proper signal handling
    # Note: We don't trap EXIT here because we want normal exit to complete normally
    trap cleanup_local_server INT TERM

    # Track wrapper PID in state file
    if command -v node >/dev/null 2>&1; then
        node -e "
            const fs = require('fs');
            const path = require('path');
            const stateFile = '$STATE_FILE';
            let state = {};
            if (fs.existsSync(stateFile)) {
                state = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
            }
            state.pids = state.pids || {};
            state.pids.wrapper = process.pid;
            state.status = 'starting';
            fs.writeFileSync(stateFile, JSON.stringify(state, null, 2));
        " 2>/dev/null || true
    fi
fi

# Run deployment and capture exit code
set +e
if [ "$IS_LOCAL_DEPLOYMENT" = true ]; then
    # For local deployments, run in background to capture PID, then wait
    # Note: The actual app PID will be tracked by the Makefile process
    # We'll try to find it after the fact by checking running processes
    $MAKE_CMD 2>&1 | tee "$LOG_FILE" &
    MAKE_PID=$!

    # Wait a moment for processes to start, then try to find app and ngrok PIDs
    sleep 3

    if command -v node >/dev/null 2>&1; then
        # Try to find ngrok PID
        NGROK_PID=$(pgrep -f "ngrok http 8080" | head -n 1 || echo "")

        # Try to find Python app PID (uv run python -m app or app.main)
        APP_PID=""
        if [ -n "$MAKE_PID" ]; then
            APP_PID=$(pgrep -f -P "$MAKE_PID" "python.*-m app" | head -n 1 || echo "")
        fi
        if [ -z "$APP_PID" ]; then
            APP_PID=$(pgrep -f "python.*-m app(\\.main)?" | head -n 1 || echo "")
        fi

        # Update state file with PIDs
        node -e "
            const fs = require('fs');
            const stateFile = '$STATE_FILE';
            if (fs.existsSync(stateFile)) {
                const state = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
                state.pids = state.pids || {};
                if ('$NGROK_PID') state.pids.ngrok = parseInt('$NGROK_PID');
                if ('$APP_PID') state.pids.app = parseInt('$APP_PID');
                if ('$MAKE_PID') state.pids.make = parseInt('$MAKE_PID');
                state.status = 'running';
                fs.writeFileSync(stateFile, JSON.stringify(state, null, 2));
            }
        " 2>/dev/null || true
    fi

    # Wait for make command to complete
    # Use timeout to prevent hanging if process doesn't respond
    wait $MAKE_PID 2>/dev/null || true
    EXIT_CODE=$?

    # If wait was interrupted (Ctrl+C), handle cleanup
    if [ $EXIT_CODE -eq 130 ] || [ $EXIT_CODE -eq 143 ]; then
        # Signal received, cleanup already handled by trap
        exit $EXIT_CODE
    fi
else
    # For non-local deployments, run normally
    $MAKE_CMD 2>&1 | tee "$LOG_FILE"
    EXIT_CODE=$?
fi
set -e

echo ""
echo "========================================"
echo "Deployment finished with exit code: $EXIT_CODE"
echo "========================================"
echo ""

# Deduplicate log file to reduce verbosity
if [ -f "$DEPLOY_DIR/deduplicate-log.js" ]; then
    echo "Cleaning up log file..."
    node "$DEPLOY_DIR/deduplicate-log.js" "$LOG_FILE" 2>/dev/null || true
    echo ""
fi

# Cleanup mutex
echo "Cleaning up mutex..."
rm -f "$SIGNAL_FILE"

if [ $EXIT_CODE -eq 0 ]; then
    echo "Deployment completed successfully!"
else
    echo "Deployment failed with exit code $EXIT_CODE"
    echo "Check the log file for details: $LOG_FILE"
fi

# Check if this is one-shot mode (skip interactive prompt)
IS_ONE_SHOT=false
if [ -f "$STATE_FILE" ] && command -v node >/dev/null 2>&1; then
    IS_ONE_SHOT=$(node -e "
        try {
            const fs = require('fs');
            const state = JSON.parse(fs.readFileSync('$STATE_FILE', 'utf8'));
            console.log(state.oneShot === true ? 'true' : 'false');
        } catch(e) {
            console.log('false');
        }
    " 2>/dev/null || echo "false")
fi

# Skip interactive prompt in one-shot mode (default auto-close)
if [ "$IS_ONE_SHOT" != "true" ]; then
    SHOULD_PAUSE=false
    if [ -n "$KEEP_DEPLOY_TERMINAL_OPEN" ]; then
        case "${KEEP_DEPLOY_TERMINAL_OPEN,,}" in
            1|true|yes|on)
                SHOULD_PAUSE=true
                ;;
        esac
    fi

    if [ "$SHOULD_PAUSE" = true ]; then
        echo ""
        echo "Press enter to close this window..."
        read
    fi
fi
