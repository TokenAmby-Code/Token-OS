#!/usr/bin/env node

/**
 * Test Command - Smart testing utility for local development
 *
 * Provides a unified interface for testing the local server with automatic
 * detection of input type and server state.
 *
 * Usage:
 *   test [input] [options]
 *
 * Input Types (auto-detected):
 *   (none)          Health check only
 *   "message text"  Google Chat message (quotes required)
 *   /path           HTTP endpoint (starts with /)
 *
 * Options:
 *   -m, --method METHOD     HTTP method (default: GET/POST auto)
 *   -b, --body JSON         Request body as JSON
 *   -H, --header KEY:VALUE  Custom header (repeatable)
 *   --teams                Send as Teams Bot Framework activity instead of Google Chat
 *   --teams-action NAME    Send Teams Action.Submit (escalate, create-ticket, view-ticket, add-comment)
 *   --action-data JSON     Extra data for --teams-action (merged into action value)
 *   --localhost             Use localhost instead of ngrok
 *   --dry-run               Show payload/config without sending
 *   --one-shot              Force start → test → stop
 *   --attach                Require running server
 *   -h, --help              Show help
 *
 * Examples:
 *   test                                  # Health check
 *   test "hello world"                    # Google Chat message
 *   test "hello" --localhost              # Via localhost
 *   test "hello" --dry-run                # Show payload
 *   test "hello" --one-shot               # Full cycle
 *   test /api/status                      # GET request
 *   test /webhook -m POST -b '{"text":"hi"}'
 *   test --teams "Create Ticket Need help with procurement"
 *   test --teams "My Tickets"             # List tickets
 *   test --teams-action escalate          # Action.Submit button click
 *   test --teams-action create-ticket --action-data '{"subject":"RFP help"}'
 */

const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');

// Import from same directory
const DEPLOY_DIR = __dirname;
const { getState } = require(path.join(DEPLOY_DIR, 'local-server-state'));
const { sendLocalRequest } = require(path.join(DEPLOY_DIR, 'send-local-request'));
const { buildRequestConfig } = require(path.join(DEPLOY_DIR, 'google-chat-message'));
const { buildRequestConfig: buildTeamsRequestConfig } = require(path.join(DEPLOY_DIR, 'teams-message'));

/**
 * Detect input type from the input string
 * @param {string|null} input - The input string
 * @returns {'health'|'message'|'endpoint'}
 */
function detectInputType(input) {
  if (!input) {
    return 'health';
  }

  // If starts with /, it's an endpoint
  if (input.startsWith('/')) {
    return 'endpoint';
  }

  // If it's quoted (user provided quotes), it's a message
  // Note: quotes are typically stripped by shell, but we check anyway
  if ((input.startsWith('"') && input.endsWith('"')) ||
      (input.startsWith("'") && input.endsWith("'"))) {
    return 'message';
  }

  // If contains spaces, likely a message (but could also be endpoint with params)
  // Default to message if ambiguous - users can use --method to override
  if (input.includes(' ')) {
    return 'message';
  }

  // Single word could be message or endpoint, default to endpoint if starts with /
  // Otherwise treat as message
  return 'message';
}

/**
 * Check if local server is running
 * @returns {boolean}
 */
function isServerRunning() {
  const state = getState();
  if (!state) {
    return false;
  }

  // Check if server was started recently (within last 24 hours)
  const startedAt = new Date(state.startedAt);
  const now = new Date();
  const hoursAgo = (now - startedAt) / (1000 * 60 * 60);

  if (hoursAgo > 24) {
    return false; // Stale state
  }

  // Check if status indicates running
  if (state.status === 'stopped' || state.status === 'failed') {
    return false;
  }

  // TODO: Could also check if PIDs are still alive, but state file is usually reliable
  return true;
}

/**
 * Parse command-line arguments
 */
function parseArgs() {
  const args = process.argv.slice(2);
  const parsed = {
    input: null,
    method: null,
    body: null,
    headers: {},
    teams: false,
    teamsAction: null,
    actionData: null,
    localhost: false,
    dryRun: false,
    oneShot: false,
    attach: false,
    help: false
  };

  for (let i = 0; i < args.length; i++) {
    const arg = args[i];

    if (arg === '-h' || arg === '--help') {
      parsed.help = true;
    } else if (arg === '-m' || arg === '--method') {
      if (i + 1 < args.length) {
        parsed.method = args[++i].toUpperCase();
      }
    } else if (arg === '-b' || arg === '--body') {
      if (i + 1 < args.length) {
        try {
          parsed.body = JSON.parse(args[++i]);
        } catch (error) {
          console.error('Error: Invalid JSON for --body');
          process.exit(1);
        }
      }
    } else if (arg === '-H' || arg === '--header') {
      if (i + 1 < args.length) {
        const header = args[++i];
        const colonIndex = header.indexOf(':');
        if (colonIndex === -1) {
          console.error('Error: Invalid header format. Use KEY:VALUE');
          process.exit(1);
        }
        const key = header.substring(0, colonIndex).trim();
        const value = header.substring(colonIndex + 1).trim();
        parsed.headers[key] = value;
      }
    } else if (arg === '--teams') {
      parsed.teams = true;
    } else if (arg === '--teams-action') {
      parsed.teams = true;
      if (i + 1 < args.length) {
        parsed.teamsAction = args[++i];
      }
    } else if (arg === '--action-data') {
      if (i + 1 < args.length) {
        try {
          parsed.actionData = JSON.parse(args[++i]);
        } catch (error) {
          console.error('Error: Invalid JSON for --action-data');
          process.exit(1);
        }
      }
    } else if (arg === '--localhost') {
      parsed.localhost = true;
    } else if (arg === '--dry-run') {
      parsed.dryRun = true;
    } else if (arg === '--one-shot') {
      parsed.oneShot = true;
    } else if (arg === '--attach') {
      parsed.attach = true;
    } else if (!arg.startsWith('-')) {
      // First non-flag argument is the input
      if (!parsed.input) {
        parsed.input = arg;
      }
    }
  }

  return parsed;
}

