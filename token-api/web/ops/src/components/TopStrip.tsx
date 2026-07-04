// The persistent glanceable HUD: the <2s "am I working / in debt / distracted /
// blocked?" readout, rendered as a cluster of floating circular gauges that
// live in the fixed top bar (bound to the viewport, never to scroll position).
// Magnitudes become arcs; enum/bool states become center glyphs (per the rule
// that app *names* — free text — get a short label, everything else a gauge).

import { useEffect, useRef, useState } from 'react';
import type { OpsState } from '../types';
import { modeVisual, desktopGlyph, phoneGlyph } from '../modes';
import { formatSignedClock, formatClock } from '../format';
import { ackAlarm, clearPhoneAttention, endMorningSession } from '../api';
import type { CockpitLayoutModel, CockpitTone } from '../layoutModel';
import { Ring } from './Ring';

// One banked hour of break fills the ring; debt fills it in hazard.
const BREAK_SCALE_MS = 60 * 60 * 1000;

// Two-tap arm window for the "I'm not on my phone" clear: first tap arms the
// Phone dial (glyph → "clear?"), a second tap within this window commits, and
// otherwise it silently disarms — no accidental one-tap clears.
const PHONE_CLEAR_ARM_MS = 3000;

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

// Work-action staleness fade endpoints (RGB), interpolated each poll.
const WA_FRESH: [number, number, number] = [147, 217, 79]; // --m-working
const WA_STALE: [number, number, number] = [255, 91, 61]; // --hazard

function waFadeColor(ratio: number): string {
  const mix = (a: number, b: number) => Math.round(a + (b - a) * ratio);
  return `rgb(${mix(WA_FRESH[0], WA_STALE[0])}, ${mix(WA_FRESH[1], WA_STALE[1])}, ${mix(WA_FRESH[2], WA_STALE[2])})`;
}

function waAgo(minutes: number): string {
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${Math.round(minutes)}m ago`;
  const h = Math.floor(minutes / 60);
  return `${h}h ${Math.round(minutes % 60).toString().padStart(2, '0')}m`;
}

function toneColor(tone: CockpitTone): string {
  if (tone === 'bad') return 'var(--hazard)';
  if (tone === 'warn') return 'var(--brass-bright)';
  if (tone === 'good') return 'var(--phosphor)';
  return 'var(--muted)';
}

export function HudRings({ state, layout }: { state: OpsState; layout?: CockpitLayoutModel }) {
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

  // Work-action dial #1 (load-bearing): today's count, with a green→red fade as
  // the last explicit action goes stale — the "log a work action" nudge.
  const wa = state.work_actions;
  const waFadeMin = wa?.stale_fade_minutes || 30;
  const waLastMs = wa?.last_at ? Date.parse(wa.last_at) : NaN;
  const waHasLast = Number.isFinite(waLastMs);
  const waMinsSince = waHasLast ? Math.max(0, (Date.now() - waLastMs) / 60000) : null;
  const waStale = waMinsSince == null ? 0 : Math.max(0, Math.min(1, waMinsSince / waFadeMin));
  const waColor = waHasLast ? waFadeColor(waStale) : 'var(--muted)';
  const waDetail = !waHasLast ? 'none today' : waStale >= 1 ? 'log one' : waAgo(waMinsSince ?? 0);
  const waTone: 'good' | 'warn' | 'bad' | 'neutral' = !waHasLast
    ? 'neutral'
    : waStale >= 1
      ? 'bad'
      : waStale >= 0.5
        ? 'warn'
        : 'good';

  const deskLabel = desk.steam_app_name
    ? truncate(desk.steam_app_name, 11)
    : desk.work_mode || desk.mode || '—';
  const phoneLabel = phone.app ? truncate(phone.app, 11) : 'clear';

  // Phone dial doubles as the "I'm not on my phone" clear: arm on first tap,
  // commit on a second tap within PHONE_CLEAR_ARM_MS, else auto-disarm.
  const [phoneArmed, setPhoneArmed] = useState(false);
  const phoneArmTimer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(phoneArmTimer.current), []);
  function tapPhoneDial() {
    if (phoneArmed) {
      window.clearTimeout(phoneArmTimer.current);
      setPhoneArmed(false);
      clearPhoneAttention().catch((err) => console.error('phone clear failed', err));
      return;
    }
    setPhoneArmed(true);
    phoneArmTimer.current = window.setTimeout(() => setPhoneArmed(false), PHONE_CLEAR_ARM_MS);
  }

  function tapTimerDial() {
    if (state.timer.mode !== 'morning_session') return;
    endMorningSession().catch((err) => console.error('morning session end failed', err));
  }

  return (
    <div className="rings">
      <Ring
        label="Timer"
        glyph={mv.glyph}
        detail={state.timer.mode === 'morning_session' ? 'tap to end' : mv.label.toLowerCase()}
        color={mv.color}
        title={
          state.timer.mode === 'morning_session'
            ? 'Timer mode · MORNING · tap to end morning session'
            : `Timer mode · ${mv.label}`
        }
        onClick={state.timer.mode === 'morning_session' ? tapTimerDial : undefined}
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
        glyph={phoneArmed ? undefined : phoneGlyph(phone)}
        value={phoneArmed ? 'clear?' : undefined}
        detail={phoneArmed ? 'tap to confirm' : phoneLabel}
        color={phoneArmed ? 'var(--brass)' : phone.is_distracted ? 'var(--hazard)' : 'var(--muted)'}
        tone={phoneArmed ? 'warn' : phone.is_distracted ? 'bad' : 'neutral'}
        title={`Phone · ${phoneLabel} · tap: I'm not on my phone (2-tap clear, no zap)`}
        onClick={tapPhoneDial}
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
        onClick={alarmAcked ? undefined : () => { ackAlarm().catch((err) => console.error('alarm ack failed', err)); }}
      />
      {wa ? (
        <Ring
          label="Work"
          value={`${wa.count}`}
          detail={waDetail}
          color={waColor}
          ratio={waHasLast ? waStale : undefined}
          tone={waTone}
          pulse={waStale >= 1}
          title={
            waHasLast
              ? `${wa.count} work action${wa.count === 1 ? '' : 's'} today · last ${formatClock(wa.last_at)}`
              : 'No work actions logged today'
          }
        />
      ) : null}
      {typeof wa?.score === 'number' ? (
        <Ring
          label="Activity"
          value={`${wa.score}`}
          detail="all signals"
          color="var(--brass)"
          title="Aggregate work signals today (non-load-bearing)"
        />
      ) : null}
      {layout?.noteworthyDials.map((dial) => (
        <Ring
          key={dial.id}
          label={dial.label}
          value={dial.value}
          detail={dial.detail}
          color={toneColor(dial.tone)}
          tone={dial.tone}
          pulse={dial.tone === 'bad'}
          title={dial.title}
        />
      ))}
    </div>
  );
}
