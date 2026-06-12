// voice-route-retry.js — loud tmux-lag warning wrapper for Discord voice routing.

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
}) {
  async function warnTmuxLagging() {
    logger?.warn?.(`Voice route [${botLabel}]: tmux lag/route failure; not retrying`);
    try {
      await voiceManager?.playTTS?.('tmux lagging', botLabel);
    } catch {}
  }

  try {
    const routed = await router.route(result);
    if (routed?.routed === false && isRetryableVoiceRouteFailure(routed)) {
      await warnTmuxLagging();
      return { ...routed, attempts: 1, warning_sent: true, retry_disabled: true, tmux_lag: true };
    }
    return { ...routed, attempts: 1, warning_sent: false };
  } catch (err) {
    if (isRetryableVoiceRouteFailure(err)) {
      await warnTmuxLagging();
      err.warning_sent = true;
      err.retry_disabled = true;
      err.tmux_lag = true;
    } else {
      err.warning_sent = false;
    }
    err.attempts = 1;
    throw err;
  }
}
