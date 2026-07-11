// Voice / TTS interactive control deck. Was a read-only describe-the-commands
// panel (see web-ops-voice-queue-panel-2026-05-25); now the buttons POST to
// Token-API — which IS the routing-through-the-authority contract, not a
// dual-write. Surfaces routing target + reason, now-playing emphasis, and makes
// a row click focus that pane in tmux (server-resolved by instance_id — raw
// %pane ids never reach the browser) + expand the full message.

import { useState } from 'react';
import type { OpsState, TtsQueueItem, TtsGlobalMode, VoiceDraft } from '../types';
import { skipTts, promotePause, playPane, setGlobalMode, focusPane } from '../api';

const MODES: TtsGlobalMode[] = ['verbose', 'muted', 'silent'];
const TTS_LANGUISHING_THRESHOLD = 5;

/** Age of an ISO timestamp in a compact "3h12m" / "45s" form. */
function ageSince(iso: string | null | undefined): string {
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

/** Small async-runner: transient note on result/failure, refetch on success. */
function useDeckAction(refresh: () => void) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  async function run<T>(fn: () => Promise<T>, onOk?: (r: T) => string | null) {
    setBusy(true);
    setNote(null);
    try {
      const r = await fn();
      const m = onOk?.(r);
      if (m) setNote(m);
      refresh();
    } catch (e) {
      setNote(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return { busy, note, run };
}

function QueueRow({
  item,
  kind,
  busy,
  onFocus,
  onAction,
}: {
  item: TtsQueueItem;
  kind: 'hot' | 'pause';
  busy: boolean;
  onFocus: (id: string) => void;
  onAction: () => void;
}) {
  const [open, setOpen] = useState(false);
  const who = item.name || item.instance_id.slice(0, 8);

  return (
    <li className={`vq__row vq__row--act ${open ? 'is-open' : ''}`}>
      <button
        type="button"
        className="vq__rowbody"
        title="focus pane in tmux + expand"
        onClick={() => {
          onFocus(item.instance_id);
          setOpen((o) => !o);
        }}
      >
        <span className="vq__who">{who}</span>
        <span className={`vq__msg ${open ? 'vq__msg--full' : ''}`}>{item.message}</span>
        <span className="vq__meta">{item.voice ?? '—'}</span>
        <time className="vq__age">{ageSince(item.queued_at)}</time>
      </button>
      <span className="vq__rowact" onClick={(e) => e.stopPropagation()}>
        {kind === 'hot' ? (
          <button type="button" className="pill vq__btn" disabled={busy} onClick={onAction}>
            skip
          </button>
        ) : (
          <button type="button" className="pill vq__btn vq__btn--go" disabled={busy} onClick={onAction}>
            play
          </button>
        )}
      </span>
    </li>
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

export function VoiceQueuePanel({ state, refresh }: { state: OpsState; refresh: () => void }) {
  const tts = state.tts;
  const drafts = state.voice_drafts ?? [];
  const current = tts.current;
  const routing = tts.routing ?? null;
  const mode = (tts.global_mode ?? 'verbose') as TtsGlobalMode;
  const { busy, note, run } = useDeckAction(refresh);

  // Languishing is derived from live pause-queue depth only; it is not a stored state.
  const pauseHasItems = tts.pause_queue_length > 0;
  const pauseLanguishing = tts.pause_queue_length > TTS_LANGUISHING_THRESHOLD;

  // Distinct instance ids in the pause queue — "promote all" fans out per
  // instance (the promote endpoint's all-of-instance semantics) to drain them.
  const pauseInstances = Array.from(new Set(tts.pause_queue.map((it) => it.instance_id)));

  function handleFocus(id: string) {
    run(
      () => focusPane(id),
      (r) => (r.snapped ? null : `pane not focused: ${r.reason ?? 'unknown'}`),
    );
  }

  return (
    <div className="vq">
      {/* Now-playing — promoted from the tiny ▶ label to a clear row. */}
      <div className="vq__now-row">
        {current ? (
          <>
            <span className="vq__now-led" aria-hidden />
            <span className="vq__who">{current.name || current.instance_id.slice(0, 8)}</span>
            <span className="vq__now-msg" title={current.message}>{current.message}</span>
            <span className="vq__now-meta">
              {current.voice ?? '—'}
              {current.backend ? ` · ${current.backend}` : ''}
              {current.started_at ? ` · ${ageSince(current.started_at)}` : ''}
            </span>
            <button
              type="button"
              className="pill vq__btn"
              disabled={busy}
              onClick={() => run(() => skipTts(false))}
            >
              skip
            </button>
          </>
        ) : (
          <span className="vq__idle">idle — nothing speaking</span>
        )}
      </div>

      {/* Routing + global mode. */}
      <div className="vq__routing">
        <span className="vq__route-lbl">
          routing:{' '}
          {routing ? (
            <>
              <b className="vq__route-dev">{routing.device.toUpperCase()}</b>
              <span className="vq__route-why"> — {routing.reason}</span>
            </>
          ) : (
            <span className="vq__route-why">unavailable</span>
          )}
        </span>
        <span className="vq__modes" role="group" aria-label="Global TTS mode">
          mode:
          {MODES.map((m) => (
            <button
              key={m}
              type="button"
              className={`vq__mode ${m === mode ? 'is-active' : ''} mode--${m}`}
              disabled={busy || m === mode}
              onClick={() => run(() => setGlobalMode(m))}
            >
              {m}
            </button>
          ))}
        </span>
        {note ? <span className="vq__note">{note}</span> : null}
      </div>

      <div className="vq__section">
        <h4>Hot queue</h4>
        {tts.hot_queue.length ? (
          <ul className="vq__rows">
            {tts.hot_queue.map((it, i) => (
              <QueueRow
                key={`${it.instance_id}-${it.queued_at}-${i}`}
                item={it}
                kind="hot"
                busy={busy}
                onFocus={handleFocus}
                onAction={() => run(() => skipTts(false))}
              />
            ))}
          </ul>
        ) : (
          <p className="empty">empty</p>
        )}
      </div>

      <div className="vq__section">
        <h4>
          Pause queue {pauseLanguishing ? <span className="vq__warn">languishing</span> : null}
          {pauseHasItems ? (
            <button
              type="button"
              className="pill vq__btn vq__btn--go vq__promote-all"
              disabled={busy}
              onClick={() => run(() => Promise.all(pauseInstances.map((id) => playPane(id))))}
            >
              promote all
            </button>
          ) : null}
        </h4>
        {tts.pause_queue.length ? (
          <ul className="vq__rows">
            {tts.pause_queue.map((it, i) => (
              <QueueRow
                key={`${it.instance_id}-${it.queued_at}-${i}`}
                item={it}
                kind="pause"
                busy={busy}
                onFocus={handleFocus}
                onAction={() => run(() => promotePause(it.instance_id))}
              />
            ))}
          </ul>
        ) : (
          <p className="empty">empty</p>
        )}
        {pauseHasItems ? (
          <p className="vq__hint">
            Paused items wait silently — <code>play</code> a row or <code>promote all</code> to
            voice them.
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
