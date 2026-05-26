import { useOpsState, useTimerHistory, useOpsGraph } from './api';
import { formatClock } from './format';
import { TopStrip } from './components/TopStrip';
import { TimerGraph } from './components/TimerGraph';
import { InstancesPanel } from './components/InstancesPanel';
import { AssertionsPanel, AttentionPanel, EventsPanel, StatusCards } from './components/SidePanels';
import { VoiceQueuePanel } from './components/VoiceQueuePanel';
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

export function App() {
  const ops = useOpsState();
  const timer = useTimerHistory();
  const graph = useOpsGraph();

  const state = ops.data;
  // Connection health: error AND data older than ~6s ⇒ visibly degraded.
  const stale = ops.error && ops.lastOk != null && Date.now() - ops.lastOk > 6000;
  const connClass = ops.error ? (stale ? 'conn--bad' : 'conn--warn') : 'conn--ok';

  return (
    <div className="cockpit">
      <div className="cockpit__grain" aria-hidden />
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
          <TopStrip state={state} />

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
                  ? `today · since 07:00 · ${timer.data.points.length} pts`
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
                {Object.entries(state.instances.counts.by_legion).map(([k, v]) => (
                  <span className="chip chip--legion" key={k}>{k} {v}</span>
                ))}
              </div>
            }
          >
            <InstancesPanel instances={state.instances.active} />
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
            <VoiceQueuePanel state={state} />
          </Panel>

          <Panel
            title="Relationship graph"
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
