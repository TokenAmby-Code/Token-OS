// Read-only session-doc pipeline board. Columns are frontmatter `status`
// lanes; each card shows the title, a one-line head excerpt, and light
// metadata — never more than a head. Obsidian owns the document and is the
// single writer of `status:`; clicking a card deep-links there. The cockpit
// never mutates anything here.

import type { PipelineDoc, SessionDocsFeed } from '../types';
import { pipelineLane, type LaneVisual } from '../modes';
import { formatAge, compactPath } from '../format';

function PipelineCard({ d }: { d: PipelineDoc }) {
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
  const docs = feed.docs ?? [];
  const totals = feed.lane_totals ?? {};
  if (!docs.length) {
    return <p className="empty">No session documents registered.</p>;
  }

  const groups = new Map<string, { lane: LaneVisual; docs: PipelineDoc[] }>();
  for (const d of docs) {
    const lane = pipelineLane(d.status);
    if (!groups.has(lane.key)) groups.set(lane.key, { lane, docs: [] });
    groups.get(lane.key)!.docs.push(d);
  }
  const lanes = [...groups.values()].sort((a, b) => a.lane.order - b.lane.order);

  return (
    <div className="lanes">
      {lanes.map(({ lane, docs }) => {
        // True count from the backend (pre-cap); fall back to what we received.
        const total = totals[lane.key] ?? docs.length;
        const hidden = Math.max(0, total - docs.length);
        return (
          <section className="lane" key={lane.key} style={{ '--lane-c': lane.color } as React.CSSProperties}>
            <header className="lane__head">
              <span className="lane__dot" />
              {lane.label}
              <span className="lane__count">{total}</span>
            </header>
            <div className="lane__cards">
              {docs.map((d) => (
                <PipelineCard key={d.id ?? d.path ?? d.title} d={d} />
              ))}
              {hidden > 0 ? (
                <p className="lane__more">+{hidden} more · open in Obsidian</p>
              ) : null}
            </div>
          </section>
        );
      })}
    </div>
  );
}
