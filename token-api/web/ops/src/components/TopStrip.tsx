// The persistent glanceable HUD: the <2s "am I working / in debt / distracted /
// blocked?" readout, rendered as a cluster of floating circular gauges that
// live in the fixed top bar (bound to the viewport, never to scroll position).
// Magnitudes become arcs; enum/bool states become center glyphs (per the rule
// that app *names* — free text — get a short label, everything else a gauge).

import { useEffect, useMemo, useRef, useState } from 'react';
import type { OpsState } from '../types';
import type { CockpitLayoutModel, NoteworthyDial } from '../layoutModel';
import { buildCockpitLayoutModel } from '../layoutModel';
import { ackAlarm, clearPhoneAttention, endMorningSession } from '../api';
import { Ring } from './Ring';

// Two-tap arm window for the "I'm not on my phone" clear: first tap arms the
// Phone dial (glyph → "clear?"), a second tap within this window commits, and
// otherwise it silently disarms — no accidental one-tap clears.
const PHONE_CLEAR_ARM_MS = 3000;

function phoneArmedDial(dial: NoteworthyDial): NoteworthyDial {
  return {
    ...dial,
    tone: 'warn',
    reason: 'phone clear confirmation armed',
    render: {
      ...dial.render,
      glyph: undefined,
      value: 'clear?',
      detail: 'tap to confirm',
      color: 'var(--brass)',
      title: 'Phone clear armed — tap again to confirm',
    },
  };
}

export function HudRings({ state, layout }: { state: OpsState; layout?: CockpitLayoutModel }) {
  const model = useMemo(() => layout ?? buildCockpitLayoutModel(state), [layout, state]);

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

  function tapAlarmDial() {
    if (state.alarm?.acked ?? true) return;
    ackAlarm().catch((err) => console.error('alarm ack failed', err));
  }

  function handlerFor(dial: NoteworthyDial): (() => void) | undefined {
    if (dial.id === 'timer' && state.timer.mode === 'morning_session') return tapTimerDial;
    if (dial.id === 'phone') return tapPhoneDial;
    if (dial.id === 'alarm' && !(state.alarm?.acked ?? true)) return tapAlarmDial;
    return undefined;
  }

  return (
    <div className="rings">
      {model.noteworthyDials.map((sourceDial) => {
        const dial = sourceDial.id === 'phone' && phoneArmed ? phoneArmedDial(sourceDial) : sourceDial;
        return (
          <Ring
            key={dial.id}
            label={dial.label}
            glyph={dial.render.glyph}
            value={dial.render.value}
            detail={dial.render.detail}
            color={dial.render.color}
            ratio={dial.render.ratio}
            tone={dial.tone}
            pulse={dial.render.pulse}
            title={`${dial.render.title} · ${dial.reason}`}
            onClick={handlerFor(dial)}
          />
        );
      })}
    </div>
  );
}
