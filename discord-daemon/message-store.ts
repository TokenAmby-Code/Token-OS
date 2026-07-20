// message-store.js — Pending message persistence (crash recovery)
// Writes outgoing messages to disk before sending, removes on success

import { writeFileSync, readFileSync, readdirSync, unlinkSync, mkdirSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PENDING_DIR = join(__dirname, '..', 'pending');

// Pending messages are human-facing and often time-sensitive (alerts, morning
// briefings). Recovery must not replay one hours or days after it was queued —
// a stale "it's 05:19, firing the backstop now" resent after a token fix is
// noise at best and misinformation at worst. Anything older than this at
// recovery time is dropped loudly instead of resent.
export const PENDING_MAX_AGE_MS = 10 * 60_000;

/**
 * Decide whether a persisted pending message is too old to replay.
 * @returns {boolean} true when the message must be dropped instead of resent.
 */
export function isStalePending(msg, nowMs = Date.now(), maxAgeMs = PENDING_MAX_AGE_MS) {
  const persistedAt = Date.parse(msg?.persisted_at || '');
  // Unknown age is stale: never resend a human-facing message of unknown vintage.
  if (Number.isNaN(persistedAt)) return true;
  // A future persisted_at means the clock moved since persistence — the true
  // age is unknowable, so anything outside ±maxAgeMs is stale too.
  return Math.abs(nowMs - persistedAt) > maxAgeMs;
}

export function createMessageStore(logger) {
  mkdirSync(PENDING_DIR, { recursive: true });

  return {
    // Save a message before attempting to send
    persist(id, data) {
      const path = join(PENDING_DIR, `${id}.json`);
      writeFileSync(path, JSON.stringify({ ...data, persisted_at: new Date().toISOString() }));
      logger.debug(`Persisted pending message ${id}`);
    },

    // Remove after successful send
    remove(id) {
      const path = join(PENDING_DIR, `${id}.json`);
      try {
        unlinkSync(path);
        logger.debug(`Removed pending message ${id}`);
      } catch {
        // Already removed, fine
      }
    },

    // Load all pending messages (for crash recovery on startup)
    // Returns objects with _filename so recovery can delete the right file
    loadPending() {
      try {
        const files = readdirSync(PENDING_DIR).filter(f => f.endsWith('.json'));
        return files.map(f => {
          try {
            const data = JSON.parse(readFileSync(join(PENDING_DIR, f), 'utf-8'));
            return { ...data, _filename: f };
          } catch {
            return null;
          }
        }).filter(Boolean);
      } catch {
        return [];
      }
    },
  };
}
