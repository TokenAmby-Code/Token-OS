// message-store.js — Pending message persistence (crash recovery)
// Writes outgoing messages to disk before sending, removes on success

import { writeFileSync, readFileSync, readdirSync, unlinkSync, mkdirSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PENDING_DIR = join(__dirname, '..', 'pending');

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
