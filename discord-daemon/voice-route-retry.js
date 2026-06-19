// voice-route-retry.js — loud tmux-lag guard for Discord voice -> tmux routing.

const DEFAULT_MAX_ATTEMPTS = 1;
const DEFAULT_RETRY_DELAY_MS = 0;

export function isRetryableVoiceRouteFailure(resultOrError) {
  if (!resultOrError) return false;
  if (resultOrError.routed === false) {
    return ['no_target', 'target_not_live', 'tmux_timeout'].includes(String(resultOrError.reason || ''));
  }
  const message = String(resultOrError?.message || resultOrError || '').toLowerCase();
  return (
    message.includes('target not live') ||
    message.includes('timed out') ||
    message.includes('timeout') ||
    message.includes('tmux') ||
    message.includes('tmux-dictate')
  );
}

export async function routeVoiceTranscriptWithRetry({
  router,
  voiceManager,
  logger,
  result,
  botLabel = result?.botName || 'voice',
  maxAttempts = DEFAULT_MAX_ATTEMPTS,
  retryDelayMs = DEFAULT_RETRY_DELAY_MS,
}) {
  let warned = false;
  // Keep the legacy knobs in the public wrapper signature, but do not honor
  // them for tmux-lag failures: retrying a timed-out tmux write can duplicate
  // text that already landed.
  void maxAttempts;
  void retryDelayMs;

  async function warnTmuxLagOnce() {
    if (warned) return;
    warned = true;
    logger?.warn?.(`Voice route [${botLabel}]: tmux lag/route failure; retry disabled`);
    try {
      await voiceManager?.playTTS?.('tmux lagging', botLabel);
    } catch {}
  }

  try {
    const routed = await router.route(result);
    if (routed?.routed === false && isRetryableVoiceRouteFailure(routed)) {
      await warnTmuxLagOnce();
      return {
        ...routed,
        attempts: 1,
        warning_sent: warned,
        retry_disabled: true,
        tmux_lag: true,
      };
    }
    return { ...routed, attempts: 1, warning_sent: warned };
  } catch (err) {
    if (!isRetryableVoiceRouteFailure(err)) {
      err.attempts = 1;
      err.warning_sent = warned;
      throw err;
    }
    await warnTmuxLagOnce();
    return {
      routed: false,
      reason: err?.message || String(err) || 'route_failed',
      attempts: 1,
      warning_sent: warned,
      retry_disabled: true,
      tmux_lag: true,
    };
  }
}
