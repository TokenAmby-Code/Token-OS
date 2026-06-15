// Read-only session-doc pipeline board. Columns are frontmatter `status`
// lanes; each card shows the title, a one-line head excerpt, and light
// metadata — never more than a head. Obsidian owns the document and is the
// single writer of `status:`; clicking a card deep-links there. The cockpit
// never mutates anything here.

import { useMemo, useState } from 'react';
import type { PipelineDoc, SessionDocsFeed } from '../types';
import { pipelineLane, type LaneVisual } from '../modes';
import { formatAge, compactPath } from '../format';
import { openSessionDoc } from '../api';

type DateScope = 'today' | 'all';

const OPS_LOCAL_TIME_ZONE = 'America/Denver';

const denverDateFormatter = new Intl.DateTimeFormat('en-CA', {
  timeZone: OPS_LOCAL_TIME_ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
});

function datePartsToKey(parts: Intl.DateTimeFormatPart[]): string | null {
  const year = parts.find((part) => part.type === 'year')?.value;
  const month = parts.find((part) => part.type === 'month')?.value;
  const day = parts.find((part) => part.type === 'day')?.value;
  return year && month && day ? `${year}-${month}-${day}` : null;
}

function localDateKey(date: Date): string | null {
  return datePartsToKey(denverDateFormatter.formatToParts(date));
}

function hasExplicitOffset(value: string): boolean {
  return /(?:z|[+-]\d{2}:?\d{2})$/i.test(value.trim());
}

function docDateKey(value: string | null | undefined): string | null {
  if (!value) return null;
  const raw = String(value).trim();
  if (!raw) return null;

  // Date-only and offset-less ISO-ish timestamps in session frontmatter are
  // authored as local document dates. Keep their calendar date literal rather
  // than feeding them through Date, which would introduce UTC/local shifts.
  const datePrefix = raw.match(/^(\d{4}-\d{2}-\d{2})/);
  if (datePrefix && !hasExplicitOffset(raw)) return datePrefix[1];

  const parsed = new Date(raw.replace(' ', 'T'));
  if (Number.isNaN(parsed.getTime())) return datePrefix?.[1] ?? null;
  return localDateKey(parsed);
}

function pipelineDocDate(d: PipelineDoc): string | null {
  return docDateKey(d.session_date ?? d.created_at);
}

function laneCounts(docs: PipelineDoc[]): Record<string, number> {
  return docs.reduce<Record<string, number>>((acc, doc) => {
    const key = pipelineLane(doc.status).key;
    acc[key] = (acc[key] ?? 0) + 1;
    return acc;
  }, {});
}

function PipelineCard({
  d,
  selected,
  onSelect,
}: {
  d: PipelineDoc;
  selected: boolean;
  onSelect: (doc: PipelineDoc) => void;
}) {
  const title = d.title ?? (d.path ? compactPath(d.path) : 'untitled');
  const body = (
    <>
      <strong className="pcard__title">{title}</strong>
      {d.head ? <p className="pcard__head">{d.head}</p> : null}
      <div className="pcard__meta">
        {d.project ? <span className="pcard__chip">{d.project}</span> : null}
        {d.linked_instances > 0 ? (
          <span className="pcard__chip pcard__chip--live">◉ {d.linked_instances}</span>
        ) : null}
        <span className="pcard__age">{formatAge(d.age_seconds)}</span>
      </div>
    </>
  );

  // A doc with a stable id selects + opens through the one open-by-id endpoint
  // (server-side obsidian CLI) — the same funnel the tmux `prefix + S` keybind
  // and the fleet-row double-click use. We route the open *through* Token-API
  // rather than deep-linking the browser at obsidian://, so the open path stays
  // single and server-driven. (Selecting highlights the card on top of that.)
  if (d.id != null) {
    return (
      <button
        type="button"
        className={`pcard pcard--openable${selected ? ' is-selected' : ''}`}
        aria-pressed={selected}
        title="Open in Obsidian"
        onClick={() => onSelect(d)}
      >
        {body}
        <span className="pcard__open">↗ obsidian</span>
      </button>
    );
  }
  // No id (can't reach the endpoint) but a direct deep link exists: fall back to
  // the obsidian:// anchor so the card is still openable.
  if (d.obsidian_uri) {
    return (
      <a className="pcard" href={d.obsidian_uri} title="Open in Obsidian">
        {body}
        <span className="pcard__open">↗ obsidian</span>
      </a>
    );
  }
  return <div className="pcard pcard--static">{body}</div>;
}

