// Active fleet. Table on desktop, card stack on mobile (same data, one source).

import type { OpsInstance } from '../types';
import { statusTone, zealotryTone } from '../modes';
import { formatAge, formatTime, compactPath } from '../format';

function Zealotry({ value }: { value: number }) {
  const tone = zealotryTone(value);
  return (
    <span className={`zeal zeal--${tone}`} title={`zealotry ${value}`}>
      {Array.from({ length: 3 }).map((_, i) => (
        <span key={i} className={`zeal__pip ${i < value ? 'is-lit' : ''}`} />
      ))}
    </span>
  );
}

function GtCell({ inst }: { inst: OpsInstance }) {
  if (inst.gt.victory_at) {
    return <span className="gt gt--victory" title={inst.gt.victory_reason ?? 'victory'}>✦ victory</span>;
  }
  if (inst.gt.next_fire) {
    return (
      <span className="gt gt--armed">
        ⟳ {formatTime(inst.gt.next_fire)}
        <span className="subline">resumes {inst.gt.resume_count}</span>
      </span>
    );
  }
  return <span className="gt gt--quiet">—</span>;
}

function SessionDocCell({ inst }: { inst: OpsInstance }) {
  const doc = inst.session_doc;
  const title = doc.title ?? (doc.path ? compactPath(doc.path) : null);
  if (!title) return <span className="faint">unbound</span>;
  return (
    <span>
      {title}
      <span className="subline">{doc.status ?? doc.policy ?? doc.binding_source ?? ''}</span>
    </span>
  );
}

function StatusPill({ status }: { status: string }) {
  return <span className={`pill pill--${statusTone(status)}`}>{status}</span>;
}

// 40k chapter / persona identity, tinted by its canonical hex shade. The chapter
// name implies the voice (chapter<->voice is 1:1), so this replaces any raw voice
// surface. null (e.g. a legacy pre-rename profile_name) renders a muted dash.
function ChapterChip({ inst }: { inst: OpsInstance }) {
  if (!inst.chapter) return <span className="faint">—</span>;
  return (
    <span
      className="chapter-chip"
      style={{ '--chip': inst.chapter_color ?? '#8a8f98' } as React.CSSProperties}
      title={inst.chapter}
    >
      <span className="chapter-chip__dot" />
      {inst.chapter}
    </span>
  );
}

// "Agent has a PR open" badge (Phase 1). Links to the PR when open; shows a muted
// state once the CD webhook flips pr_state→merged (Phase 2). Renders nothing otherwise.
function PrBadge({ inst }: { inst: OpsInstance }) {
  if (inst.pr_state !== 'open' && inst.pr_state !== 'merged') return null;
  const label = inst.pr_state === 'merged' ? '⛗ merged' : '⇡ PR open';
  const cls = `tag tag--pr tag--pr-${inst.pr_state}`;
  if (inst.pr_url) {
    return (
      <a className={cls} href={inst.pr_url} target="_blank" rel="noreferrer" title={inst.pr_url}>
        {label}
      </a>
    );
  }
  return <span className={cls}>{label}</span>;
}

export function InstancesPanel({ instances }: { instances: OpsInstance[] }) {
  if (instances.length === 0) {
    return <p className="empty">No active instances reported. The fleet is dormant.</p>;
  }
  return (
    <>
      {/* Desktop table */}
      <div className="tableWrap only-wide">
        <table className="fleet">
          <thead>
            <tr>
              <th>Instance</th>
              <th>Chapter</th>
              <th>Status</th>
              <th>Age</th>
              <th>Fervor</th>
              <th>Pane</th>
              <th>Session doc</th>
              <th>Golden Throne</th>
              <th>Next action</th>
            </tr>
          </thead>
          <tbody>
            {instances.map((inst) => (
              <tr key={inst.id} className={inst.stale.is_stale ? 'isStale' : ''}>
                <td>
                  <strong>{inst.display_name}</strong> <PrBadge inst={inst} />
                  <span className="subline">{inst.engine} · {inst.device_id ?? '—'} · {inst.legion ?? 'no legion'}</span>
                </td>
                <td><ChapterChip inst={inst} /></td>
                <td><StatusPill status={inst.status} /></td>
                <td className="num">
                  {formatAge(inst.age_seconds)}
                  {inst.stale.is_stale ? <span className="tag tag--stale">stale</span> : null}
                </td>
                <td><Zealotry value={inst.zealotry} /></td>
                <td>
                  {inst.pane_label ?? inst.tmux_pane ?? '—'}
                  <span className="subline">{compactPath(inst.working_dir)}</span>
                </td>
                <td><SessionDocCell inst={inst} /></td>
                <td><GtCell inst={inst} /></td>
                <td className="action">{inst.next_required_action ?? inst.workflow_state ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Mobile cards */}
      <div className="fleet-cards only-narrow">
        {instances.map((inst) => (
          <article key={inst.id} className={`fcard ${inst.stale.is_stale ? 'isStale' : ''}`}>
            <header className="fcard__head">
              <strong>{inst.display_name}</strong> <PrBadge inst={inst} />
              <StatusPill status={inst.status} />
            </header>
            <span className="subline">{inst.engine} · {inst.device_id ?? '—'} · {inst.legion ?? 'no legion'}</span>
            <div className="fcard__grid">
              <div><span className="k">chapter</span><ChapterChip inst={inst} /></div>
              <div><span className="k">age</span>{formatAge(inst.age_seconds)}{inst.stale.is_stale ? ' · stale' : ''}</div>
              <div><span className="k">fervor</span><Zealotry value={inst.zealotry} /></div>
              <div><span className="k">pane</span>{inst.pane_label ?? inst.tmux_pane ?? '—'}</div>
              <div><span className="k">GT</span><GtCell inst={inst} /></div>
              <div className="fcard__wide"><span className="k">doc</span><SessionDocCell inst={inst} /></div>
              <div className="fcard__wide"><span className="k">next</span>{inst.next_required_action ?? inst.workflow_state ?? '—'}</div>
            </div>
          </article>
        ))}
      </div>
    </>
  );
}
