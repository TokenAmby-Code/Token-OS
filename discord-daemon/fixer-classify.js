// fixer-classify.js — shared, pure predicate for the Discord voice fixer router.
// Distinguishes benign Realtime lifecycle events from genuine errors so a normal
// 60-minute session expiry never escalates to ERROR-level logging or pages the fixer.

// Returns true for benign Realtime lifecycle events that must NOT page the fixer.
// The match is deliberately narrow — only the known 60-minute session cap signal.
/**
 * @param {string} errorCode
 * @param {string} msg
 * @returns {boolean}
 */
export function isBenignFixerError(errorCode, msg) {
  if (errorCode === 'session_expired') return true;
  const text = String(msg || '').toLowerCase();
  return text.includes('maximum duration of 60 minutes');
}
