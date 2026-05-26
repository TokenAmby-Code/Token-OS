// Bespoke time-series chart. No chart library: a few hundred lines of SVG buy
// full control over the segmented mode shading and threshold-colored balance
// line the brief asks for, and keep the committed build dependency-free.
//
// Consumes the `TimerHistory` contract directly.

import { useEffect, useMemo, useRef, useState } from 'react';
import type { TimerHistory } from '../types';
import { modeVisual } from '../modes';
import { formatSignedDuration, formatClock } from '../format';

const PAD = { top: 16, right: 16, bottom: 26, left: 52 };
const HEIGHT = 240;

type Props = { history: TimerHistory };

export function TimerGraph({ history }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(900);
  const [hover, setHover] = useState<number | null>(null);

  // Observe container width for responsive sizing.
  useEffect(() => {
    const el = wrapRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w) setWidth(Math.max(320, Math.floor(w)));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const { points, segments } = history;

  const geom = useMemo(() => {
    if (points.length === 0) return null;
    const t0 = Date.parse(points[0].t);
    const t1 = Date.parse(points[points.length - 1].t);
    const span = Math.max(1, t1 - t0);

    const balances = points.map((p) => p.break_balance_ms);
    const loRaw = Math.min(0, ...balances);
    const hiRaw = Math.max(0, ...balances);

    // Quantize the axis to quarter-hour steps. Pick the smallest step from a
    // ladder of 15-minute multiples that keeps the gridline count sane — never
    // a blind divide, and every step stays on a clean %15 value. Then snap the
    // domain to that step so top/bottom/zero all land on quarter-hour values.
    const MIN = 60_000;
    const rangeMin = Math.max(15, (hiRaw - loRaw) / MIN);
    const stepMs = chooseQuarterHourStep(rangeMin) * MIN;
    let lo = Math.floor(loRaw / stepMs) * stepMs;
    let hi = Math.ceil(hiRaw / stepMs) * stepMs;
    if (hi === lo) hi += stepMs;

    const plotW = width - PAD.left - PAD.right;
    const plotH = HEIGHT - PAD.top - PAD.bottom;
    const x = (t: number) => PAD.left + ((t - t0) / span) * plotW;
    const y = (v: number) => PAD.top + (1 - (v - lo) / (hi - lo)) * plotH;

    return { t0, t1, span, lo, hi, stepMs, plotW, plotH, x, y };
  }, [points, width]);

  if (!geom) {
    return (
      <div className="chart-empty">No timer history in window.</div>
    );
  }

  const { t0, span, lo, hi, stepMs, plotW, plotH, x, y } = geom;
  const zeroY = y(0);

  // Balance path + a baseline-to-line fill, split at the zero crossing so the
  // area reads green above / hazard below.
  const linePath = points
    .map((p, i) => `${i === 0 ? 'M' : 'L'} ${x(Date.parse(p.t)).toFixed(1)} ${y(p.break_balance_ms).toFixed(1)}`)
    .join(' ');

  const yTicks = quarterHourTicks(lo, hi, stepMs);
  const tape = hourTapeTicks(t0, t0 + span);

  const hoverPoint = hover != null ? points[hover] : null;
  const hoverX = hoverPoint ? x(Date.parse(hoverPoint.t)) : 0;

  function onMove(e: React.MouseEvent<SVGRectElement>) {
    const rect = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - rect.left + PAD.left;
    const ratio = (px - PAD.left) / plotW;
    const idx = Math.round(ratio * (points.length - 1));
    setHover(Math.max(0, Math.min(points.length - 1, idx)));
  }

  return (
    <div className="chart" ref={wrapRef}>
      <svg width={width} height={HEIGHT} role="img" aria-label="Break balance over time">
        <defs>
          <clipPath id="tg-above"><rect x={PAD.left} y={PAD.top} width={plotW} height={Math.max(0, zeroY - PAD.top)} /></clipPath>
          <clipPath id="tg-below"><rect x={PAD.left} y={zeroY} width={plotW} height={Math.max(0, HEIGHT - PAD.bottom - zeroY)} /></clipPath>
          <linearGradient id="tg-fill-pos" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--m-working)" stopOpacity="0.28" />
            <stop offset="100%" stopColor="var(--m-working)" stopOpacity="0.02" />
          </linearGradient>
          <linearGradient id="tg-fill-neg" x1="0" y1="1" x2="0" y2="0">
            <stop offset="0%" stopColor="var(--hazard)" stopOpacity="0.30" />
            <stop offset="100%" stopColor="var(--hazard)" stopOpacity="0.02" />
          </linearGradient>
        </defs>

        {/* Mode shading bands */}
        {segments.map((seg, i) => {
          const sx = x(Math.max(t0, Date.parse(seg.start)));
          const ex = x(Math.min(t0 + span, Date.parse(seg.end)));
          const w = Math.max(0, ex - sx);
          if (w <= 0) return null;
          const v = modeVisual(seg.mode);
          return (
            <g key={i} aria-label={v.label}>
              <rect x={sx} y={PAD.top} width={w} height={plotH} fill={v.color} opacity={0.1} />
            </g>
          );
        })}

        {/* Y gridlines + labels */}
        {yTicks.map((v) => (
          <g key={v}>
            <line x1={PAD.left} x2={width - PAD.right} y1={y(v)} y2={y(v)} className="grid" />
            <text x={PAD.left - 8} y={y(v) + 3} className="axis-y" textAnchor="end">
              {formatSignedDuration(v)}
            </text>
          </g>
        ))}

        {/* Filled areas split at zero */}
        <path d={`${linePath} L ${x(t0 + span).toFixed(1)} ${zeroY} L ${x(t0).toFixed(1)} ${zeroY} Z`} fill="url(#tg-fill-pos)" clipPath="url(#tg-above)" />
        <path d={`${linePath} L ${x(t0 + span).toFixed(1)} ${zeroY} L ${x(t0).toFixed(1)} ${zeroY} Z`} fill="url(#tg-fill-neg)" clipPath="url(#tg-below)" />

        {/* Zero line — prominent */}
        <line x1={PAD.left} x2={width - PAD.right} y1={zeroY} y2={zeroY} className="zero-line" />

        {/* Balance line, threshold colored */}
        <path d={linePath} className="bal-line bal-line--pos" clipPath="url(#tg-above)" />
        <path d={linePath} className="bal-line bal-line--neg" clipPath="url(#tg-below)" />

        {/* X axis — tape-measure: labeled hour marks, medium :30, minor :15/:45 */}
        {tape.map((tk) => {
          const tx = x(tk.t);
          const len = tk.kind === 'major' ? 9 : tk.kind === 'mid' ? 6 : 4;
          const axisY = HEIGHT - PAD.bottom;
          return (
            <g key={tk.t}>
              <line x1={tx} x2={tx} y1={axisY} y2={axisY + len} className={`tick tick--${tk.kind}`} />
              {tk.kind === 'major' ? (
                <text x={tx} y={HEIGHT - 7} className="axis-x" textAnchor="middle">
                  {tk.label}
                </text>
              ) : null}
            </g>
          );
        })}

        {/* Hover crosshair */}
        {hoverPoint ? (
          <g>
            <line x1={hoverX} x2={hoverX} y1={PAD.top} y2={HEIGHT - PAD.bottom} className="crosshair" />
            <circle cx={hoverX} cy={y(hoverPoint.break_balance_ms)} r={4} className="cursor-dot" />
          </g>
        ) : null}

        {/* Interaction surface */}
        <rect
          x={PAD.left}
          y={PAD.top}
          width={plotW}
          height={plotH}
          fill="transparent"
          onMouseMove={onMove}
          onMouseLeave={() => setHover(null)}
        />
      </svg>

      {hoverPoint ? (
        <div
          className="chart-tip"
          style={{ left: `${Math.min(hoverX, width - 180)}px` }}
        >
          <div className="chart-tip__time">{formatClock(hoverPoint.t)}</div>
          <div className="chart-tip__row">
            <span className="dot-sm" style={{ background: modeVisual(hoverPoint.mode).color }} />
            <strong>{modeVisual(hoverPoint.mode).label}</strong>
          </div>
          <div className="chart-tip__row">balance <strong>{formatSignedDuration(hoverPoint.break_balance_ms)}</strong></div>
          <div className="chart-tip__row chart-tip__muted">
            {hoverPoint.productivity_active ? 'productive' : 'inactive'}
            {hoverPoint.desktop_mode ? ` · ${hoverPoint.desktop_mode}` : ''}
            {hoverPoint.phone_app ? ` · 📱${hoverPoint.phone_app}` : ''}
          </div>
        </div>
      ) : null}
    </div>
  );
}

