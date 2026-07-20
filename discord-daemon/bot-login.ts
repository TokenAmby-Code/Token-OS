// bot-login.ts — gateway login with bounded retry.
//
// A failed login used to `delete botClients[name]` and move on: one transient
// Discord 500 at boot permanently removed the bot until the next daemon
// restart (inquisition lost its voice-selftest speaker seat for days that
// way), while the never-logged-in client object lingered as a zombie. Retries
// reuse the SAME client object — handlers registered at creation time
// (onMessage, voice auto-join, selftest) stay wired — with bounded backoff.
// While a bot is down it is removed from the shared botClients map so sends
// fall back to the default bot instead of dying inside a dead client; a
// successful retry re-adds it. Exhausted retries destroy the client so no
// zombie ws/token state survives, and say so loudly.

export const LOGIN_RETRY_DELAYS_MS = [60_000, 300_000, 900_000];

/**
 * @returns {{connect: (name: string, client: object, attempt?: number) => Promise<boolean>}}
 */
export function createBotLogin({
  botClients,
  logger,
  retryDelaysMs = LOGIN_RETRY_DELAYS_MS,
  setTimeoutImpl = setTimeout,
}) {
  async function connect(name, client, attempt = 0) {
    try {
      await client.start();
      botClients[name] = client;
      logger.info(`Bot '${name}' connected${attempt ? ` (login retry ${attempt})` : ''}`);
      return true;
    } catch (err) {
      logger.warn(`Bot '${name}' failed to connect${attempt ? ` (login retry ${attempt})` : ''}: ${err?.message || err}`);
      delete botClients[name];
      if (attempt >= retryDelaysMs.length) {
        try { await client.stop(); } catch { /* already dead */ }
        logger.warn(
          `Bot '${name}': login retries exhausted after ${retryDelaysMs.length + 1} attempts; ` +
          'bot stays down until the token is fixed and the daemon restarts',
        );
        return false;
      }
      const delayMs = retryDelaysMs[attempt];
      logger.info(`Bot '${name}': retrying login in ${Math.round(delayMs / 1000)}s`);
      const timer = setTimeoutImpl(() => {
        connect(name, client, attempt + 1).catch(() => {});
      }, delayMs);
      timer?.unref?.();
      return false;
    }
  }

  async function startAll() {
    for (const [name, client] of Object.entries(botClients)) {
      await connect(name, client);
    }
  }

  return { startAll, connect };
}