/**
 * Show help text
 */
function showHelp() {
  console.log(`Test Command - Smart testing utility for local development

Usage: test [input] [options]

Input Types (auto-detected):
  (none)          Health check only
  "message text"  Google Chat message (quotes required)
  /path           HTTP endpoint (starts with /)

Channel Flags:
  --teams                Send as Teams Bot Framework activity
  --teams-action NAME    Send Teams Action.Submit button click
  --action-data JSON     Extra data for --teams-action

Options:
  -m, --method METHOD     HTTP method (default: GET/POST auto)
  -b, --body JSON         Request body as JSON
  -H, --header KEY:VALUE  Custom header (repeatable)
  --localhost             Use localhost instead of ngrok
  --dry-run               Show payload/config without sending
  --one-shot              Force start → test → stop
  --attach                Require running server
  -h, --help              Show help

Detection Rules:
  - No input → health check
  - Starts with / → HTTP endpoint
  - --teams flag → Teams Bot Framework activity
  - Default → Google Chat message
  - Auto-selects one-shot if no server running

Teams Commands (text):
  "Create Ticket <subject>"           → ticket/create
  "My Tickets [open|resolved|all]"    → ticket/list
  "Escalate to Advisor [context]"     → ticket/escalate

Teams Actions (--teams-action):
  escalate, create-ticket, view-ticket, add-comment

Note: Teams endpoint returns {"status":"processing"} immediately.
      Command runs in background — check logs or use deploy debug for breakpoints.

Examples:
  test                                  # Health check
  test "hello world"                    # Google Chat message
  test --teams "Create Ticket Need help with procurement"
  test --teams "My Tickets"             # List open tickets
  test --teams "Escalate to Advisor"    # Text escalation
  test --teams-action escalate          # Button click escalation
  test --teams-action create-ticket --action-data '{"subject":"RFP help"}'
  test --teams "hello" --dry-run        # Show Teams payload
  test /api/status                      # GET request
`);
}

/**
 * Run health check
 */
async function runHealthCheck(localhost) {
  console.log('Running health check...');

  try {
    const response = await sendLocalRequest('/health', { method: 'GET' }, !localhost);
    if (response.status === 200) {
      console.log('✅ Server is healthy');
      return true;
    } else {
      console.log(`⚠️  Server returned status ${response.status}`);
      return false;
    }
  } catch (error) {
    console.error('❌ Health check failed:', error.message);
    return false;
  }
}

/**
 * Run Google Chat message test
 */
async function runGoogleChatMessage(messageText, options) {
  console.log(`Sending Google Chat message: "${messageText}"`);

  const config = buildRequestConfig(messageText);

  if (options.dryRun) {
    console.log('\nPayload (dry-run):');
    console.log(JSON.stringify(config.body, null, 2));
    console.log('\nEndpoint:', config.endpoint);
    console.log('Method:', config.method);
    return true;
  }

  try {
    const response = await sendLocalRequest(
      config.endpoint,
      {
        method: config.method,
        body: config.body,
        headers: { ...config.headers, ...options.headers }
      },
      !options.localhost
    );

    if (response.status === 200) {
      console.log('✅ Message sent successfully');
      return true;
    } else {
      console.log(`⚠️  Server returned status ${response.status}`);
      return false;
    }
  } catch (error) {
    console.error('❌ Message failed:', error.message);
    return false;
  }
}

/**
 * Run Teams message/action test
 */
