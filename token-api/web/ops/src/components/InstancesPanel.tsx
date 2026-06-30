// Active fleet. Table on desktop, card stack on mobile (same data, one source).
//
// Selection: double-clicking a row selects + expands that instance (feature A);
// the currently-talking instance is auto-selected (feature B). Both feed the
// SAME selection state (owned by App) and render the SAME <ExpandedInstance>
// card — one "select + expand" mechanism, not two. Manual selection also POSTs
// /api/instances/{id}/focus-pane so the tmux pane is focused + marked.

import type { OpsInstance, TtsCurrent } from '../types';
import { statusTone, zealotryTone } from '../modes';
import { formatAge, formatTime, compactPath } from '../format';
import { openSessionDoc } from '../api';

export type Selection = { id: string; source: 'manual' | 'talking' };

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
  const docId = doc.id;
  const openable = docId != null;
  // Double-click opens the doc in Obsidian via the one open-by-id endpoint
  // (server-side obsidian CLI) — the same path the tmux `prefix + S` keybind takes.
  const onDoubleClick = openable
    ? () => {
        void openSessionDoc(docId).catch(() => {});
      }
    : undefined;
  return (
    <span
      className={openable ? 'sessiondoc is-openable' : 'sessiondoc'}
      onDoubleClick={onDoubleClick}
      title={openable ? 'Double-click to open in Obsidian' : undefined}
    >
      {title}
      <span className="subline">{doc.status ?? doc.policy ?? doc.binding_source ?? ''}</span>
    </span>
  );
}

function StatusPill({ status }: { status: string }) {
  return <span className={`pill pill--${statusTone(status)}`}>{status}</span>;
}

