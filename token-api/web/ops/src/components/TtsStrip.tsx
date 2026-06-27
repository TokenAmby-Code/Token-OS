// Compact pinned TTS strip. Rendered right after the masthead so it sits in
// flow at first paint, then `position: sticky` pins it to the top on scroll —
// NOT a permanent banner (deliberately slim + TTS-specific so it never reads as
// the identity masthead that was removed). Shows now-playing, queue depths, the
// routing device, and the two hottest controls: global-mode toggle + skip.

import { useState } from 'react';
import type { OpsState, TtsGlobalMode } from '../types';
import { skipTts, setGlobalMode } from '../api';

const MODES: TtsGlobalMode[] = ['verbose', 'muted', 'silent'];
const TTS_LANGUISHING_THRESHOLD = 5;

/** Clamp a message to a single short glance-line for the strip. */
function clamp(msg: string | null | undefined, n = 64): string {
  if (!msg) return '';
  const t = msg.replace(/\s+/g, ' ').trim();
  return t.length > n ? `${t.slice(0, n - 1)}…` : t;
}

export function TtsStrip({ state, refresh }: { state: OpsState; refresh: () => void }) {
  const tts = state.tts;
  const current = tts.current;
  const routing = tts.routing ?? null;
  const mode = (tts.global_mode ?? 'verbose') as TtsGlobalMode;
  const pauseLanguishing = tts.pause_queue_length > TTS_LANGUISHING_THRESHOLD;

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

  return (
    <div className="ttsstrip" role="region" aria-label="TTS control strip">
      <span className="ttsstrip__now">
        {current ? (
          <>
            <span className="ttsstrip__led" aria-hidden />
            <b className="ttsstrip__tab">{current.tab_name || current.instance_id.slice(0, 8)}</b>
            <span className="ttsstrip__msg" title={current.message}>
              {clamp(current.message)}
            </span>
          </>
        ) : (
          <span className="ttsstrip__idle">idle</span>
        )}
      </span>

      <span className="ttsstrip__stat">
        hot <b className={tts.hot_queue_length ? 'ok' : 'muted'}>{tts.hot_queue_length}</b>
      </span>
      <span className="ttsstrip__stat">
        pause <b className={pauseLanguishing ? 'bad' : 'muted'}>{tts.pause_queue_length}</b>
      </span>

      {routing ? (
        <span className="ttsstrip__route" title={routing.reason}>
          → <b>{routing.device.toUpperCase()}</b>
        </span>
      ) : null}

      <span className="ttsstrip__div" aria-hidden />

      <span className="ttsstrip__modes" role="group" aria-label="Global TTS mode">
        {MODES.map((m) => (
          <button
            key={m}
            type="button"
            className={`ttsstrip__mode ${m === mode ? 'is-active' : ''} mode--${m}`}
            disabled={busy || m === mode}
            onClick={() => run(() => setGlobalMode(m))}
          >
            {m}
          </button>
        ))}
      </span>

      <button
        type="button"
        className="ttsstrip__skip"
        disabled={busy || !current}
        onClick={() => run(() => skipTts(false))}
      >
        skip
      </button>

      {note ? <span className="ttsstrip__note">{note}</span> : null}
    </div>
  );
}
