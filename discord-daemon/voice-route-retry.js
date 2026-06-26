// voice-route-retry.js — loud warning wrapper for Discord voice routing.
// There is deliberately no retry loop and no fallback route.

/**
 * @returns {boolean}
 */
export function isRetryableVoiceRouteFailure(resultOrError) {
  if (!resultOrError) return false;
  if (resultOrError.routed === false) {
    return ['no_target', 'target_not_live', 'route_timeout', 'voice_session_not_found'].includes(String(resultOrError.reason || ''));
  }
  const message = String(resultOrError?.message || resultOrError || '').toLowerCase();
  return (
    message.includes('target not live') ||
    message.includes('timed out') ||
    message.includes('timeout') ||
    message.includes('voice session not found')
  );
}

export async function routeVoiceTranscriptWithRetry({
  router,
  voiceManager,
  logger,
  result,
  botLabel = result?.botName || 'voice',
}) {
  async function warnRouteFailure(reason = 'route failure') {
    logger?.warn?.(`Voice route [${botLabel}]: ${reason}; not retrying`);
    try {
      await voiceManager?.playTTS?.('voice route failed', botLabel);
    } catch {}
  }

  try {
    const routed = await router.route(result);
    if (routed?.routed === false && isRetryableVoiceRouteFailure(routed)) {
      await warnRouteFailure(routed.reason || 'route failure');
      return { ...routed, attempts: 1, warning_sent: true, retry_disabled: true };
    }
    return { ...routed, attempts: 1, warning_sent: false };
  } catch (err) {
    if (isRetryableVoiceRouteFailure(err)) {
      await warnRouteFailure(err.message || 'route failure');
      err.warning_sent = true;
      err.retry_disabled = true;
    } else {
      err.warning_sent = false;
    }
    err.attempts = 1;
    throw err;
  }
}
