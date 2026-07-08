// fleet-status-publisher.ts — the #fleet-status read-model surface.
//
// Feature-flagged (config.fleet_status_enabled + a 'fleet-status' channel
// alias; default OFF) poll of token-api's ops read-model, validated with the
// shared contract and rendered via fleet-render into ONE message the daemon
// edits in place. The daemon renders read-models; it never derives state
// itself — token-api stays the single authority.

import { OpsStateSchema } from '@token-os/contracts';
import { renderFleetStatus } from './fleet-render.ts';

const DEFAULT_INTERVAL_MS = 15_000;
const FETCH_TIMEOUT_MS = 5_000;

export function createFleetStatusPublisher({
  client,
  config,
  logger,
  fetchImpl = fetch,
  intervalMs = DEFAULT_INTERVAL_MS,
}) {
  const channelId = config.channels?.['fleet-status'] || null;
  const enabled = Boolean(config.fleet_status_enabled) && Boolean(channelId);
  let timer = null;
  let stopped = false;
  let messageId = null;
  let lastContent = '';

  async function tick() {
    const resp = await fetchImpl(
      `http://127.0.0.1:${config.token_api_port}/api/ui/ops/state`,
      { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) },
    );
    if (!resp.ok) throw new Error(`ops state HTTP ${resp.status}`);
    const state = OpsStateSchema.parse(await resp.json());
    const { content } = renderFleetStatus(state);
    if (content === lastContent) return { changed: false };

    if (messageId) {
      try {
        await client.editMessage(channelId, messageId, content);
        lastContent = content;
        return { changed: true, edited: true };
      } catch (err) {
        // Message deleted/aged out — fall through to a fresh send.
        logger.warn(`fleet-status: edit failed (${err.message}); re-sending`);
        messageId = null;
      }
    }
    const result = await client.sendMessage(channelId, content);
    messageId = result.message_id || (result.message_ids || [])[0] || null;
    lastContent = content;
    return { changed: true, edited: false };
  }

  function start() {
    if (!enabled) {
      logger.info('fleet-status publisher disabled (flag or channel missing)');
      return false;
    }
    stopped = false;
    const run = async () => {
      try {
        await tick();
      } catch (err) {
        logger.warn(`fleet-status: tick failed: ${err.message}`);
      } finally {
        if (!stopped) timer = setTimeout(run, intervalMs);
      }
    };
    run();
    return true;
  }

  function stop() {
    stopped = true;
    if (timer) clearTimeout(timer);
    timer = null;
  }

  return {
    start,
    stop,
    tick,
    get enabled() { return enabled; },
    get messageId() { return messageId; },
  };
}
