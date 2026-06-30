import { useCallback, useEffect, useRef, useState } from 'react';
import { focusPane, useOpsState, useTimerHistory, useOpsGraph, useSessionDocs } from './api';
import type { TtsCurrent } from './types';
import { formatClock } from './format';
import { HudRings } from './components/TopStrip';
import { TimerGraph } from './components/TimerGraph';
import { InstancesPanel, type Selection } from './components/InstancesPanel';
import { AssertionsPanel, AttentionPanel, EventsPanel, StatusCards } from './components/SidePanels';
import { VoiceQueuePanel } from './components/VoiceQueuePanel';
import { TtsStrip } from './components/TtsStrip';
import { PipelinePanel } from './components/PipelinePanel';
import { OpsGraph } from './components/OpsGraph';

function Panel({
  title,
  meta,
  children,
  className,
}: {
  title: string;
  meta?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className ?? ''}`}>
      <header className="panel__head">
        <h2><span className="panel__bracket">▌</span>{title}</h2>
        {meta ? <div className="panel__meta">{meta}</div> : null}
      </header>
      {children}
    </section>
  );
}

// One "select + expand an instance" mechanism for both cockpit features:
//   A) a manual double-click in the fleet table, and
//   B) the currently-talking instance (auto).
// Both set the same selection and render the same expanded card. A manual
// selection also reflects into tmux via /api/instances/{id}/focus-pane
// (focus + zoom + @OPS_SELECTED mark); the talking case is already mirrored in
// tmux by the backend TTS focus-snap, so we don't double-fire it from here.
function useInstanceSelection(current: TtsCurrent | null, activeIds: string[]) {
  const [selection, setSelection] = useState<Selection | null>(null);
  const [focusNote, setFocusNote] = useState<string | null>(null);
  const selectionRef = useRef<Selection | null>(null);
  selectionRef.current = selection;
  // Last *distinct* talker we auto-selected on. Only advanced when a real talker
  // appears — deliberately NOT reset when TTS falls silent — so a same-speaker
  // pause/resume (a gap between queued utterances → tts.current null → same id)
  // is not mistaken for a new talking event and cannot clobber a manual pin the
  // operator set during the gap.
  const prevTalking = useRef<string | null>(null);

  // Feature B: talking drives selection on each *distinct* new speaker, so a
  // manual pin survives until a genuinely different instance speaks
  // (last-writer-wins on real events, not on every 2s poll or TTS gap).
  const talkingId = current?.instance_id ?? null;
  useEffect(() => {
    if (talkingId && talkingId !== prevTalking.current) {
      setSelection({ id: talkingId, source: 'talking' });
      setFocusNote(null);
      prevTalking.current = talkingId;
    }
  }, [talkingId]);

  // Prune a selection whose instance has left the active fleet, so a vanished
  // row can't leave an invisible, undismissable ghost selection. If that
  // instance was the tracked talker, forget it so it re-expands if it returns.
  const activeKey = activeIds.join(',');
  useEffect(() => {
    setSelection((prev) => {
      if (prev && !activeIds.includes(prev.id)) {
        if (prevTalking.current === prev.id) prevTalking.current = null;
        return null;
      }
      return prev;
    });
  }, [activeKey]); // activeIds captured fresh each render; activeKey gates the run

  // Feature A: manual double-click. Re-selecting the manually-pinned row
  // collapses it. (A talking selection converts to manual on double-click —
  // double-click once more to collapse. Clearing a talking card via ✕ is a
  // deliberate dismissal; the same speaker won't auto-re-expand until a
  // different instance talks.)
  const select = useCallback((id: string) => {
    const prev = selectionRef.current;
    if (prev && prev.id === id && prev.source === 'manual') {
      setSelection(null);
      setFocusNote(null);
      return;
    }
    setSelection({ id, source: 'manual' });
    setFocusNote(null);
    focusPane(id)
      .then((r) => {
        if (!r.snapped) setFocusNote(`tmux pane not focused: ${r.reason ?? 'unknown'}`);
      })
      .catch((e) => setFocusNote(`tmux focus failed: ${e instanceof Error ? e.message : String(e)}`));
  }, []);

  const clear = useCallback(() => {
    setSelection(null);
    setFocusNote(null);
  }, []);

  return { selection, talkingId, focusNote, select, clear };
}

export function App() {
  const ops = useOpsState();
  const timer = useTimerHistory();
  const graph = useOpsGraph();
  const docs = useSessionDocs();

  const state = ops.data;
  const selection = useInstanceSelection(
    state?.tts.current ?? null,
    state ? state.instances.active.map((i) => i.id) : [],
  );
  const initialBuildId = useRef<string | null | undefined>(undefined);

  useEffect(() => {
    if (!state) return;
    if (initialBuildId.current === undefined) {
      initialBuildId.current = state.ui_build_id;
      return;
    }
    if (state.ui_build_id && initialBuildId.current && state.ui_build_id !== initialBuildId.current) {
      window.location.reload();
    }
  }, [state?.ui_build_id]);

  // Connection health: error AND data older than ~6s ⇒ visibly degraded.
  const stale = ops.error && ops.lastOk != null && Date.now() - ops.lastOk > 6000;
  const connClass = ops.error ? (stale ? 'conn--bad' : 'conn--warn') : 'conn--ok';

  return (
    <div className="cockpit">
      <div className="cockpit__grain" aria-hidden />

      {/* Free-floating, corner-aligned dials. Fixed to the viewport, overlays
          content, scroll-independent. Not a banner — just the gauges. */}
      {state ? (
        <div className="dials" aria-hidden>
          <HudRings state={state} />
        </div>
      ) : null}

      {/* Identity + connection scroll with the page — deliberately NOT part of
          the persistent overlay. */}
      <header className="masthead">
        <div className="masthead__id">
          <span className="masthead__sigil">⛭</span>
          <div>
            <span className="eyebrow">Terminus · Cogitator</span>
            <h1>Ops Cockpit</h1>
          </div>
        </div>
        <div className={`conn ${connClass}`}>
          <span className="conn__led" />
          <div className="conn__text">
            {ops.error
              ? stale
                ? `Signal lost · ${ops.error}`
                : `Retrying · ${ops.error}`
              : 'Telemetry nominal'}
            <span className="conn__sub">
              {state ? `synced ${formatClock(state.generated_at)}` : 'awaiting first frame'}
            </span>
          </div>
        </div>
      </header>

      {/* Slim, TTS-specific control strip — in flow after the masthead, pins to
          the top on scroll (sticky). Not a banner; below the floating dials. */}
      {state ? <TtsStrip state={state} refresh={ops.refresh} /> : null}

      {ops.loading && !state ? (
        <div className="boot">
          <div className="boot__bar"><span /></div>
          <p>Establishing cogitator link…</p>
        </div>
      ) : !state ? (
        <div className="boot boot--error">
          <p>No state available.</p>
          <span className="conn__sub">{ops.error ?? 'unknown error'}</span>
        </div>
      ) : (
        <>
          <Panel
            title="State assertions"
            meta={<span className="panel__meta-note">what Token-API believes is true</span>}
          >
            <AssertionsPanel state={state} />
          </Panel>

          <Panel
            title="Timer posture"
            className="panel--timerGraph"
            meta={
              <span className="panel__meta-note">
                {timer.data
                  ? `today · since 07:20 · ${timer.data.points.length} pts`
                  : 'loading'}
                {timer.error ? ` · ${timer.error}` : ''}
              </span>
            }
          >
            {timer.data ? <TimerGraph history={timer.data} /> : <div className="chart-empty">Loading history…</div>}
          </Panel>

          <Panel
            title="Active fleet"
            meta={
              <div className="chips">
                {Object.entries(state.instances.counts.by_status).map(([k, v]) => (
                  <span className="chip" key={k}>{k} {v}</span>
                ))}
                {Object.entries(state.instances.counts.by_persona).map(([k, v]) => (
                  <span className="chip chip--persona" key={k}>{k} {v}</span>
                ))}
              </div>
            }
          >
            <InstancesPanel
              instances={state.instances.active}
              selection={selection.selection}
              talkingId={selection.talkingId}
              current={state.tts.current}
              focusNote={selection.focusNote}
              onSelect={selection.select}
              onClear={selection.clear}
            />
          </Panel>

          <Panel
            title="Session pipeline"
            meta={
              <span className="panel__meta-note">
                read-only · authored in Obsidian
                {docs.data ? ` · ${docs.data.docs.length} docs` : ''}
                {docs.error ? ' · offline' : ''}
              </span>
            }
          >
            {docs.data ? (
              <PipelinePanel feed={docs.data} />
            ) : (
              <div className="chart-empty">Loading pipeline…</div>
            )}
          </Panel>

          <div className="row row--split">
            <Panel title="Attention evidence">
              <AttentionPanel state={state} />
            </Panel>
            <Panel title="Event stream">
              <EventsPanel events={state.events} />
            </Panel>
          </div>

          <Panel title="Subsystems">
            <StatusCards state={state} />
          </Panel>

          <Panel
            title="Voice / TTS queue"
            meta={
              <span className="panel__meta-note">
                hot {state.tts.hot_queue_length} · pause {state.tts.pause_queue_length} · drafts{' '}
                {state.voice_drafts?.length ?? 0}
              </span>
            }
          >
            <VoiceQueuePanel state={state} refresh={ops.refresh} />
          </Panel>

          <Panel
            title="Relationship graph"
            className="panel--graph"
            meta={<span className="panel__meta-note">{graph.data?.graph ?? '—'}{graph.error ? ' · mock' : ''}</span>}
          >
            {graph.data ? <OpsGraph graph={graph.data} /> : <div className="chart-empty">Loading graph…</div>}
          </Panel>

          <footer className="footnote">
            Read-only surface · mutations route through Token-API / CLI / tmux · /api/ui/ops/state
          </footer>
        </>
      )}
    </div>
  );
}
