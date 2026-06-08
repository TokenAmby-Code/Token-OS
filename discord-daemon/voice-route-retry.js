// voice-route-retry.js — loud retry wrapper for Discord voice -> tmux routing.

const DEFAULT_MAX_ATTEMPTS = 3;
const DEFAULT_RETRY_DELAY_MS = 1000;

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

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
  let lastFailure = null;
  const attempts = Math.max(1, Number(maxAttempts) || DEFAULT_MAX_ATTEMPTS);

  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const routed = await router.route(result);
      if (routed?.routed !== false || !isRetryableVoiceRouteFailure(routed) || attempt >= attempts) {
        return { ...routed, attempts: attempt, warning_sent: warned };
      }
      lastFailure = routed;
    } catch (err) {
      if (!isRetryableVoiceRouteFailure(err) || attempt >= attempts) {
        err.attempts = attempt;
        err.warning_sent = warned;
        throw err;
      }
      lastFailure = err;
    }

    if (!warned) {
      warned = true;
      logger?.warn?.(
        `Voice route [${botLabel}]: tmux lag/route failure; starting retry ` +
        `(${attempt}/${attempts})`
      );
      try {
        await voiceManager?.playTTS?.('Tmux route is lagging. Retrying voice delivery.', botLabel);
      } catch {}
    }

    await sleep(retryDelayMs);
  }

  return {
    routed: false,
    reason: lastFailure?.reason || lastFailure?.message || 'route_failed',
    attempts,
    warning_sent: warned,
  };
}
