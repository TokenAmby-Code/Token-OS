// Voice / TTS queue visibility. Surfaces what was previously invisible: the
// TTS pause queue (which accumulates silently and needs explicit promotion)
// and the Discord voice-draft locks (operator speech buffered against a pane).
// Both languished unseen before this panel — see
// web-ops-voice-queue-panel-2026-05-25. Read-only; consumes /api/ui/ops/state.

import type { OpsState, TtsQueueItem, VoiceDraft } from '../types';
import { formatTime } from '../format';

/** Age of an ISO timestamp in a compact "3h12m" / "45s" form. */
function ageSince(iso: string | null): string {
  if (!iso) return '—';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '—';
  const sec = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h${m % 60}m`;
}

function QueueRows({ items }: { items: TtsQueueItem[] }) {
  return (
    <ul className="vq__rows">
      {items.map((it, i) => (
        <li key={`${it.instance_id}-${it.queued_at}-${i}`} className="vq__row">
          <span className="vq__who">{it.tab_name || it.instance_id.slice(0, 8)}</span>
          <span className="vq__msg" title={it.message}>{it.message}</span>
          <span className="vq__meta">{it.voice ?? '—'}</span>
          <time className="vq__age">{ageSince(it.queued_at)}</time>
        </li>
      ))}
    </ul>
  );
}

function DraftRows({ drafts }: { drafts: VoiceDraft[] }) {
  return (
    <ul className="vq__rows">
      {drafts.map((d, i) => (
        <li key={`${d.bot_name}-${d.author_id}-${i}`} className="vq__row">
          <span className="vq__who">{d.bot_name}</span>
          <span className="vq__msg">
            {d.utterances} utterance{d.utterances === 1 ? '' : 's'} → {d.pane ?? '—'}
          </span>
          <span className={`vq__meta ${d.pane_alive === false ? 'bad' : ''}`}>
            {d.pane_alive === false ? 'pane dead' : 'pane live'}
          </span>
          <time className="vq__age">{ageSince(d.created_at)}</time>
        </li>
      ))}
    </ul>
  );
}

export function VoiceQueuePanel({ state }: { state: OpsState }) {
  const tts = state.tts;
  const drafts = state.voice_drafts ?? [];
  const current = tts.current as Record<string, unknown> | null;

  // pause queue is the silent-languish risk — make depth loud when non-zero.
  const pauseDeep = tts.pause_queue_length > 0;

  return (
    <div className="vq">
      <div className="vq__strip">
        <span className={`vq__stat ${tts.hot_queue_length ? 'ok' : 'muted'}`}>
          hot <b>{tts.hot_queue_length}</b>
        </span>
        <span className={`vq__stat ${pauseDeep ? 'bad' : 'muted'}`}>
          pause <b>{tts.pause_queue_length}</b>
        </span>
        <span className="vq__stat muted">
          drafts <b>{drafts.length}</b>
        </span>
        <span className="vq__now">
          {current ? `▶ ${String(current.tab_name ?? current.instance_id ?? 'playing')}` : 'idle'}
        </span>
      </div>

      <div className="vq__section">
        <h4>Hot queue</h4>
        {tts.hot_queue.length ? <QueueRows items={tts.hot_queue} /> : <p className="empty">empty</p>}
      </div>

      <div className="vq__section">
        <h4>Pause queue {pauseDeep ? <span className="vq__warn">languishing</span> : null}</h4>
        {tts.pause_queue.length ? (
          <QueueRows items={tts.pause_queue} />
        ) : (
          <p className="empty">empty</p>
        )}
        {pauseDeep ? (
          <p className="vq__hint">
            Promote with <code>POST /api/tts/queue/promote</code> or clear with{' '}
            <code>tts-skip --all</code>.
          </p>
        ) : null}
      </div>

      <div className="vq__section">
        <h4>Discord voice drafts</h4>
        {drafts.length ? <DraftRows drafts={drafts} /> : <p className="empty">no active drafts</p>}
      </div>
    </div>
  );
}
