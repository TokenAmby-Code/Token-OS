// A free-floating HUD dial: a circular gauge with the glyph/value *inside* the
// circle and the label/detail as a caption *below* it — so text never overruns
// the dial. Magnitudes (break balance, fleet) render an arc; enum/bool states
// (timer mode, desktop, phone) render a center glyph. Read from the shared
// visual language so the corner cluster stays coherent.

const R = 40; // arc radius within the 96×96 viewBox
const C = 2 * Math.PI * R;

export type RingProps = {
  label: string;
  value?: string; // center text — numbers, signed durations
  glyph?: string; // center glyph — enum/bool states
  detail?: string; // caption line under the dial
  color?: string; // accent (CSS var or hex); defaults to brass
  ratio?: number; // 0..1 arc fill; omit for a plain track ring
  tone?: 'good' | 'bad' | 'warn' | 'neutral';
  pulse?: boolean; // pulsing ring (e.g. pending enforcement)
  title?: string;
  onClick?: () => void;
};

export function Ring({ label, value, glyph, detail, color, ratio, tone, pulse, title, onClick }: RingProps) {
  const accent = color ?? 'var(--brass)';
  const clamped = ratio == null ? null : Math.max(0, Math.min(1, ratio));
  const dash = clamped == null ? null : `${clamped * C} ${C}`;
  const interactive = Boolean(onClick);
  return (
    <div
      className={`ring ${tone ? `ring--${tone}` : ''} ${pulse ? 'ring--pulse' : ''} ${interactive ? 'ring--clickable' : ''}`}
      title={title}
      style={{ '--ring-c': accent } as React.CSSProperties}
      onClick={onClick}
      role={interactive ? 'button' : undefined}
      tabIndex={interactive ? 0 : undefined}
      aria-label={interactive ? title ?? label : undefined}
      onKeyDown={
        interactive
          ? (event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                onClick?.();
              }
            }
          : undefined
      }
    >
      <div className="ring__dial">
        <svg viewBox="0 0 96 96" className="ring__svg" aria-hidden>
          <circle cx="48" cy="48" r={R} className="ring__track" />
          {dash != null ? (
            <circle cx="48" cy="48" r={R} className="ring__arc" strokeDasharray={dash} />
          ) : null}
        </svg>
        <span className="ring__center">
          {glyph ? <span className="ring__glyph">{glyph}</span> : null}
          {value ? <span className="ring__value">{value}</span> : null}
        </span>
      </div>
      <div className="ring__cap">
        <span className="ring__label">{label}</span>
        {detail ? <span className="ring__detail">{detail}</span> : null}
      </div>
    </div>
  );
}
