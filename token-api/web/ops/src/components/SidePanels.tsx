// Compact secondary panels: attention evidence, events timeline, and the
// cron / Golden Throne / enforcement status cards.

import type { OpsState } from '../types';
import type {
  AssertionCard,
  CockpitLayoutModel,
  CockpitTone,
  CorrectionQueueItem,
  DrawerRailSummary,
  SourceHealthBucket,
  SourceHealthItem,
} from '../layoutModel';
import { formatAge, formatTime, summarizeDetails } from '../format';

function toneClass(tone: CockpitTone): string {
  return `tone--${tone}`;
}

function bucketLabel(bucket: SourceHealthBucket): string {
  return bucket[0].toUpperCase() + bucket.slice(1);
}

function SourceHealthRows({ items }: { items: SourceHealthItem[] }) {
  if (!items.length) return <p className="empty">No sources in this bucket.</p>;
  return (
    <ul className="sourcehealth__rows">
      {items.map((item) => (
        <li key={item.id} className={`sourcehealth__row ${toneClass(item.tone)}`}>
          <div>
            <strong>{item.label}</strong>
            <span className="subline">
              health {item.healthStatus ?? '—'} · freshness {item.freshnessStatus ?? '—'} · age{' '}
              {item.ageSeconds == null ? '—' : formatAge(item.ageSeconds)}
            </span>
          </div>
          <span className="sourcehealth__msg">{item.message}</span>
          {item.evidence.length ? <span className="sourcehealth__evidence">{item.evidence.join(' · ')}</span> : null}
        </li>
      ))}
    </ul>
  );
}

