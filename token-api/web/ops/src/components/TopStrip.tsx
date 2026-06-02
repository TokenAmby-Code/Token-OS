// The persistent glanceable HUD: the <2s "am I working / in debt / distracted /
// blocked?" readout, rendered as a cluster of floating circular gauges that
// live in the fixed top bar (bound to the viewport, never to scroll position).
// Magnitudes become arcs; enum/bool states become center glyphs (per the rule
// that app *names* — free text — get a short label, everything else a gauge).

import type { OpsState } from '../types';
import { modeVisual, desktopGlyph, phoneGlyph } from '../modes';
import { formatSignedClock, formatClock } from '../format';
import { Ring } from './Ring';

// One banked hour of break fills the ring; debt fills it in hazard.
const BREAK_SCALE_MS = 60 * 60 * 1000;

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

export function HudRings({ state }: { state: OpsState }) {
  const mv = modeVisual(state.timer.mode);
  const bal = state.timer.break_balance_ms;
  const debt = state.timer.is_in_backlog;
  const breakRatio = Math.min(1, Math.abs(bal) / BREAK_SCALE_MS);

  const desk = state.attention.desktop;
  const phone = state.attention.phone;
  const pending = state.enforcement.pending_count;
  const active = state.instances.counts.active;
  const stale = state.instances.counts.stale;

  const alarmAcked = state.alarm?.acked ?? false;
  const alarmTime = state.alarm?.day_started_at ? formatClock(state.alarm.day_started_at) : null;

  const deskLabel = desk.steam_app_name
    ? truncate(desk.steam_app_name, 11)
    : desk.work_mode || desk.mode || '—';
  const phoneLabel = phone.app ? truncate(phone.app, 11) : 'clear';

  return (
    <div className="rings">
      <Ring
        label="Timer"
        glyph={mv.glyph}
        detail={mv.label.toLowerCase()}
        color={mv.color}
        title={`Timer mode · ${mv.label}`}
      />
      <Ring
        label="Break"
        value={formatSignedClock(bal)}
        detail={debt ? 'in debt' : 'banked'}
        color={debt ? 'var(--hazard)' : 'var(--phosphor)'}
        ratio={breakRatio}
        tone={debt ? 'bad' : 'good'}
        title="Break balance"
      />
      <Ring
        label="Desktop"
        glyph={desktopGlyph(desk)}
        detail={deskLabel}
        color="var(--cyan)"
        tone={desk.steam_app_name ? 'warn' : 'neutral'}
        title={`Desktop · ${deskLabel}`}
      />
      <Ring
        label="Phone"
        glyph={phoneGlyph(phone)}
        detail={phoneLabel}
        color={phone.is_distracted ? 'var(--hazard)' : 'var(--muted)'}
        tone={phone.is_distracted ? 'bad' : 'neutral'}
        title={`Phone · ${phoneLabel}`}
      />
      <Ring
        label="Fleet"
        value={`${active}`}
        detail={stale > 0 ? `${stale} stale` : 'all fresh'}
        color="var(--brass)"
        ratio={active > 0 ? 1 - Math.min(1, stale / active) : undefined}
        tone={stale > 0 ? 'warn' : 'good'}
        title="Active fleet"
      />
      <Ring
        label="Enforce"
        value={`${pending}`}
        detail={`tts q ${state.tts.queue_length}`}
        color={pending > 0 ? 'var(--hazard)' : 'var(--muted)'}
        ratio={pending > 0 ? 1 : undefined}
        tone={pending > 0 ? 'bad' : 'neutral'}
        pulse={pending > 0}
        title="Enforcement pending"
      />
      <Ring
        label="Alarm"
        glyph={alarmAcked ? '✓' : '○'}
        detail={alarmAcked ? (alarmTime ?? 'acked') : 'pending'}
        color={alarmAcked ? 'var(--phosphor)' : 'var(--muted)'}
        tone={alarmAcked ? 'good' : 'neutral'}
        title="Alarm ack — tap to simulate"
        onClick={alarmAcked ? undefined : () => { fetch('/api/alarm/ack', { method: 'POST' }); }}
      />
    </div>
  );
}
