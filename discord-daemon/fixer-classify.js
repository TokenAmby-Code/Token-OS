// fixer-classify.js — shared, pure predicate for the Discord voice fixer router.
// Distinguishes benign Realtime lifecycle events from genuine errors so normal
// provider lifecycle noise never escalates to ERROR-level logging or pages the fixer.

// Returns true for benign Realtime lifecycle/provider events that must NOT page the fixer.
// The matches are deliberately narrow: the known 60-minute session cap and the
// provider's empty-buffer commit response after a local 0ms/tiny cleanup race.
/**
 * @param {string} errorCode
 * @param {string} msg
 * @returns {boolean}
 */
export function isBenignFixerError(errorCode, msg) {
  if (errorCode === 'session_expired') return true;
  const text = String(msg || '').toLowerCase();
  if (text.includes('maximum duration of 60 minutes')) return true;
  // (?<!\d\.|\d) keeps the zero standalone: "250.0ms"/"10ms" must not match.
  if (text.includes('buffer too small') && /(?<!\d\.|\d)0(?:\.0+)?\s*ms\b/.test(text)) return true;
  return false;
}
