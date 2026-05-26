// Persistent glanceable strip: the <2s "am I working / in debt / distracted /
// blocked?" readout. Single row on desktop, wraps to a grid on narrow frames.

import type { OpsState } from '../types';
import { modeVisual } from '../modes';
import { formatSignedDuration } from '../format';

function Stat({
  label,
  value,
  detail,
  tone,
  accent,
}: {
  label: string;
  value: string;
  detail?: string;
  tone?: 'good' | 'bad' | 'warn' | 'neutral';
  accent?: string;
}) {
  return (
    <div className={`stat ${tone ? `stat--${tone}` : ''}`}>
      <span className="stat__label">{label}</span>
      <strong className="stat__value" style={accent ? { color: accent } : undefined}>{value}</strong>
      {detail ? <span className="stat__detail">{detail}</span> : null}
    </div>
  );
}

export function TopStrip({ state }: { state: OpsState }) {
  const mv = modeVisual(state.timer.mode);
  const debt = state.timer.is_in_backlog;
  const phoneDistracted = state.attention.phone.is_distracted;
  const pending = state.enforcement.pending_count;

  return (
    <div className="strip">
      <div className="strip__mode" style={{ '--mode-c': mv.color } as React.CSSProperties}>
        <span className="strip__glyph">{mv.glyph}</span>
        <div>
          <span className="stat__label">Timer</span>
          <strong className="strip__modename">{mv.label}</strong>
        </div>
      </div>

      <Stat
        label="Break balance"
        value={formatSignedDuration(state.timer.break_balance_ms)}
        detail={debt ? 'in debt' : 'available'}
        tone={debt ? 'bad' : 'good'}
      />
      <Stat
        label="Desktop"
        value={state.attention.desktop.mode || '—'}
        detail={state.attention.desktop.steam_app_name ?? state.attention.desktop.work_mode}
      />
      <Stat
        label="Phone"
        value={state.attention.phone.app ?? 'clear'}
        detail={phoneDistracted ? 'distracted' : 'no distraction'}
        tone={phoneDistracted ? 'bad' : 'neutral'}
      />
      <Stat
        label="Fleet"
        value={`${state.instances.counts.active}`}
        detail={`${state.instances.counts.stale} stale`}
        tone={state.instances.counts.stale > 0 ? 'warn' : 'good'}
      />
      <Stat
        label="Enforcement"
        value={`${pending}`}
        detail={`tts q ${state.tts.queue_length}`}
        tone={pending > 0 ? 'bad' : 'neutral'}
      />
    </div>
  );
}