// 40k persona identity, tinted by its canonical hex shade. The persona
// name implies the voice (persona<->voice is 1:1), so this replaces any raw voice
// surface. null (e.g. a legacy pre-rename profile_name) renders a muted dash.
function ChapterChip({ inst }: { inst: OpsInstance }) {
  if (!inst.persona?.display_name) return <span className="faint">—</span>;
  return (
    <span
      className="persona-chip"
      style={{ '--chip': inst.persona?.chip_color ?? 'var(--muted)' } as React.CSSProperties}
      title={inst.persona?.display_name}
    >
      <span className="persona-chip__dot" />
      {inst.persona?.display_name}
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

// Live state of the expanded instance: talking right now vs. just selected. Driven
// by whether it is the current speaker (not by how it was selected), so the tag
// stays truthful if a talking-selected instance falls silent.
function SourceTag({ isTalking }: { isTalking: boolean }) {
  return (
    <span className={`xi__src xi__src--${isTalking ? 'talking' : 'manual'}`}>
      {isTalking ? '🔊 talking' : '◆ selected'}
    </span>
  );
}

// The expanded instance view — shared by feature A (manual) and feature B
// (talking). One card, regardless of how the instance became selected.
function ExpandedInstance({
  inst,
  isTalking,
  current,
  focusNote,
  onClear,
}: {
  inst: OpsInstance;
  isTalking: boolean;
  current: TtsCurrent | null;
  focusNote: string | null;
  onClear: () => void;
}) {
  return (
    <article className={`xi ${isTalking ? 'xi--talking' : ''}`}>
      <header className="xi__head">
        <div className="xi__id">
          <strong>{inst.display_name}</strong>
          <PrBadge inst={inst} />
          <ChapterChip inst={inst} />
          <StatusPill status={inst.status} />
          <SourceTag isTalking={isTalking} />
        </div>
        <button type="button" className="xi__close" onClick={onClear} title="collapse">
          ✕
        </button>
      </header>

      <span className="subline">
        {inst.engine} · {inst.device_id ?? '—'} · {inst.persona?.slug ?? 'no persona'}
        {inst.is_subagent ? ' · subagent' : ''}
      </span>

      {isTalking && current ? (
        <div className="xi__speaking">
          <span className="xi__speaking-led" aria-hidden />
          <span className="xi__speaking-msg" title={current.message}>{current.message}</span>
          <span className="xi__speaking-meta">
            {current.voice ?? '—'}
            {current.backend ? ` · ${current.backend}` : ''}
          </span>
        </div>
      ) : null}

      <div className="xi__grid">
        <div><span className="k">fervor</span><Zealotry value={inst.zealotry} /></div>
        <div>
          <span className="k">age</span>
          {formatAge(inst.age_seconds)}
          {inst.stale.is_stale ? <span className="tag tag--stale">stale</span> : null}
        </div>
        <div><span className="k">pane</span>{inst.runtime?.role ?? inst.runtime?.pane_id ?? '—'}</div>
        <div><span className="k">Golden Throne</span><GtCell inst={inst} /></div>
        <div className="xi__wide"><span className="k">working dir</span><code>{inst.working_dir ?? '—'}</code></div>
        <div className="xi__wide"><span className="k">session doc</span><SessionDocCell inst={inst} /></div>
        <div className="xi__wide"><span className="k">next action</span>{inst.next_required_action ?? inst.workflow_state ?? '—'}</div>
      </div>

      {focusNote ? <p className="xi__note">{focusNote}</p> : null}
    </article>
  );
}

export function InstancesPanel({
  instances,
  selection,
  talkingId,
  current,
  focusNote,
  onSelect,
  onClear,
}: {
  instances: OpsInstance[];
  selection: Selection | null;
  talkingId: string | null;
  current: TtsCurrent | null;
  focusNote: string | null;
  onSelect: (id: string) => void;
  onClear: () => void;
}) {
  if (instances.length === 0) {
    return <p className="empty">No active instances reported. The fleet is dormant.</p>;
  }

  const selectedInst = selection ? instances.find((i) => i.id === selection.id) ?? null : null;

  function marks(inst: OpsInstance): string {
    const cls: string[] = [];
    if (inst.stale.is_stale) cls.push('isStale');
    if (selection?.id === inst.id) cls.push('is-selected');
    if (talkingId === inst.id) cls.push('is-talking');
    return cls.join(' ');
  }

  return (
    <>
      {selectedInst && selection ? (
        <ExpandedInstance
          inst={selectedInst}
          isTalking={talkingId === selectedInst.id}
          current={current}
          focusNote={focusNote}
          onClear={onClear}
        />
      ) : null}

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
              <tr
                key={inst.id}
                className={marks(inst)}
                onDoubleClick={() => onSelect(inst.id)}
                title="double-click to select + expand"
              >
                <td>
                  <strong>{inst.display_name}</strong> <PrBadge inst={inst} />
                  <span className="subline">{inst.engine} · {inst.device_id ?? '—'} · {inst.persona?.slug ?? 'no persona'}</span>
                </td>
                <td><ChapterChip inst={inst} /></td>
                <td><StatusPill status={inst.status} /></td>
                <td className="num">
                  {formatAge(inst.age_seconds)}
                  {inst.stale.is_stale ? <span className="tag tag--stale">stale</span> : null}
                </td>
                <td><Zealotry value={inst.zealotry} /></td>
                <td>
                  {inst.runtime?.role ?? inst.runtime?.pane_id ?? '—'}
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
          <article
            key={inst.id}
            className={`fcard ${marks(inst)}`}
            onDoubleClick={() => onSelect(inst.id)}
          >
            <header className="fcard__head">
              <strong>{inst.display_name}</strong> <PrBadge inst={inst} />
              <StatusPill status={inst.status} />
            </header>
            <span className="subline">{inst.engine} · {inst.device_id ?? '—'} · {inst.persona?.slug ?? 'no persona'}</span>
            <div className="fcard__grid">
              <div><span className="k">persona</span><ChapterChip inst={inst} /></div>
              <div><span className="k">age</span>{formatAge(inst.age_seconds)}{inst.stale.is_stale ? ' · stale' : ''}</div>
              <div><span className="k">fervor</span><Zealotry value={inst.zealotry} /></div>
              <div><span className="k">pane</span>{inst.runtime?.role ?? inst.runtime?.pane_id ?? '—'}</div>
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