async function runTeamsMessage(messageText, options) {
  const isAction = !!options.teamsAction;

  if (isAction) {
    console.log(`Sending Teams Action.Submit: "${options.teamsAction}"`);
  } else {
    console.log(`Sending Teams message: "${messageText}"`);
  }

  const config = buildTeamsRequestConfig(messageText, {
    action: options.teamsAction || undefined,
    actionData: options.actionData || undefined,
  });

  if (options.dryRun) {
    console.log('\nPayload (dry-run):');
    console.log(JSON.stringify(config.body, null, 2));
    console.log('\nEndpoint:', config.endpoint);
    console.log('Method:', config.method);
    return true;
  }

  try {
    const response = await sendLocalRequest(
      config.endpoint,
      {
        method: config.method,
        body: config.body,
        headers: { ...config.headers, ...options.headers }
      },
      !options.localhost
    );

    if (response.status === 200) {
      const status = response.body && response.body.status;
      if (status === 'processing') {
        console.log('✅ Activity accepted (processing in background)');
        console.log('   Check server logs for command result, or use deploy debug for breakpoints');
      } else if (status === 'duplicate') {
        console.log('⚠️  Duplicate activity (already processed)');
      } else {
        console.log(`✅ Response: ${JSON.stringify(response.body)}`);
      }
      return true;
    } else {
      console.log(`⚠️  Server returned status ${response.status}`);
      return false;
    }
  } catch (error) {
    console.error('❌ Teams message failed:', error.message);
    return false;
  }
}

/**
 * Run HTTP endpoint test
 */
async function runEndpointTest(endpoint, options) {
  const method = options.method || (options.body ? 'POST' : 'GET');
  console.log(`Testing endpoint: ${method} ${endpoint}`);

  if (options.dryRun) {
    console.log('\nRequest config (dry-run):');
    console.log('  Endpoint:', endpoint);
    console.log('  Method:', method);
    if (options.body) {
      console.log('  Body:', JSON.stringify(options.body, null, 2));
    }
    if (Object.keys(options.headers).length > 0) {
      console.log('  Headers:', options.headers);
    }
    return true;
  }

  try {
    const response = await sendLocalRequest(
      endpoint,
      {
        method,
        body: options.body,
        headers: options.headers
      },
      !options.localhost
    );

    console.log(`✅ Response received (status ${response.status})`);
    return true;
  } catch (error) {
    console.error('❌ Request failed:', error.message);
    return false;
  }
}

/**
 * Run one-shot mode (delegate to trigger-deploy.js)
 */
function runOneShot(input, options) {
  const triggerDeploy = path.join(DEPLOY_DIR, 'trigger-deploy.js');

  const args = [
    'development',
    '-l',
    '--blocking',
    '--one-shot'
  ];

  // Add input-specific arguments
  const inputType = detectInputType(input);
  if (options.teams || options.teamsAction) {
    // Teams: use generic request with pre-built payload
    const config = buildTeamsRequestConfig(input, {
      action: options.teamsAction || undefined,
      actionData: options.actionData || undefined,
    });
    args.push('--request-endpoint', config.endpoint);
    args.push('--request-method', 'POST');
    args.push('--request-body', JSON.stringify(config.body));
  } else if (inputType === 'message') {
    args.push('--google-chat-message', input);
  } else if (inputType === 'endpoint') {
    args.push('--request-endpoint', input);
    if (options.method) {
      args.push('--request-method', options.method);
    }
    if (options.body) {
      args.push('--request-body', JSON.stringify(options.body));
    }
  } else {
    // Health check
    args.push('--request-endpoint', '/health');
  }

  if (options.localhost) {
    args.push('--force-localhost');
  }

  console.log('Running one-shot deployment...');
  const child = spawn('node', [triggerDeploy, ...args], {
    stdio: 'inherit',
    cwd: process.cwd()
  });

  child.on('exit', (code) => {
    process.exit(code);
  });
}

/**
 * Main entry point
 */
async function main() {
  const options = parseArgs();

  if (options.help) {
    showHelp();
    process.exit(0);
  }

  // Detect input type
  const inputType = detectInputType(options.input);

  // Check server status
  const serverRunning = isServerRunning();

  // Determine mode
  let mode;
  if (options.oneShot) {
    mode = 'one-shot';
  } else if (options.attach) {
    mode = 'attach';
  } else if (serverRunning) {
    mode = 'attach';
  } else {
    mode = 'one-shot';
    console.log('No running server detected, using one-shot mode');
  }

  // If attach mode but no server, error
  if (mode === 'attach' && !serverRunning && options.attach) {
    console.error('Error: --attach requires a running local server');
    console.error('Hint: Start a server with "deploy local" first');
    process.exit(1);
  }

  // Execute based on mode
  if (mode === 'one-shot') {
    runOneShot(options.input, options);
  } else {
    // Attach mode - use existing server
    let success = false;

    if (options.teams || options.teamsAction) {
      success = await runTeamsMessage(options.input, options);
    } else if (inputType === 'health') {
      success = await runHealthCheck(options.localhost);
    } else if (inputType === 'message') {
      success = await runGoogleChatMessage(options.input, options);
    } else if (inputType === 'endpoint') {
      success = await runEndpointTest(options.input, options);
    }

    process.exit(success ? 0 : 1);
  }
}

// Run if called directly
if (require.main === module) {
  main().catch(error => {
    console.error('Error:', error);
    process.exit(1);
  });
}

module.exports = { detectInputType, isServerRunning, parseArgs };