function CorrectionQueue({ items }: { items: CorrectionQueueItem[] }) {
  if (!items.length) return <p className="empty">No backend recommended actions.</p>;
  return (
    <div className="corrections">
      {items.map((item) => (
        <article key={item.id} className={`correction correction--${item.tone}`}>
          <header>
            <span className="correction__severity">{item.severity}</span>
            <span className="correction__source">{item.sourceAssertionId}</span>
          </header>
          <strong>{item.label}</strong>
          <p>{item.action}</p>
          {item.evidence.length ? (
            <ul>
              {item.evidence.slice(0, 3).map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          ) : null}
        </article>
      ))}
    </div>
  );
}

function RailSummary({ summary }: { summary: DrawerRailSummary }) {
  return (
    <article className={`railcard railcard--${summary.kind} ${toneClass(summary.tone)}`}>
      <header>
        <span>{summary.label}</span>
        <strong>{summary.count}</strong>
      </header>
      <p>{summary.headline}</p>
      <span className="subline">{summary.detail}</span>
    </article>
  );
}

export function HealthCorrectionsPanel({ model }: { model: CockpitLayoutModel }) {
  const sourceBuckets = model.sourceHealthSummary.buckets;
  const sourceSummaries = model.drawerSummaries.filter((summary) => summary.kind === 'sources');
  const correctionSummaries = model.drawerSummaries.filter((summary) => summary.kind === 'corrections');
  return (
    <div className="opshealth">
      <section className={`opshealth__banner ${toneClass(model.overallHealth.tone)}`}>
        <div>
          <span className="eyebrow">authoritative health</span>
          <strong>{model.overallHealth.status.toUpperCase()}</strong>
          <p>{model.overallHealth.summary}</p>
        </div>
        <dl>
          <div><dt>degraded sources</dt><dd>{model.sourceHealthSummary.degraded}</dd></div>
          <div><dt>corrections</dt><dd>{model.correctionQueue.length}</dd></div>
          <div><dt>assertions</dt><dd>{model.overallHealth.badAssertionCount}/{model.overallHealth.warnAssertionCount}</dd></div>
        </dl>
      </section>

      <div className="opshealth__grid">
        <section>
          <h3>Correction queue</h3>
          <CorrectionQueue items={model.correctionQueue} />
        </section>
        <section>
          <h3>Noteworthy dials</h3>
          {model.noteworthyDials.length ? (
            <div className="notedials">
              {model.noteworthyDials.map((dial) => (
                <article key={dial.id} className={`notedial ${toneClass(dial.tone)}`} title={dial.title}>
                  <span>{dial.label}</span>
                  <strong>{dial.value}</strong>
                  <small>{dial.detail}</small>
                </article>
              ))}
            </div>
          ) : (
            <p className="empty">No degraded health dials.</p>
          )}
        </section>
      </div>

      <section>
        <h3>Source freshness rail</h3>
        <div className="railgrid">
          {[...correctionSummaries, ...sourceSummaries].map((summary) => (
            <RailSummary key={summary.id} summary={summary} />
          ))}
        </div>
      </section>

      <section>
        <h3>Source health buckets</h3>
        <div className="sourcehealth">
          {(['bad', 'missing', 'warn', 'stale', 'unknown', 'fresh'] as SourceHealthBucket[]).map((bucket) => (
            <details key={bucket} open={bucket !== 'fresh' && sourceBuckets[bucket].length > 0}>
              <summary className={toneClass(sourceBuckets[bucket][0]?.tone ?? (bucket === 'fresh' ? 'good' : 'warn'))}>
                {bucketLabel(bucket)} <span>{sourceBuckets[bucket].length}</span>
              </summary>
              <SourceHealthRows items={sourceBuckets[bucket]} />
            </details>
          ))}
        </div>
      </section>
    </div>
  );
}

export function AssertionsPanel({ assertions }: { assertions: AssertionCard[] }) {
  if (!assertions.length) return <p className="empty">No state assertions reported.</p>;
  return (
    <div className="assertions">
      {assertions.map((item) => (
        <article
          key={item.id}
          className={`assertion assertion--${item.tone} assertion--conf-${item.confidence}`}
          title={item.hasCorrectionHint ? 'Backend provided correction hint' : undefined}
        >
          <header>
            <span className="assertion__label">{item.label}</span>
            <span className="assertion__confidence">{item.confidence}</span>
          </header>
          <strong className="assertion__value">{item.value}</strong>
          <ul>
            {item.evidence.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          <footer>
            <span>{item.freshnessSeconds == null ? 'freshness —' : `fresh ${formatAge(item.freshnessSeconds)}`}</span>
            {item.hasCorrectionHint ? <span className="assertion__hint">correction hint</span> : null}
          </footer>
        </article>
      ))}
    </div>
  );
}

export function AttentionPanel({ state }: { state: OpsState }) {
  const ws = state.work_state;
  const d = state.attention.desktop;
  const p = state.attention.phone;
  return (
    <dl className="facts">
      <dt>Productivity</dt>
      <dd className={ws.productivity_active ? 'ok' : 'muted'}>
        {ws.productivity_active ? 'active' : 'inactive'}
        <span className="subline">{ws.reason}</span>
      </dd>
      <dt>Active agents</dt><dd className="num">{ws.active_instance_count}</dd>
      <dt>Processing</dt><dd className="num">{ws.processing_recent_count} recent</dd>
      <dt>Observed panes</dt><dd className="num">{ws.observed_agent_count}</dd>
      <dt>Desktop</dt>
      <dd>{d.mode || '—'}<span className="subline">{d.work_mode}{d.in_meeting ? ' · in meeting' : ''}{d.location_zone ? ` · ${d.location_zone}` : ''}</span></dd>
      <dt>Phone</dt>
      <dd className={p.is_distracted ? 'bad' : ''}>
        {p.app ?? 'clear'}
        <span className="subline">{p.is_distracted ? 'distracted' : 'no distraction'} · hb {formatAge(p.heartbeat_age_seconds)}</span>
      </dd>
    </dl>
  );
}

export function EventsPanel({ events }: { events: OpsState['events'] }) {
  if (!events.length) return <p className="empty">No recent events.</p>;
  return (
    <ul className="events">
      {events.slice(0, 12).map((event, i) => {
        const summary = summarizeDetails(event.details);
        return (
          <li key={`${event.created_at}-${i}`}>
            <time>{formatTime(event.created_at)}</time>
            <span className="events__body">
              <strong>{event.event_type}</strong>
              {summary ? <span className="events__detail">{summary}</span> : null}
            </span>
            <span className="events__src">{event.device_id ?? event.instance_id ?? ''}</span>
          </li>
        );
      })}
    </ul>
  );
}

function StatusCard({
  title,
  ok,
  primary,
  rows,
}: {
  title: string;
  ok: boolean;
  primary: string;
  rows: Array<[string, string]>;
}) {
  return (
    <div className={`statuscard ${ok ? '' : 'statuscard--down'}`}>
      <header>
        <span className="statuscard__title">{title}</span>
        <span className={`led ${ok ? 'led--ok' : 'led--down'}`} />
      </header>
      <strong className="statuscard__primary">{primary}</strong>
      <dl>
        {rows.map(([k, v]) => (
          <div key={k}><dt>{k}</dt><dd>{v}</dd></div>
        ))}
      </dl>
    </div>
  );
}

export function StatusCards({ state }: { state: OpsState }) {
  const cron = state.cron;
  const tts = state.tts;
  const enf = state.enforcement;
  return (
    <div className="statuscards">
      <StatusCard
        title="Cron"
        ok={cron.available}
        primary={`${cron.enabled}/${cron.total_jobs}`}
        rows={[
          ['running', `${cron.running}`],
          ['runs/24h', `${cron.runs_last_24h}`],
          ['state', cron.available ? 'online' : cron.error ?? 'offline'],
        ]}
      />
      <StatusCard
        title="TTS"
        ok={tts.satellite_available !== false}
        primary={(tts.global_mode ?? tts.backend ?? '—').toString()}
        rows={[
          ['queue', `${tts.queue_length}`],
          ['hot / pause', `${tts.hot_queue_length} / ${tts.pause_queue_length}`],
          ['satellite', tts.satellite_available === false ? 'down' : 'up'],
        ]}
      />
      <StatusCard
        title="Enforcement"
        ok={enf.pending_count === 0}
        primary={enf.pending_count === 0 ? 'clear' : `${enf.pending_count} pending`}
        rows={[
          ['available', enf.available ? 'yes' : 'no'],
          ['pavlok', String((enf.pavlok as Record<string, unknown>)?.enabled ?? '—')],
          ['zaps today', String((enf.pavlok as Record<string, unknown>)?.zap_count ?? '—')],
        ]}
      />
    </div>
  );
}
