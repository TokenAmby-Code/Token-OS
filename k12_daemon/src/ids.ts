// Canonical-id membrane (spec §7 rung 2).
//
// Canonical ids (seat names like `somnium:NE`) are the ONLY id surface the
// daemon exposes. Raw tmux ids — pane `%N`, window `@N`, session `$N` — live
// strictly BELOW the tmux control plane and must never appear in an API
// response, a log line, or an event payload. This module is the guard that
// makes that invariant testable: the control plane translates at the membrane,
// and `assertNoTmuxId` fails loud if anything leaks upward.

// tmux id sigils followed by digits: `%5` (pane), `@5` (window), `$5` (session).
// Anchored to the sigil+digits shape so canonical ids (`somnium:NE`, `palace:W`)
// and ordinary text never false-positive.
const TMUX_ID_PATTERN = /(?:^|[^A-Za-z0-9_])([%@$]\d+)\b/;

export function findTmuxId(text: string): string | null {
  const m = TMUX_ID_PATTERN.exec(text);
  return m ? m[1]! : null;
}

/**
 * Recursively scan a value for a leaked tmux id. Returns the JSON-path of the
 * first offender (e.g. `payload.pane`) or null when clean. Used by the
 * committed no-%id test and by the membrane before anything crosses upward.
 */
export function findTmuxIdDeep(value: unknown, path = '$'): string | null {
  if (typeof value === 'string') {
    return findTmuxId(value) ? path : null;
  }
  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i++) {
      const hit = findTmuxIdDeep(value[i], `${path}[${i}]`);
      if (hit) return hit;
    }
    return null;
  }
  if (value && typeof value === 'object') {
    for (const [k, v] of Object.entries(value)) {
      // Keys can leak too (e.g. an object keyed by pane id).
      if (findTmuxId(k)) return `${path}.${k} (key)`;
      const hit = findTmuxIdDeep(v, `${path}.${k}`);
      if (hit) return hit;
    }
    return null;
  }
  return null;
}

export function assertNoTmuxId(value: unknown, where: string): void {
  const leak = findTmuxIdDeep(value);
  if (leak) {
    throw new Error(`k12_daemon canonical-id breach: raw tmux id leaked at ${where} (${leak})`);
  }
}
