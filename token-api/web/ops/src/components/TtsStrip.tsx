// Compact pinned TTS strip. Rendered right after the masthead so it sits in
// flow at first paint, then `position: sticky` pins it to the top on scroll —
// NOT a permanent banner (deliberately slim + TTS-specific so it never reads as
// the identity masthead that was removed). Shows now-playing, queue depths, the
// routing device, and the two hottest controls: global-mode toggle + skip.

import { useState } from 'react';
import type { OpsState } from '../types';
import type { CockpitLayoutModel } from '../layoutModel';
import { skipTts } from '../api';

/** Clamp a message to a single short glance-line for the strip. */
function clamp(msg: string | null | undefined, n = 64): string {
  if (!msg) return '';
  const t = msg.replace(/\s+/g, ' ').trim();
  return t.length > n ? `${t.slice(0, n - 1)}…` : t;
}

export function TtsStrip({ state, layout, refresh }: { state: OpsState; layout: CockpitLayoutModel; refresh: () => void }) {
  const tts = state.tts;
  const routing = tts.routing ?? null;
  const waiters = layout.activeTtsWaiters.slice(0, 4);

  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  async function run<T>(fn: () => Promise<T>, ok?: (r: T) => string | null) {
    setBusy(true);
    setNote(null);
    try {
      const r = await fn();
      const m = ok?.(r);
      if (m) setNote(m);
      refresh();
    } catch (e) {
      setNote(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (!waiters.length) return null;

  return (
    <div className="ttsstrip ttsstrip--active" role="region" aria-label="Active TTS waiters">
      <span className="ttsstrip__label">voice waiters</span>
      <span className="ttsstrip__waiters">
        {waiters.map((waiter) => (
          <span key={waiter.id} className={`ttsstrip__waiter ttsstrip__waiter--${waiter.kind}`}>
            {waiter.kind === 'speaking' ? <span className="ttsstrip__led" aria-hidden /> : null}
            <b className="ttsstrip__tab">{waiter.label}</b>
            <span className="ttsstrip__msg" title={waiter.message}>{clamp(waiter.message, 54)}</span>
          </span>
        ))}
      </span>
      <span className="ttsstrip__stat">
        hot <b className={tts.hot_queue_length ? 'ok' : 'muted'}>{tts.hot_queue_length}</b>
      </span>
      <span className="ttsstrip__stat">
        pause <b className={tts.pause_queue_length > 5 ? 'bad' : 'muted'}>{tts.pause_queue_length}</b>
      </span>

      {routing ? (
        <span className="ttsstrip__route" title={routing.reason}>
          → <b>{routing.device.toUpperCase()}</b>
        </span>
      ) : null}

      <span className="ttsstrip__div" aria-hidden />

      <button
        type="button"
        className="ttsstrip__skip"
        disabled={busy || !tts.current}
        onClick={() => run(() => skipTts(false))}
      >
        skip
      </button>

      {note ? <span className="ttsstrip__note">{note}</span> : null}
    </div>
  );
}