// ── tick helpers ──────────────────────────────────────────────────────────

// Ladder of quarter-hour multiples (minutes). The Y step is whichever rung
// keeps the gridline count reasonable — chosen, not computed by division, so
// every value stays on a clean %15 boundary.
const STEP_LADDER_MIN = [15, 30, 45, 60, 90, 120, 180, 240, 360, 480, 720];
const MAX_GRIDLINES = 6;

function chooseQuarterHourStep(rangeMin: number): number {
  for (const step of STEP_LADDER_MIN) {
    if (rangeMin / step <= MAX_GRIDLINES) return step;
  }
  // Beyond the ladder, round up to a whole hour that fits.
  return Math.ceil(rangeMin / MAX_GRIDLINES / 60) * 60;
}

// Ticks at every quarter-hour step across the (already-snapped) domain. Both
// bounds and the step are multiples of `stepMs`, so zero falls on a tick.
function quarterHourTicks(lo: number, hi: number, stepMs: number): number[] {
  const ticks: number[] = [];
  for (let v = lo; v <= hi + 1; v += stepMs) ticks.push(Math.round(v));
  return ticks;
}

type TapeTick = { t: number; kind: 'major' | 'mid' | 'minor'; label: string };

// Tape-measure X axis: a mark every 15 min. Labeled on the hour, a medium
// mark at :30, small marks at :15 and :45.
function hourTapeTicks(t0: number, t1: number): TapeTick[] {
  const QUARTER = 15 * 60_000;
  const first = Math.ceil(t0 / QUARTER) * QUARTER;
  const ticks: TapeTick[] = [];
  for (let t = first; t <= t1; t += QUARTER) {
    const d = new Date(t);
    const m = d.getMinutes();
    const kind: TapeTick['kind'] = m === 0 ? 'major' : m === 30 ? 'mid' : 'minor';
    const label = m === 0 ? `${d.getHours().toString().padStart(2, '0')}:00` : '';
    ticks.push({ t, kind, label });
  }
  return ticks;
}
