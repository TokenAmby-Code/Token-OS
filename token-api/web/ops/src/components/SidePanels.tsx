// Compact secondary panels: attention evidence, events timeline, and the
// cron / Golden Throne / enforcement status cards.

import type { OpsState, StateAssertion } from '../types';
import { formatAge, formatTime, summarizeDetails } from '../format';

export function AssertionsPanel({
  state,
  assertions,
  compact = false,
}: {
  state: OpsState;
  assertions?: StateAssertion[];
  compact?: boolean;
}) {
  const items = assertions ?? state.assertions ?? [];
  if (!items.length) return <p className="empty">No noteworthy state assertions.</p>;
  return (
    <div className={`assertions ${compact ? 'assertions--compact' : ''}`}>
      {items.map((item) => (
        <article
          key={item.id}
          className={`assertion assertion--${item.status} assertion--conf-${item.confidence}`}
          title={item.correction_hint ?? undefined}
        >
          <header>
            <span className="assertion__label">{item.label}</span>
            <span className="assertion__confidence">{item.confidence}</span>
          </header>
          <strong className="assertion__value">{item.value}</strong>
          <ul>
            {item.evidence.slice(0, 3).map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          <footer>
            <span>{item.freshness_seconds == null ? 'freshness —' : `fresh ${formatAge(item.freshness_seconds)}`}</span>
            {item.correction_hint ? <span className="assertion__hint">{item.correction_hint}</span> : null}
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