export function PipelinePanel({ feed }: { feed: SessionDocsFeed }) {
  const [scope, setScope] = useState<DateScope>('today');
  // Single-select: the one selected card is highlighted. Selecting also opens
  // the doc in Obsidian via the shared open-by-id endpoint (read-only cockpit —
  // the server runs the open; we never mutate the document).
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const onSelect = (doc: PipelineDoc) => {
    if (doc.id == null) return;
    setSelectedId(doc.id);
    void openSessionDoc(doc.id).catch(() => {});
  };
  const allDocs = feed.docs ?? [];
  const todayKey = localDateKey(new Date());
  const docs = useMemo(() => {
    if (scope === 'all') return allDocs;
    if (!todayKey) return [];
    return allDocs.filter((doc) => pipelineDocDate(doc) === todayKey);
  }, [allDocs, scope, todayKey]);
  const totals = scope === 'all' ? (feed.lane_totals ?? {}) : laneCounts(docs);
  const visibleCount = docs.length;
  const allCount = allDocs.length;

  const controls = (
    <div className="pipeline__toolbar">
      <div className="pipeline__scope" role="group" aria-label="Session date scope">
        <button
          type="button"
          className={`pipeline__scope-btn ${scope === 'today' ? 'is-active' : ''}`}
          aria-pressed={scope === 'today'}
          onClick={() => setScope('today')}
        >
          Today
        </button>
        <button
          type="button"
          className={`pipeline__scope-btn ${scope === 'all' ? 'is-active' : ''}`}
          aria-pressed={scope === 'all'}
          onClick={() => setScope('all')}
        >
          All
        </button>
      </div>
      <span className="pipeline__scope-meta">
        {scope === 'today' ? `${visibleCount}/${allCount} cards · ${todayKey ?? 'local today'}` : `${allCount} cards`}
      </span>
    </div>
  );

  if (!allDocs.length) {
    return (
      <>
        {controls}
        <p className="empty">No session documents registered.</p>
      </>
    );
  }

  if (!docs.length) {
    return (
      <>
        {controls}
        <p className="empty">No session documents for today. Switch to All to see historical cards.</p>
      </>
    );
  }

  const groups = new Map<string, { lane: LaneVisual; docs: PipelineDoc[] }>();
  for (const d of docs) {
    const lane = pipelineLane(d.status);
    if (!groups.has(lane.key)) groups.set(lane.key, { lane, docs: [] });
    groups.get(lane.key)!.docs.push(d);
  }
  const lanes = [...groups.values()].sort((a, b) => a.lane.order - b.lane.order);

  return (
    <>
      {controls}
      <div className="lanes">
        {lanes.map(({ lane, docs }) => {
          // True count from the backend (pre-cap) in All mode; filtered count in Today mode.
          const total = totals[lane.key] ?? docs.length;
          const hidden = scope === 'all' ? Math.max(0, total - docs.length) : 0;
          return (
            <section className="lane" key={lane.key} style={{ '--lane-c': lane.color } as React.CSSProperties}>
              <header className="lane__head">
                <span className="lane__dot" />
                {lane.label}
                <span className="lane__count">{total}</span>
              </header>
              <div className="lane__cards">
                {docs.map((d) => (
                  <PipelineCard
                    key={d.id ?? d.path ?? d.title}
                    d={d}
                    selected={d.id != null && d.id === selectedId}
                    onSelect={onSelect}
                  />
                ))}
                {hidden > 0 ? (
                  <p className="lane__more">+{hidden} more · open in Obsidian</p>
                ) : null}
              </div>
            </section>
          );
        })}
      </div>
    </>
  );
}
