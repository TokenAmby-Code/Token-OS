// startup-voice-cleanup.ts — boot-time voice draft-truth sweep.
//
// token-restart restarts tmuxctld and this daemon together, so the old blind
// clear raced a tmuxctld that was still coming up and burned a 75s long-hold
// timeout on every boot ("startup voice cleanup failed: ETIMEDOUT"). Wait for
// tmuxctld /health first (bounded retries with backoff), then run the clears
// with short per-call timeouts, then sweep the third copy of draft truth:
// token-api's in-memory _discord_voice_drafts dict.

export const DEFAULT_HEALTH_ATTEMPTS = 6;
export const DEFAULT_HEALTH_TIMEOUT_MS = 1_500;
export const DEFAULT_CLEAR_TIMEOUT_MS = 5_000;
export const DEFAULT_TOKEN_API_TIMEOUT_MS = 3_000;

/**
 * @typedef {object} StartupVoiceCleanup
 * @property {function(): Promise<{tmuxctld: object, clears: object[], tokenApiSweep: object}>} run
 * @property {function(): Promise<{healthy: boolean, attempts: number, reason?: string}>} waitForTmuxctld
 * @property {function(): Promise<object[]>} clearTmuxctldSessions
 * @property {function(): Promise<object>} sweepTokenApiDrafts
 */

/**
 * @returns {StartupVoiceCleanup}
 */
export function createStartupVoiceCleanup({
  config,
  logger,
  voiceTranscriptRouter,
  tmuxctld,
  fetchImpl = fetch,
  sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  healthAttempts = DEFAULT_HEALTH_ATTEMPTS,
  healthTimeoutMs = DEFAULT_HEALTH_TIMEOUT_MS,
  clearTimeoutMs = DEFAULT_CLEAR_TIMEOUT_MS,
  tokenApiTimeoutMs = DEFAULT_TOKEN_API_TIMEOUT_MS,
}) {
  async function waitForTmuxctld() {
    let delayMs = 250;
    for (let attempt = 1; attempt <= healthAttempts; attempt++) {
      try {
        await tmuxctld.health({ timeoutMs: healthTimeoutMs });
        return { healthy: true, attempts: attempt };
      } catch (err) {
        const code = err?.code || err?.message || 'unknown';
        if (code === 'ETIMEDOUT') {
          // The socket accepted the connection but /health never answered:
          // the endpoint is wedged, not booting. Retrying will not un-wedge
          // it — record a typed error and move on to the short-timeout clears.
          logger.warn(
            `Voice startup cleanup: tmuxctld /health timed out after ${healthTimeoutMs}ms (endpoint wedged)`,
            { errorCode: 'tmuxctld_health_timeout' },
          );
          return { healthy: false, attempts: attempt, reason: 'ETIMEDOUT' };
        }
        // ECONNREFUSED / fetch failed: tmuxctld is still coming up alongside
        // this daemon — retry with backoff.
        if (attempt === healthAttempts) {
          logger.warn(
            `Voice startup cleanup: tmuxctld unreachable after ${attempt} attempts (${code})`,
            { errorCode: 'tmuxctld_unreachable_at_boot' },
          );
          return { healthy: false, attempts: attempt, reason: code };
        }
        await sleep(delayMs);
        delayMs = Math.min(delayMs * 2, 4_000);
      }
    }
    return { healthy: false, attempts: healthAttempts, reason: 'exhausted' };
  }

  async function clearTmuxctldSessions() {
    const results = [];
    for (const botName of Object.keys(config.voice_channels || {})) {
      try {
        const cleared = await voiceTranscriptRouter.clear({ bot: botName }, { timeoutMs: clearTimeoutMs });
        results.push({ botName, ok: true, cleared: cleared.length });
      } catch (err) {
        const code = err?.code || err?.message || 'unknown';
        const errorCode = code === 'ETIMEDOUT' ? 'voice_boot_clear_timeout' : 'voice_boot_clear_failed';
        logger.warn(`Voice [${botName}]: startup voice cleanup failed: ${code}`, { errorCode });
        results.push({ botName, ok: false, errorCode });
      }
    }
    return results;
  }

  async function sweepTokenApiDrafts() {
    const port = config.token_api_port;
    if (!port) return { ok: false, skipped: true, reason: 'no_token_api_port' };
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), tokenApiTimeoutMs);
    try {
      const resp = await fetchImpl(`http://127.0.0.1:${port}/api/discord/voice-drafts/clear`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
        signal: controller.signal,
      });
      if (!resp.ok) {
        logger.warn(
          `Voice startup cleanup: token-api draft sweep returned ${resp.status}`,
          { errorCode: 'token_api_draft_sweep_failed' },
        );
        return { ok: false, status: resp.status };
      }
      const payload = await resp.json().catch(() => ({}));
      logger.info(`Voice startup cleanup: token-api draft sweep cleared ${payload.cleared ?? 0} draft(s)`);
      return { ok: true, cleared: payload.cleared ?? 0 };
    } catch (err) {
      const code = err?.name === 'AbortError' ? 'ETIMEDOUT' : (err?.cause?.code || err?.code || err?.message);
      logger.warn(
        `Voice startup cleanup: token-api draft sweep failed: ${code}`,
        { errorCode: 'token_api_draft_sweep_failed' },
      );
      return { ok: false, error: code };
    } finally {
      clearTimeout(timer);
    }
  }

  async function run() {
    const health = await waitForTmuxctld();
    let clears = [];
    if (health.healthy || health.reason === 'ETIMEDOUT') {
      // Wedged /health still gets a clear attempt — the clears carry their own
      // short timeouts and typed errors, so the cost is bounded either way.
      clears = await clearTmuxctldSessions();
    } else {
      logger.warn(
        'Voice startup cleanup: skipping tmuxctld voice clears (daemon unreachable)',
        { errorCode: 'voice_boot_clear_skipped' },
      );
    }
    // token-api is an independent service — sweep its draft dict regardless.
    const tokenApiSweep = await sweepTokenApiDrafts();
    return { tmuxctld: health, clears, tokenApiSweep };
  }

  return { run, waitForTmuxctld, clearTmuxctldSessions, sweepTokenApiDrafts };
}
