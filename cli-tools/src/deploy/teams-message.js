/**
 * Teams Message Payload Builder
 *
 * Library module for generating Teams Bot Framework activity payloads.
 * Mirrors google-chat-message.js for the Teams channel.
 *
 * Usage (via test CLI):
 *   test --teams "Create Ticket Need help with procurement"
 *   test --teams --action escalate
 *   test --teams --action create_ticket --action-data '{"subject":"RFP help"}'
 *
 * Supported text commands (parsed by TeamsCommandAdapter.parse_text):
 *   "Create Ticket <subject>"
 *   "My Tickets [open|resolved|all]"
 *   "Escalate to Advisor [context]"
 *
 * Supported Action.Submit actions (parsed by TeamsCommandAdapter.parse_action_submit):
 *   escalate, view_ticket, add_comment, create_ticket
 */

const fs = require('fs');
const path = require('path');

const TEXT_TEMPLATE_PATH = path.join(__dirname, 'payloads', 'teams-text-command.json');
const ACTION_TEMPLATE_PATH = path.join(__dirname, 'payloads', 'teams-action-submit.json');

// Teams webhook endpoint (no auth-bypass endpoint needed — TEAMS_AUTH_BYPASS env var handles it)
const TEAMS_WEBHOOK_ENDPOINT = '/api/webhooks/teams/messages';

/**
 * Generate a random activity ID (simulates Bot Framework activity IDs)
 */
function generateActivityId() {
  const chars = 'abcdefghijklmnopqrstuvwxyz0123456789';
  const segments = [8, 4, 4, 4, 12];
  return segments.map(len => {
    let s = '';
    for (let i = 0; i < len; i++) {
      s += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return s;
  }).join('-');
}

/**
 * Generate a random user/conversation ID
 */
function generateId(prefix = '') {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  let result = prefix;
  for (let i = 0; i < 20; i++) {
    result += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return result;
}

// Stable test IDs so deduplication doesn't interfere across runs
const DEFAULT_USER_ID = 'test-user-local-dev-001';
const DEFAULT_AAD_ID = '00000000-0000-0000-0000-000000000001';
const DEFAULT_TENANT_ID = '00000000-0000-0000-0000-000000000000';
const DEFAULT_CONVERSATION_ID = 'test-convo-local-dev-001';

/**
 * Build a Teams text command activity payload
 * @param {string} messageText - The message text (e.g., "Create Ticket Need help")
 * @param {object} options - Optional overrides
 * @returns {object} Bot Framework activity
 */
function buildTextPayload(messageText, options = {}) {
  const template = JSON.parse(fs.readFileSync(TEXT_TEMPLATE_PATH, 'utf8'));
  const timestamp = new Date().toISOString();
  // Each message gets a unique activity ID to avoid deduplication
  const activityId = generateActivityId();

  let payloadStr = JSON.stringify(template);
  payloadStr = payloadStr.replace(/\{\{ACTIVITY_ID\}\}/g, activityId);
  payloadStr = payloadStr.replace(/\{\{TIMESTAMP\}\}/g, timestamp);
  payloadStr = payloadStr.replace(/\{\{USER_ID\}\}/g, options.userId || DEFAULT_USER_ID);
  payloadStr = payloadStr.replace(/\{\{USER_NAME\}\}/g, options.userName || 'Test User');
  payloadStr = payloadStr.replace(/\{\{AAD_OBJECT_ID\}\}/g, options.aadObjectId || DEFAULT_AAD_ID);
  payloadStr = payloadStr.replace(/\{\{TENANT_ID\}\}/g, options.tenantId || DEFAULT_TENANT_ID);
  payloadStr = payloadStr.replace(/\{\{CONVERSATION_ID\}\}/g, options.conversationId || DEFAULT_CONVERSATION_ID);
  payloadStr = payloadStr.replace(/\{\{MESSAGE_TEXT\}\}/g, messageText);

  return JSON.parse(payloadStr);
}

/**
 * Build a Teams Action.Submit activity payload
 * @param {string} action - Action name (e.g., "escalate", "create_ticket", "view_ticket")
 * @param {object} actionData - Additional action data fields
 * @param {object} options - Optional overrides
 * @returns {object} Bot Framework activity
 */
function buildActionPayload(action, actionData = {}, options = {}) {
  const template = fs.readFileSync(ACTION_TEMPLATE_PATH, 'utf8');
  const timestamp = new Date().toISOString();
  const activityId = generateActivityId();

  // Build the action value object
  const actionValue = JSON.stringify({ action, ...actionData });

  let payloadStr = template;
  payloadStr = payloadStr.replace(/\{\{ACTIVITY_ID\}\}/g, activityId);
  payloadStr = payloadStr.replace(/\{\{TIMESTAMP\}\}/g, timestamp);
  payloadStr = payloadStr.replace(/\{\{USER_ID\}\}/g, options.userId || DEFAULT_USER_ID);
  payloadStr = payloadStr.replace(/\{\{USER_NAME\}\}/g, options.userName || 'Test User');
  payloadStr = payloadStr.replace(/\{\{AAD_OBJECT_ID\}\}/g, options.aadObjectId || DEFAULT_AAD_ID);
  payloadStr = payloadStr.replace(/\{\{TENANT_ID\}\}/g, options.tenantId || DEFAULT_TENANT_ID);
  payloadStr = payloadStr.replace(/\{\{CONVERSATION_ID\}\}/g, options.conversationId || DEFAULT_CONVERSATION_ID);
  payloadStr = payloadStr.replace(/\{\{ACTION_VALUE\}\}/g, actionValue);

  return JSON.parse(payloadStr);
}

/**
 * Predefined action payloads for common test scenarios
 */
const PRESET_ACTIONS = {
  escalate: {
    action: 'escalate',
    data: { conversation_context: 'User was asking about procurement policies and needs human help.' }
  },
  'create-ticket': {
    action: 'create_ticket',
    data: { subject: 'Need help with RFP process' }
  },
  'view-ticket': {
    action: 'view_ticket',
    data: { ticket_id: '00000000-0000-0000-0000-000000000001' }
  },
  'add-comment': {
    action: 'add_comment',
    data: { ticket_id: '00000000-0000-0000-0000-000000000001', comment_text: 'Any updates on this?' }
  }
};

/**
 * Build request configuration for use with test CLI
 * @param {string} input - Text message OR action name (prefixed with "action:")
 * @param {object} options - Configuration options
 * @returns {object} Request configuration { endpoint, method, body, headers }
 */
function buildRequestConfig(input, options = {}) {
  let body;

  if (options.action) {
    // Action.Submit mode
    const preset = PRESET_ACTIONS[options.action];
    if (preset) {
      const data = { ...preset.data, ...options.actionData };
      body = buildActionPayload(preset.action, data, options);
    } else {
      // Custom action
      body = buildActionPayload(options.action, options.actionData || {}, options);
    }
  } else {
    // Text command mode
    body = buildTextPayload(input, options);
  }

  return {
    endpoint: TEAMS_WEBHOOK_ENDPOINT,
    method: 'POST',
    body,
    headers: {
      'Content-Type': 'application/json'
    }
  };
}

module.exports = {
  buildTextPayload,
  buildActionPayload,
  buildRequestConfig,
  generateActivityId,
  TEAMS_WEBHOOK_ENDPOINT,
  PRESET_ACTIONS,
};
