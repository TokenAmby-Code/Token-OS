import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { personaIcon, personaIconInner, personaImage, FACTION_PERSONAS } from './personaIcons';
import {
  DAY_END,
  DAY_START,
  dials,
  initialDialCount,
  initialTtsDepth,
  MAX_DIAL_COUNT,
  MAX_TTS_DEPTH,
  timerPoints,
  timerSegments,
  ttsLanguishThreshold,
  ttsQueue,
  type DialTone,
  type MockDial,
  type MockTimerMode,
  type MockTtsItem,
  type MockTtsStatus,
} from './mockCockpitData';
import {
  DIR_DEGREES,
  resolveCompass,
  type CompassStar,
  type ResolvedCompass,
  type StarColor,
} from './compass';

// Mode → hex, mirroring the live styles.css --m-* tokens so SVG gradients and
// DOM chips read as one instrument.
const MODE_HEX: Record<MockTimerMode, string> = {
  working: '#93d94f',
  multitasking: '#56c2d6',
  distracted: '#ff5b3d',
  break: '#e8a13c',
  idle: '#7e7790',
};
const MODE_LABEL: Record<MockTimerMode, string> = {
  working: 'WORKING',
  multitasking: 'MULTITASK',
  distracted: 'DISTRACTED',
  break: 'BREAK',
  idle: 'IDLE',
};

const toMin = (hhmm: string) => {
  const [h, m] = hhmm.split(':').map(Number);
  return h * 60 + m;
};

// Signed break balance → compact readout (unicode minus for the debt side).
const fmtBalance = (v: number) => {
  const n = Math.round(v);
  if (n > 0) return `+${n}m`;
  if (n < 0) return `−${Math.abs(n)}m`;
  return '0m';
};

// Signed break balance in minutes → `±Xh Ym` (or `±Ym` under an hour; `0m` at
// zero). Credit reads with +, debt (backlog) with the unicode minus.
const fmtBreak = (m: number) => {
  const sign = m > 0 ? '+' : m < 0 ? '−' : '';
  const a = Math.abs(m);
  const h = Math.floor(a / 60);
  const mm = a % 60;
  const body = h > 0 ? `${h}h ${mm}m` : `${mm}m`;
  return `${sign}${body}`;
};

// ═══════════════════════════════════════════════════════════════════════════
// GENERIC SCREEN-SIZE RESILIENCE — one viewport-derived scale factor.
//
// The whole instrument cluster is authored at DESIGN_W (1440px). A single
// `uiScale = clamp(SCALE_MIN, vp.w / DESIGN_W, 1)` shrinks it coherently at any
// narrower width: every INSTRUMENT LENGTH is multiplied by it at the point of
// consumption (base constants stay authored at 1440 — one source), while ANGLES
// and every `*Frac` RATIO are scale-invariant, so proportional scaling is
// geometrically exact (dials keep nesting in the rim, the arc keeps meeting it).
//
//   • capped at 1 — never upscale past the authored design (past 1440 the timer's
//     ampScale keeps today's line-stretch behaviour; the cluster stays put).
//   • floored at SCALE_MIN so phones don't go microscopic (tune by eye via the
//     demo-bar knob).
// ═══════════════════════════════════════════════════════════════════════════
const DESIGN_W = 1440; // authoring width — mirrors AMP_BASE_W inside TimerField
const SCALE_MIN = 0.45; // tunable floor: how small the cluster may shrink
const clamp = (lo: number, v: number, hi: number) => Math.min(hi, Math.max(lo, v));

// ═══════════════════════════════════════════════════════════════════════════
// THE TIMER'S DUAL CORE — two first-class contracts.
//
// The timer field is modelled as TWO distinct, non-overlapping contracts; every
// element on the field belongs to exactly one of them:
//
//   ① TrueBounds — the FROZEN functional plot. The data line, brass horizon,
//      credit/debt area fills, the now-dot and the hover-INDEX math all read from
//      here. Its box is stapled to the top + left of the page and capped on the
//      right (--graph-w) and bottom (--graph-h) so the dial + arc never obscure
//      real data. Nothing here is ever extended — altering render output means
//      changing THIS contract, which is out of bounds for a hygiene pass.
//
//   ② Facade — the DRESSING, extended past TrueBounds to fill the dead space that
//      always opens up cramming a rectangle between circles: the mode-band
//      columns + dashed gridlines bleed RIGHT (to bgRight) and DOWN (to bgBottom),
//      and the facade also anchors the hover crosshair + hover ZONE.
//      ── THE FACADE RULE ── every facade element is OCCLUDED by the dial and the
//      arc; it must NOT show through either:
//        • dial → each facade element carries `clipPath={facade.occludeClip}`, one
//          shared clip that punches the dial disc out (evenodd) so nothing bleeds
//          through the dial's translucent inner face.
//        • arc  → the opaque `.arc-fill` (z:3) paints over everything below the
//          arc curve, so z-order does it — no clip needed.
//      The SOLE piercing exception is the Tooltip (see TooltipContract below).
// ═══════════════════════════════════════════════════════════════════════════

// Y-axis window + gridline cadence — shared by both contracts.
const Y_MIN = -45;
const Y_MAX = 50;
const GRID_UNIT = 15; // gridline spacing, in balance-minutes
const Y_TICKS = [45, 30, 15, 0, -15, -30] as const;

interface TrueBounds {
  readonly w: number;
  readonly h: number;
  readonly plotW: number;
  readonly plotH: number;
  readonly X: (min: number) => number; // minute-of-day → local px x
  readonly Y: (balanceMin: number) => number; // signed balance → local px y
  readonly horizonY: number; // Y(0) — the break-even line
}

// The frozen functional plot. padL/padR/padT/padB are all 0 (flush box), folded
// straight into X/Y here — the plot maps 1:1 onto its measured px footprint.
function computeBounds(w: number, h: number): TrueBounds {
  const t0 = toMin(DAY_START);
  const t1 = toMin(DAY_END);
  const plotW = w;
  const plotH = h;
  const X = (min: number) => ((min - t0) / (t1 - t0)) * plotW;
  const Y = (v: number) => ((Y_MAX - v) / (Y_MAX - Y_MIN)) * plotH;
  return { w, h, plotW, plotH, X, Y, horizonY: Y(0) };
}

interface Facade {
  readonly bgRight: number; // bands + gridlines bleed right to here
  readonly bgBottom: number; // …and down to here
  readonly belowTicksY: readonly number[]; // continuation gridlines below the plot floor
  readonly occludeClipId: string; // <clipPath> id — the dial disc punched out
  readonly occludeClipPath: string; // its path data (evenodd: everywhere EXCEPT the disc)
  readonly occludeClip: string; // ready-to-use `url(#id)` for facade clipPath props
}

// The extended dressing. bgBottomPx + viewportW are VIEWPORT-top px handed
// straight in — valid ONLY because .timerfield is stapled flush at page-top (see
// the .timerfield INVARIANT in cockpit.css). The occlusion clip reuses the exact
// break-hub disc: centre (viewportW, −Y_SHIFT), radius HUB_R.
function computeFacade(b: TrueBounds, bgBottomPx: number, viewportW: number, scale: number): Facade {
  const bgRight = b.w * 1.6;
  const bgBottom = Math.max(b.plotH, bgBottomPx);
  const gridStepPx = (b.plotH * GRID_UNIT) / (Y_MAX - Y_MIN);
  const belowTicksY: number[] = [];
  for (let y = b.Y(-30) + gridStepPx; y <= bgBottom + 0.5; y += gridStepPx) belowTicksY.push(y);
  // The occlusion disc rides the SCALED break-hub rim (centre (viewportW, −Y_SHIFT·scale),
  // radius HUB_R·scale) so the dressing is swept under the dial at any width.
  const cx = viewportW;
  const cy = -Y_SHIFT * scale;
  const r = HUB_R * scale;
  const occludeClipPath =
    `M-10000,-10000 H10000 V10000 H-10000 Z ` +
    `M${(cx - r).toFixed(1)},${cy.toFixed(1)} ` +
    `a${r},${r} 0 1,0 ${(2 * r).toFixed(1)},0 ` +
    `a${r},${r} 0 1,0 ${(-2 * r).toFixed(1)},0 Z`;
  const occludeClipId = 'tf-hub-clip';
  return { bgRight, bgBottom, belowTicksY, occludeClipId, occludeClipPath, occludeClip: `url(#${occludeClipId})` };
}

// ═══════════════════════════════════════════════════════════════════════════
// FUTURE — first-class Tooltip contract (documented now; unified during the
// data-layer integration pass, NOT this round — see the operator's directive).
//
// The tooltip is the SOLE element that PIERCES the facade occlusion: every other
// facade element is swept under the dial + arc, while the tooltip flows OVER them.
// It is a bounded (max-height) text box that tracks the cursor and is clamped
// ONLY to the hard viewport edges — never to TrueBounds, the facade extent, the
// dial, or the arc.
//
// Two instances exist independently today and should collapse onto this one
// contract later: the timer's `.chart-tip` (cursor-following, React-driven — the
// reference clamp lives at the TimerField call site) and each dial's `.dial-tip`
// (CSS :hover, ring-anchored). Unify so every instrument reads + clamps alike.
// ═══════════════════════════════════════════════════════════════════════════
export interface TooltipContract {
  readonly anchor: { x: number; y: number }; // cursor / element the tip tracks
  readonly clampTo: 'viewport'; // hard screen edges ONLY — pierces every occluder
  readonly maxHeightPx: number; // bounded box; content elides/scrolls past it
  readonly piercesOcclusion: true; // never clipped by the dial disc or arc-fill
}

// ═══════════════════════════════════════════════════════════════════════════
// Timer field — the background itself. No card, no border, no surface: the
// gridlines, brass horizon, mode bands and threshold-coloured balance line
// paint straight onto the body's graphite metal. Fills stay translucent so the
// metal reads through (phosphor sky above the horizon, hazard-striped debt
// ground below). Full-bleed: the graph owns the top of the page.
//
// BOLD + TEXT-FREE: zero <text> — no axis labels, no break-even caption, no
// legend. The hover crosshair is the SOLE readout, mirroring the live cockpit.
//
// Rendered at REAL PIXELS: a ResizeObserver measures the container's px w/h and
// the SVG draws 1:1 into a matching viewBox (default preserveAspectRatio). No
// non-uniform 1000×300 stretch, so a tiled/narrow window never distorts it.
// ═══════════════════════════════════════════════════════════════════════════
function TimerField({ bgBottomPx, uiScale }: { bgBottomPx: number; uiScale: number }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [dims, setDims] = useState({ w: 1000, h: 480 });
  const [hover, setHover] = useState<number | null>(null);
  const [cursorX, setCursorX] = useState(0); // container-relative cursor X (raw, un-snapped)
  const [cursorY, setCursorY] = useState(0); // container-relative cursor height

  // Measure the container in real px (guarded) so geometry renders undistorted.
  useEffect(() => {
    const el = wrapRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (r && r.width && r.height) setDims({ w: Math.floor(r.width), h: Math.floor(r.height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const { w, h } = dims;
  // Live viewport width — the tooltip may drift right of the graph, so it's
  // clamped to the SCREEN edge (not the graph's), read fresh each render (renders
  // fire on hover + on resize via the ResizeObserver, so it stays current).
  const viewportW = typeof document !== 'undefined' ? document.documentElement.clientWidth : w;
  const padL = 0; // flush left — graph paints to the viewport edge
  const padR = 0; // flush right
  const padT = 0; // flush top — bands/line reach y=0 (yMax=50 already gives the peak headroom)

  // ── THE DUAL CORE (see "THE TIMER'S DUAL CORE" above) ──
  // ① the frozen functional plot; ② the extended, occluded dressing. Every render
  // element below reads from exactly one of these two typed contracts.
  const bounds = computeBounds(w, h);
  // bgBottomPx arrives authored at 1440; scale it so the dressing floor tracks the
  // arc's (also-scaled) left contact. The occlusion disc scales via the same factor.
  const facade = computeFacade(bounds, bgBottomPx * uiScale, viewportW, uiScale);
  const { plotW, X, Y, horizonY } = bounds;
  // occludeClip is THE facade rule: applied to every facade element so it is swept
  // under the dial disc (the arc-fill z:3 handles the arc). The tooltip omits it.
  const { bgRight, bgBottom, belowTicksY, occludeClipId, occludeClipPath, occludeClip } = facade;

  // Amplitude scales gently with viewport width. A wider viewport stretches the
  // day horizontally, so the balance line's slopes flatten out; nudge the vertical
  // swing back up to keep them legible. Pivots around the horizon (the y=0 baseline
  // stays put, so the midline↔rim seam is untouched) and is CAPPED so the tallest
  // peak / deepest trough never clip the plot box — the data already fills most of
  // the [Y_MIN, Y_MAX] range, so the headroom, not the width, is the real limit.
  const AMP_BASE_W = 1440; // at/below this width the tuned amplitude is left as-is
  const AMP_GAIN = 0.5; // how hard extra width pushes amplitude (before the cap)
  const AMP_MARGIN = 6; // px kept clear of the top (y=0) and the box floor
  const baseYs = timerPoints.map((p) => Y(p.breakBalanceMinutes));
  const devUp = horizonY - Math.min(...baseYs); // peak rise above the horizon
  const devDown = Math.max(...baseYs) - horizonY; // trough drop below it
  const ampCap = Math.max(1, Math.min(
    devUp > 0.5 ? (horizonY - AMP_MARGIN) / devUp : Infinity,
    devDown > 0.5 ? (h - AMP_MARGIN - horizonY) / devDown : Infinity,
  ));
  const ampScale = Math.min(1 + Math.max(0, viewportW / AMP_BASE_W - 1) * AMP_GAIN, ampCap);
  const ampY = (v: number) => horizonY + (Y(v) - horizonY) * ampScale;

  const pts = timerPoints.map((p) => ({ x: X(toMin(p.t)), y: ampY(p.breakBalanceMinutes), v: p.breakBalanceMinutes }));
  const linePath = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
  // Area from the line down/up to the horizon (closed along the zero baseline).
  const areaPath =
    `M${pts[0].x.toFixed(1)},${horizonY.toFixed(1)} ` +
    pts.map((p) => `L${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ') +
    ` L${pts[pts.length - 1].x.toFixed(1)},${horizonY.toFixed(1)} Z`;

  const hoverP = hover != null ? timerPoints[hover] : null;
  const hoverX = hoverP ? X(toMin(hoverP.t)) : 0;
  const hoverY = hoverP ? ampY(hoverP.breakBalanceMinutes) : 0;
  // Crosshair x: snapped to the sample WITHIN the graph, but once the cursor
  // passes the graph's right edge it rides the raw cursor into the extended bg
  // (the dot + data stay pinned to the last sample). No blocking at the edge.
  const lineX = cursorX > plotW ? cursorX : hoverX;

  // Extend the break-even HORIZON rightward to touch the hub rim, so it springs
  // from the same circle the arc does — the two brass instrument lines fork off
  // the shared rim instead of the horizon dead-ending in mid-air short of the
  // dial. The rim is the disc centred at (viewportW, −Y_SHIFT), radius HUB_R (same
  // as .break-hub + the arc's wheel clip); its LEFT intersection at y=horizonY is
  // viewportW − √(HUB_R² − (horizonY+Y_SHIFT)²). Push a few px PAST it so the round
  // cap tucks UNDER the opaque hub (z:1 over this z:0 layer) and merges into the
  // rim stroke with no gap. Falls back to the graph edge (w−padR) when the horizon
  // rides above the hub (no intersection). Stroke-continuity ONLY — horizonY and
  // the TrueBounds plot math are untouched.
  // Rim junction rides the SCALED hub (centre (viewportW, −Y_SHIFT·scale), radius
  // HUB_R·scale); horizonY is vh-locked (plot height), so only the hub side scales.
  const rimDy = horizonY + Y_SHIFT * uiScale;
  const rimHalfChord = HUB_R * uiScale * (HUB_R * uiScale) - rimDy * rimDy;
  const horizonRightX =
    rimHalfChord > 0 ? Math.max(w - padR, viewportW - Math.sqrt(rimHalfChord) + 4) : w - padR;

  // Map cursor → nearest sample index (clamped). The interaction rect spans the
  // plot area, so its width equals plotW and clientX-left divides straight in.
  function onMove(e: React.MouseEvent<SVGRectElement>) {
    const rect = e.currentTarget.getBoundingClientRect();
    // Index maps against the FROZEN plot width, not the (now wider) hit rect, so
    // the sample mapping is unchanged — the extra width only enlarges the hover
    // ZONE into the dressing. Past the graph's right edge, ratio>1 clamps to last.
    const x = e.clientX - rect.left;
    const ratio = plotW > 0 ? x / plotW : 0;
    const idx = Math.round(ratio * (timerPoints.length - 1));
    setHover(Math.max(0, Math.min(timerPoints.length - 1, idx)));
    // raw cursor x drives the crosshair line + tooltip once past the graph edge;
    // the data INDEX above stays clamped to the last sample there.
    setCursorX(x);
    // rect top sits at svg y=padT, so container-relative cursor height = offset + padT
    setCursorY(e.clientY - rect.top + padT);
  }

  return (
    <div className="timerfield" ref={wrapRef}>
      <svg className="timerfield__svg" width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img"
        aria-label="Break-balance over the day. Trough −38 minutes at 14:10; recovered to +9 by 16:45.">
        <defs>
          <linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#93d94f" stopOpacity="0.34" />
            <stop offset="100%" stopColor="#93d94f" stopOpacity="0.02" />
          </linearGradient>
          <linearGradient id="ground" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#ff5b3d" stopOpacity="0.05" />
            <stop offset="100%" stopColor="#ff5b3d" stopOpacity="0.34" />
          </linearGradient>
          <pattern id="hazard" width="10" height="10" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
            <rect width="10" height="10" fill="transparent" />
            <rect width="5" height="10" fill="#ff5b3d" opacity="0.16" />
          </pattern>
          <clipPath id="above"><rect x="0" y="0" width={w} height={horizonY} /></clipPath>
          <clipPath id="below"><rect x="0" y={horizonY} width={w} height={h - horizonY} /></clipPath>
          {/* THE facade occlusion clip — everywhere EXCEPT the break-hub disc.
              Shared by every facade element (bands, gridlines, crosshair) so the
              whole dressing is swept cleanly under the dial's translucent face. */}
          <clipPath id={occludeClipId}><path d={occludeClipPath} clipRule="evenodd" /></clipPath>
        </defs>

        {/* mode bands — authoritative categorical background (full-bleed to the
            bottom edge now that padB=0; the baseline ribbon is retired — the
            forthcoming bottom border will overlap this region and carry mode).
            FACADE element → occludeClip sweeps the band columns under the dial. */}
        {timerSegments.map((s, i) => {
          const x = X(toMin(s.start));
          const last = i === timerSegments.length - 1;
          // the rightmost mode keeps going to bgRight; every column drops to
          // bgBottom so the bands continue down into the dead space (dressing).
          const bw = (last ? bgRight : X(toMin(s.end))) - x;
          return (
            <rect key={i} x={x} y={padT} width={bw} height={bgBottom - padT} fill={MODE_HEX[s.mode]} opacity={0.07}
              clipPath={occludeClip} />
          );
        })}

        {/* credit sky + debt ground, split at the horizon. The area now runs right
            up to the dial-anchored border, so it's swept under the dial disc too. */}
        <g clipPath={occludeClip}>
          <path d={areaPath} fill="url(#sky)" clipPath="url(#above)" />
          <path d={areaPath} fill="url(#ground)" clipPath="url(#below)" />
          <path d={areaPath} fill="url(#hazard)" clipPath="url(#below)" />
        </g>

        {/* y gridlines — lines only, no labels. Extended RIGHT to bgRight so the
            dashed rules carry on past the graph into the gap. FACADE → occludeClip. */}
        {Y_TICKS.map((v) => (
          <line key={v} x1={padL} y1={Y(v)} x2={bgRight} y2={Y(v)} stroke="#2c2738" strokeWidth={v === 0 ? 0 : 1}
            strokeDasharray="2 6" opacity={0.6} clipPath={occludeClip} />
        ))}
        {/* …and CONTINUED DOWN below the plot floor at the same interval, filling
            the dead space beneath the graph with the same dashed grid. FACADE. */}
        {belowTicksY.map((y, i) => (
          <line key={`b${i}`} x1={padL} y1={y} x2={bgRight} y2={y} stroke="#2c2738" strokeWidth={1}
            strokeDasharray="2 6" opacity={0.6} clipPath={occludeClip} />
        ))}

        {/* the horizon — break-even. A structural instrument line: reads the
            shared --instrument palette token via the .horizon class (see cockpit.css),
            NOT an inline colour. Alpha lives once in the token, so no inline opacity. */}
        <line x1={padL} y1={horizonY} x2={horizonRightX} y2={horizonY} className="horizon" />

        {/* balance line, threshold-coloured at the horizon. Runs to the dial-anchored
            border and tucks under the dial disc (occludeClip) at the same seam the
            horizon reaches — so the timer's end is obscured by the dial at any width. */}
        <g clipPath={occludeClip}>
          <path d={linePath} fill="none" stroke="#93d94f" strokeWidth={2.4} clipPath="url(#above)"
            strokeLinejoin="round" strokeLinecap="round" />
          <path d={linePath} fill="none" stroke="#ff5b3d" strokeWidth={2.4} clipPath="url(#below)"
            strokeLinejoin="round" strokeLinecap="round" />

          {/* now marker — just the dot on the balance line; the full-height dashed
              gold rule is retired (it read as a stray border at the graph's edge). */}
          <circle cx={pts[pts.length - 1].x} cy={pts[pts.length - 1].y} r={4} fill="#f2c463" stroke="#07060a" strokeWidth={1.5} />
        </g>

        {/* hover crosshair — the sole readout */}
        {hoverP ? (
          <g>
            {/* crosshair runs the full DRESSING height (down to bgBottom), not
                just the functional floor, so it reads against the extended grid. */}
            <line x1={lineX} y1={padT} x2={lineX} y2={bgBottom} className="crosshair" clipPath={occludeClip} />
            <circle cx={hoverX} cy={hoverY} r={4} className="cursor-dot" />
          </g>
        ) : null}

        {/* transparent interaction surface — spans the full VISUAL area (the
            dressing extents), so hovering into the extended background works. Where
            the opaque arc-fill/hub cover the graph they intercept the pointer
            (pointer-events:auto in CSS) and this rect never sees it — so covered
            regions don't hover, matching what's actually visible. */}
        <rect x={padL} y={padT} width={bgRight - padL} height={bgBottom - padT} fill="transparent"
          onMouseMove={onMove} onMouseLeave={() => setHover(null)} />
      </svg>

      {/* floating HTML readout — follows the cursor, never distorted by SVG
          scaling. This is the reference implementation of TooltipContract (above):
          the SOLE facade element that PIERCES the dial/arc occlusion (no
          occludeClip, z:200), clamped ONLY to the viewport edges below. */}
      {hoverP ? (
        <div className="chart-tip tip-card" style={{
          // sits to the RIGHT of the raw cursor and follows it into the extended
          // bg; clamped only at the true SCREEN edge (viewportW), not the graph's.
          left: `${Math.max(0, Math.min(cursorX + 14, viewportW - 172))}px`,
          // vertical clamp follows the cursor down through the whole hover-able
          // area (the dressing floor, not the functional graph height), so the
          // tip flows anywhere the hover reaches instead of pinning to the top.
          top: `${Math.max(4, Math.min(cursorY - 34, bgBottom - 88))}px`,
        }}>
          <div className="chart-tip__time">{hoverP.t}</div>
          <div className="chart-tip__row">
            <span className="dot-sm" style={{ background: MODE_HEX[hoverP.mode] }} />
            <strong>{MODE_LABEL[hoverP.mode]}</strong>
          </div>
          <div className="chart-tip__row">balance <strong>{fmtBalance(hoverP.breakBalanceMinutes)}</strong></div>
        </div>
      ) : null}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// Floating radial dials — the stack test.
//
// One `.dials` layer, position:fixed / z-index 60 / pointer-events:none,
// mirroring the live cockpit so the gauges follow scroll and never intercept
// clicks. EVERY ring restores pointer-events (`.ring { pointer-events: auto }`)
// so all dials hover/click; the gaps between them stay click-through.
//
// Placement is a single deterministic polar formula, not a per-dial pixel
// table. Each dial i sits at radius R and angle θ from the top-RIGHT viewport
// corner, expressed as CSS offsets (distance from that corner):
//
//   right = R·cos(θ) − r      top = R·sin(θ) − r      (r = half dial size)
//
// The fan is a DOUBLE RADIAL that sawtooth-nests, plus a right-edge overflow:
//
//   • OUTER ring (larger radius, R_OUT) fills FIRST — up to OUTER_MAX dials,
//     evenly swept θ = THETA_MIN + i·Δθ.
//   • INNER ring (radius R_IN, kept close to R_OUT) fills next — its dials sit
//     at the VALLEY angles θ = THETA_MIN + (k+½)·Δθ, so they tuck into the gaps
//     between outer dials like gear teeth (sawtooth pack).
//   • OVERFLOW beyond both rings trails straight down the right edge, stacked
//     under the outer arc's foot — the vertical tail from the sketch.
//
// The floating dials are ICON-ONLY and compact — at-a-glance status, one glyph
// per circle, no caption. The value + a subheader ("what is this dial?") live
// in the hover tooltip and the side drawer instead, so the glanceable cluster
// stays small.
// ═══════════════════════════════════════════════════════════════════════════
// Geometry tuned so the minimum centre-to-centre distance stays ≥ dial diameter
// at every count — neither adjacent outer dials nor the nested inner row touch.
// With Δθ = 17.5°: inner-inner ≈ 2·R_IN·sin(Δθ/2) ≈ 55px, inner-outer ≈ 56px,
// outer-outer ≈ 69px — all clear of the 50px diameter with ~5px margin.
const DIAL_PX = 50;
const RING_R = DIAL_PX / 2;

// The big top-right fraction dial's diameter (the `4/1`). SINGLE SOURCE of truth:
// MockOpsCockpit publishes it to CSS as --corner-dial-d (so .corner-dial sizes off
// it) and ArcLayer reads it for the agent-dial radius — so if this number moves,
// the corner dial AND the agent dials riding the arc resize together, in lockstep.
const CORNER_DIAL_PX = 104;

// Static-persona row: LOCKED to exactly six dials — Custodes ("1", pinned at the
// pocket) plus the five standing personas marching left off it. This is a fixed
// roster, not a demo knob; the count never changes with the viewport or the fleet.
const PERSONA_COUNT = 6;
// Worker-count knob ceiling. Generous — the worker row wraps into further right-
// anchored rows below (trailing down the RHS) rather than capping, so this is just
// how far the demo slider travels, not a layout limit.
const MAX_WORKER_COUNT = 30;
// Idle-worker-queue ceiling — far past the crossbar band so the chips climb well up
// the two edge ditches and overflow (clobber) the region above; the overflow IS the
// "too many idle instances" error visual, so this is intentionally generous.
const MAX_IDLE_COUNT = 40;
const THETA_MIN = (12 * Math.PI) / 180;
const THETA_MAX = (82 * Math.PI) / 180;
const OUTER_MAX = 5; // outer ring capacity — fills first
const INNER_MAX = 4; // inner nests in the 4 valleys between the 5 outer dials
const R_OUT = 228; // outer radius (bigger sweep)
const R_IN = 182; // inner radius — one row in, close enough that the rows mesh
const D_THETA = (THETA_MAX - THETA_MIN) / (OUTER_MAX - 1);

// Global upward nudge. The raw polar formula leaves a bigger gap ABOVE the first
// dial (outer i=0, near the top edge: R_OUT·sinθmin − r) than BESIDE the last
// (outer i=4, near the right edge: R_OUT·cosθmax − r). Subtracting the
// difference from every dial's `top` slides the whole cluster up so those two
// gaps become equal (both = R_OUT·cosθmax − r) while the fan's internal
// geometry is untouched — relative positions stay identical.
const Y_SHIFT = R_OUT * (Math.sin(THETA_MIN) - Math.cos(THETA_MAX));

// The break hub is locked at 260px (operator-settled, no longer a tuning knob).
// Single source of truth for both the hub element and the break-marker geometry.
const HUB_R = 260;

// The hub's lowest visible point below the top edge (= the CSS --hub-bottom). The
// arc's contact points live in THIS top band; the opaque layer under the arc then
// drops from here to the bottom of the screen, so the band height and the layer
// height are decoupled (arc geometry stays put as the layer grows full-page).
const HUB_BOTTOM = HUB_R - Y_SHIFT;

// Break-time marker geometry. The marker rides the hub rim at radius HUB_R, using
// the SAME corner-anchored polar convention as dialOffset and made concentric via
// Y_SHIFT (the hub centre sits Y_SHIFT above the top-right corner). The marker
// walks the visible lower-left quadrant arc as breakTime grows 0→100:
//   • THETA_TOP — the rim meets the TOP edge, far left (top-left of the arc). At
//     this angle the rim's `top` offset is 0, i.e. HUB_R·sinθ = Y_SHIFT.
//   • THETA_RIGHT — the rim meets the RIGHT edge (bottom-right of the arc).
const THETA_TOP = Math.asin(Y_SHIFT / HUB_R);
const THETA_RIGHT = Math.PI / 2;
const THETA_RANGE = THETA_RIGHT - THETA_TOP; // one lap sweeps this arc
const BREAK_MARKER_PX = 10;

// ══ THE BREAK HUB AS A DOMAIN MODEL ═════════════════════════════════════════
// The hub renders a single SIGNED quantity — break balance in minutes — as a
// radial gauge on the visible rim arc. Zero is the neutral origin. Each hour is
// one full lap of the arc; a second hour lays a second lap over the first; beyond
// two hours the balance "spills over" and the whole rim just glows.
//
//   CREDIT (+, break earned): laps fill top-left → bottom-right.
//     lap 1 green (--phosphor) → lap 2 teal (--cyan) → spillover GOLD glow.
//   DEBT  (−, backlog): laps fill the SAME arc in REVERSE, bottom-right → top-left
//     ("walking backwards"). lap 1 dark red (--hazard-deep) → lap 2 bright red
//     (--hazard) → spillover RED glow.
//
// So the arc indexes ±2h from zero — symmetric but for sweep direction + palette,
// with a glow for the spillover past either end. `breakHubView(min)` is the single
// pure function that turns the signed minutes into everything the DOM paints.
const BREAK_LAP_MIN = 60; // one full rim lap = one hour
const BREAK_SPILL_MIN = 2 * BREAK_LAP_MIN; // beyond two laps → glow, ball retired
const BREAK_MAX_MIN = 150; // demo slider ceiling (headroom past the spill threshold)

// lap palettes, indexed [lap1, lap2] per polarity (CSS custom-property refs).
const CREDIT_TONES = ['var(--phosphor)', 'var(--cyan)'] as const;
const DEBT_TONES = ['var(--hazard-deep)', 'var(--hazard)'] as const;
// each pass thickens: lap 1 rides thinner, lap 2 draws heavier over it.
const LAP_WIDTHS = [3, 4.2] as const;

type BreakGlow = 'gold' | 'red' | null;
interface RimLap {
  d: string; // SVG arc path (box-local coords)
  tone: string; // stroke colour (CSS custom-property ref)
  width: number; // stroke width — grows with each pass
}
interface BreakHubView {
  laps: RimLap[]; // arcs to paint, earlier laps first (later ones draw on top)
  ball: { off: { right: number; top: number }; tone: string } | null; // leading head
  glow: BreakGlow; // rim glow once the balance spills past ±2h
}

// Corner-anchored offset (distance from the top-right corner) for a rim point at
// polar angle θ — used to place the marker bubble; m = half its size.
function rimOffset(theta: number, scale = 1): { right: number; top: number } {
  const m = (BREAK_MARKER_PX * scale) / 2; // half the marker's SCALED rendered size
  return {
    right: HUB_R * scale * Math.cos(theta) - m,
    top: HUB_R * scale * Math.sin(theta) - Y_SHIFT * scale - m,
  };
}

// SVG path for the rim arc between two angles, in box-local coords (viewBox
// 0 0 2·HUB_R, hub centre = (HUB_R, HUB_R), same fixed footprint as the hub).
// Sampled as a polyline so the sweep direction stays unambiguous; a rim point at
// angle θ is centre + HUB_R·(−cosθ, sinθ).
function arcPath(thetaA: number, thetaB: number): string {
  const STEPS = 48;
  let d = '';
  for (let s = 0; s <= STEPS; s++) {
    const theta = thetaA + (thetaB - thetaA) * (s / STEPS);
    const x = HUB_R - HUB_R * Math.cos(theta);
    const y = HUB_R + HUB_R * Math.sin(theta);
    d += `${s === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)} `;
  }
  return d.trim();
}

// One lap's arc filled to `frac` (0→1). Credit fills from THETA_TOP forward; debt
// fills from THETA_RIGHT backward (the "walk backwards"). Empty at frac 0.
function lapPath(frac: number, reversed: boolean): string {
  if (frac <= 0) return '';
  return reversed
    ? arcPath(THETA_RIGHT - frac * THETA_RANGE, THETA_RIGHT)
    : arcPath(THETA_TOP, THETA_TOP + frac * THETA_RANGE);
}

// The leading angle of a lap filled to `frac` — where the ball sits.
function leadTheta(frac: number, reversed: boolean): number {
  return reversed ? THETA_RIGHT - frac * THETA_RANGE : THETA_TOP + frac * THETA_RANGE;
}

// The whole model: signed minutes → laps + ball + glow. `scale` positions the ball
// on the SCALED rim (via rimOffset); the lap PATHS stay authored at HUB_R — they
// render inside the .break-trail SVG, whose element size scales via CSS --hub-r, so
// the paths ride along untouched (arcPath/lapPath need no scale).
function breakHubView(min: number, scale = 1): BreakHubView {
  // Neutral origin — a single resting head at the credit start, no trail yet.
  if (min === 0) {
    return { laps: [], ball: { off: rimOffset(THETA_TOP, scale), tone: CREDIT_TONES[0] }, glow: null };
  }
  const reversed = min < 0; // debt walks the arc backwards
  const abs = Math.abs(min);
  const tones = reversed ? DEBT_TONES : CREDIT_TONES;

  // Spillover past two laps: the rim just glows, laps + ball retire.
  if (abs >= BREAK_SPILL_MIN) {
    return { laps: [], ball: null, glow: reversed ? 'red' : 'gold' };
  }

  const lap1 = Math.min(1, abs / BREAK_LAP_MIN); // fills over hour 1
  const lap2 = Math.min(1, Math.max(0, (abs - BREAK_LAP_MIN) / BREAK_LAP_MIN)); // over hour 2
  const laps: RimLap[] = [{ d: lapPath(lap1, reversed), tone: tones[0], width: LAP_WIDTHS[0] }];
  if (lap2 > 0) laps.push({ d: lapPath(lap2, reversed), tone: tones[1], width: LAP_WIDTHS[1] });

  // The ball rides the leading (in-progress) lap.
  const onLap2 = abs >= BREAK_LAP_MIN;
  const activeFrac = onLap2 ? lap2 : lap1;
  const activeTone = onLap2 ? tones[1] : tones[0];
  return { laps, ball: { off: rimOffset(leadTheta(activeFrac, reversed), scale), tone: activeTone }, glow: null };
}

// Nudge the vertical overflow tail down a few px so the last outer dial (i=4, at
// the right edge) and the first overflow dial (i=9) part enough for the hub rim
// to pass cleanly between them. Applied to the whole stack — internal spacing kept.
const OVERFLOW_DROP = 10;

// Reserved overflow state-dial column — the single source of where that column
// lives. Overflow dials (dialOffset's else branch) stack in a FIXED right-edge
// column: box `right` CSS = R_OUT·cosθmax − RING_R, width DIAL_PX, independent of
// how many overflow. OVERFLOW_COL_RIGHT is that box `right` CSS position.
const OVERFLOW_COL_RIGHT = R_OUT * Math.cos(THETA_MAX) - RING_R;

// Polar offset (distance from the top-right corner) for dial i — the radial-fan
// geometry for the RIGHT status cluster. (The left TTS stack is a plain vertical
// column — anchored at the column origin + translated by slot, see TtsStack — not
// a mirror of this, so this stays single-corner.)
function dialOffset(i: number, scale = 1): { right: number; top: number } {
  const r = RING_R * scale;
  let right: number;
  let top: number;
  if (i < OUTER_MAX) {
    const theta = THETA_MIN + i * D_THETA;
    right = R_OUT * scale * Math.cos(theta) - r;
    top = R_OUT * scale * Math.sin(theta) - r;
  } else if (i - OUTER_MAX < INNER_MAX) {
    const theta = THETA_MIN + (i - OUTER_MAX + 0.5) * D_THETA; // valley angle → sawtooth
    right = R_IN * scale * Math.cos(theta) - r;
    top = R_IN * scale * Math.sin(theta) - r;
  } else {
    // overflow → vertical tail down the right edge, under the outer arc's foot.
    // UNBOUNDED: each extra dial stacks another DIAL_PX+8 row lower, so the tail
    // grows as far as the slider's max allows — nothing here caps it.
    const trailIdx = i - (OUTER_MAX + INNER_MAX);
    right = OVERFLOW_COL_RIGHT * scale; // === (R_OUT·cosθmax − RING_R)·scale
    top = R_OUT * scale * Math.sin(THETA_MAX) - r + OVERFLOW_DROP * scale + (trailIdx + 1) * (DIAL_PX + 8) * scale;
  }
  return { right, top: top - Y_SHIFT * scale };
}

// Left TTS stack layout — a plain vertical column down the left edge, NOT a
// radial fan. Head (i=0, currently speaking) sits at the top; each queued item
// stacks one row below.
//
// The TTS dials get their OWN size + gap (separate from the 50px status dials),
// packed a bit tighter, so that ~ttsLanguishThreshold of them fit above the
// connecting arc's left-edge contact — a convenient cosmetic marker (see the
// threshold's note in mockCockpitData). Both knobs are here and freely tunable.
//
// This only lines up at scroll-top: the stack is position:fixed while the arc
// scrolls with the page, so they drift apart on scroll BY DESIGN — the marker is
// a nice-to-have at rest, deliberately not coded around.
const TTS_DIAL_PX = 36; // TTS dial diameter (smaller than the status dials' 50)
const TTS_GAP = 8; // vertical padding between TTS dials
const TTS_LEFT = 16; // px inset from the left edge
const TTS_TOP = 10; // px inset of the head from the top edge
const TTS_ROW = TTS_DIAL_PX + TTS_GAP; // centre-to-centre vertical step = one UNIT

// Play-gesture timings. SPEAK_MS is the static stand-in for a real utterance's
// duration (no audio wired this round); PROMOTE_MS / DISMISS_MS match the CSS
// keyframe durations (tts-promote / tts-dismiss) so the JS state machine and the
// animations land in lockstep — the promote keyframe ends at translate(0,0),
// exactly the dial's new slot-0 base transform, so dropping the class is jump-free.
const SPEAK_MS = 5000;
const PROMOTE_MS = 820;
const DISMISS_MS = 300;
// Hold the shift-down of slots 0…n−1 a beat after the click so the promoted dial
// clears the column (moves 1 unit RIGHT — the first ~30% of PROMOTE_MS) before the
// rest collapse into the gap, rather than shifting down while it's still passing.
const SHIFT_DELAY_MS = 200;

// ── one generic, fully-interactive dial ────────────────────────────────────
// EVERY dial is a button: soft hover glow (CSS), keyboard-activatable, and a
// hover tooltip (`.dial-tip`, same visual card as the timer's `.chart-tip`)
// surfacing the subtitle. The tooltip is pure CSS `:hover` (no React state) and
// opens toward the screen interior (down-left of the ring) so it never runs off
// the top-right corner the cluster hugs.
//
// Click / Enter / Space resolve in priority order: an explicit `onActivate`
// override (the TTS stack's promote handler) wins outright; otherwise `dial.action`
// runs its mock handler; otherwise the default opens the drawer. The generic
// component owns this resolution, so the dial data stays purely declarative — the
// promote gesture rides `onActivate` rather than a new `tts-promote` action kind.
function Dial({
  dial,
  style,
  onOpenDrawer,
  onActivate,
  className,
  icon,
}: {
  dial: MockDial;
  style: React.CSSProperties;
  onOpenDrawer: (id: string) => void;
  onActivate?: (id: string) => void; // overrides the drawer path entirely when set
  className?: string; // extra classes (TTS phase modifiers: promoting/speaking/…)
  icon?: ReactNode; // optional glyph override — the TTS senders pass a persona SVG
  //                   in place of the unicode glyph; the status fan omits it.
}) {
  function activate() {
    if (onActivate) {
      onActivate(dial.id);
      return;
    }
    switch (dial.action?.kind) {
      // Override hooks. In the static mock these focus the drawer on the dial
      // or are labeled no-ops — the point is the seam exists and is trivial to
      // wire to a real feature later.
      case 'toggle-timer':
        console.log('[dial] toggle-timer — pause/resume the running timer (mock)');
        onOpenDrawer(dial.id);
        break;
      case 'dismiss-phone':
        console.log('[dial] dismiss-phone — dismiss the phone-distraction alert (mock)');
        onOpenDrawer(dial.id);
        break;
      case 'ack-enforce':
        console.log('[dial] ack-enforce — acknowledge the pending enforcement (mock)');
        onOpenDrawer(dial.id);
        break;
      default:
        // No override → the default click: open the dials drawer.
        onOpenDrawer(dial.id);
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      activate();
    }
  }

  return (
    <div
      className={`ring ring--${dial.tone}${className ? ` ${className}` : ''}`}
      style={style}
      role="button"
      tabIndex={0}
      aria-label={`${dial.label}: ${dial.value}`}
      onClick={activate}
      onKeyDown={onKeyDown}
    >
      {/* icon override (TTS persona SVG) wins over the unicode glyph; both sit in
          .ring__glyph so the persona icon's currentColor inherits its --tone. */}
      <span className="ring__glyph">{icon ?? dial.glyph}</span>
      {/* hover tooltip — same card as the timer tip, opens toward the interior.
          When a `tag` is present (the TTS stack's sender tmuxctl id) it leads as a
          mono chip, followed by the instance-name label. */}
      <div className="dial-tip tip-card" role="tooltip">
        <div className="dial-tip__head">
          {dial.tag ? <span className="dial-tip__id">{dial.tag}</span> : null}
          <span className="dial-tip__label">{dial.label}</span>
        </div>
        <div className="dial-tip__sub">{dial.subtitle}</div>
      </div>
    </div>
  );
}

function Dials({ count, onOpenDrawer, uiScale }: { count: number; onOpenDrawer: (id: string) => void; uiScale: number }) {
  // Cycle the catalog if the slider runs past it, so density keeps growing.
  const shown = Array.from({ length: count }, (_, i) => dials[i % dials.length]);

  return (
    <div className="dials" aria-label={`Floating state dials · ${count}`}>
      {shown.map((dial, i) => (
        <Dial
          key={i}
          dial={dial}
          style={{ width: DIAL_PX * uiScale, height: DIAL_PX * uiScale, ...dialOffset(i, uiScale) }}
          onOpenDrawer={onOpenDrawer}
        />
      ))}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// Left-side TTS-queue stack — the SECOND dial cluster.
//
// Shares the visual + interaction primitives with the right-hand status fan:
// the same <Dial> ring chrome and the same hover tooltip. Its LAYOUT, though, is
// a plain vertical column down the left edge (transform-by-slot, see TtsStack),
// NOT a radial fan — a queue reads top-to-bottom, so the fan geometry is
// deliberately not reused here.
//
// The model is a live, stateful ORDERED queue (slot = array index). It has a PLAY
// GESTURE: clicking a queued dial promotes it to the head along an L-shaped path,
// it reverberates while it "speaks" (a static SPEAK_MS stand-in — no real TTS
// audio wired), then vanishes and the stack shuffles up. The depth slider is the
// source-of-truth resize of the tail. Still a static study: the 5 s stands in for
// a real utterance's duration; nothing is wired to the live speak queue yet.
//
// Tone + glyph come from a SMALL status map local to the stack (deliberately
// distinct from the status dials'), then flow through the shared <Dial> as an
// ordinary MockDial. So the chrome is shared; only the mapping is bespoke.
// ═══════════════════════════════════════════════════════════════════════════
const TTS_TONE: Record<MockTtsStatus, DialTone> = {
  speaking: 'good', // live on the wire
  queued: 'warn', // waiting its turn
  done: 'idle', // already delivered, draining out of the tail
};
const TTS_GLYPH: Record<MockTtsStatus, string> = {
  speaking: '▶',
  queued: '☰',
  done: '✓',
};

// Project a queue item onto the shared MockDial contract the <Dial> ring reads.
// The hover tip leads with the sender's identity — its tmuxctl id (`tag`, the mono
// chip) then its instance-name (`label`) — with the utterance as the subtitle.
// glyph/tone come from the bespoke TTS map above; `value` carries the route.
function ttsItemToDial(item: MockTtsItem): MockDial {
  return {
    id: item.id,
    label: item.senderName,
    tag: item.senderTmuxId,
    glyph: TTS_GLYPH[item.status],
    value: item.route,
    tone: TTS_TONE[item.status],
    noteworthy: item.status === 'speaking',
    subtitle: item.text,
  };
}

// ── Mutual-exclusion display (the DB owns the invariant) ─────────────────────
// Each persona is a SINGLETON: it must appear at most once in any one queue (the
// TTS queue, a worker queue). That uniqueness is enforced UPSTREAM in the DB, not
// here — so the cockpit deliberately does NOT dedupe, reorder, or hide anything to
// make a breach disappear. It renders the queue EXACTLY as the data says and, when
// the data is broken (a persona repeats), marks every 2nd-or-later occurrence —
// walked in queue order — so the operator sees the breach LOUDLY (a bright-red
// error glow) instead of a silently-corrected display. Broken data reads AS broken.
//
// Returns the set of entry keys that are duplicate occurrences (the first stays
// clean; the rest are flagged).
function duplicatePersonaKeys<T>(
  items: readonly T[],
  order: (t: T) => number,
  persona: (t: T) => string,
  key: (t: T) => string,
): Set<string> {
  const seen = new Set<string>();
  const dup = new Set<string>();
  [...items]
    .sort((a, b) => order(a) - order(b))
    .forEach((t) => {
      const p = persona(t);
      if (seen.has(p)) dup.add(key(t));
      else seen.add(p);
    });
  return dup;
}

// The play gesture turns the stack from a static display into a small state
// machine over a stateful ordered list. Each live entry carries a stable key (so
// React identity survives the reorders + removals), the projected queue item, a
// phase, and an explicit SLOT (visual row; 0 = head).
//
// CRUCIAL: the array's ORDER is stable (insertion order) — it is NEVER reordered.
// Position is driven ONLY by the `slot` field, so a promote just MUTATES slot
// numbers in place. That keeps each dial's DOM node fixed in the tree: moving a
// node in the DOM resets its CSS transition baseline in Chrome, which made the
// shift-down teleport when slot == array index. Decoupling slot from DOM order
// lets every slot change (shift-down, shuffle-up) transition smoothly.
type TtsPhase = 'idle' | 'promoting' | 'speaking' | 'dismissing';
interface TtsEntry {
  key: string;
  item: MockTtsItem;
  phase: TtsPhase;
  slot: number; // visual row (0 = head); the sole position driver
  promoteN?: number; // click-time slot n — feeds the L-path keyframe's --n
}

// Per-phase class set. The reverb (--speaking) is DELAYED until the dial LANDS at
// the head: during the travel the dial rides on top of the column (z:3), so a
// reverb there would throw rings over the other dials; holding it until `speaking`
// (where it sits at z:1, behind its siblings) keeps the shockwaves tucked cleanly
// behind them. The start-lag is faithful anyway — real playback waits on token-api,
// a MacroDroid ping for mobile, or WSL inter-device comms before the voice begins.
const TTS_PHASE_CLASS: Record<TtsPhase, string> = {
  idle: 'tts-dial',
  promoting: 'tts-dial tts-dial--promoting',
  speaking: 'tts-dial tts-dial--speaking',
  dismissing: 'tts-dial tts-dial--dismissing',
};

function TtsStack({ depth, onOpenDrawer, uiScale }: { depth: number; onOpenDrawer: (id: string) => void; uiScale: number }) {
  // Stateful ORDERED queue (slot = array index) with stable per-entry keys.
  // Seeded from ttsQueue cycled to depth. Keys are MONOTONIC via seqRef: a drain
  // removes an entry for good, so index-derived keys would collide when the tail
  // later regrows — the counter guarantees uniqueness across the whole session.
  const seqRef = useRef(0);
  const [entries, setEntries] = useState<TtsEntry[]>(() =>
    Array.from({ length: depth }, (_, i) => ({
      key: `t${i}`,
      item: ttsQueue[i % ttsQueue.length],
      phase: 'idle' as TtsPhase,
      slot: i,
    })),
  );

  // Stage timers (promote-land → speak window → dismiss → remove). Tracked so an
  // unmount / hot-reload can't leave a setState firing on a dead component.
  const timers = useRef<number[]>([]);
  useEffect(() => () => timers.current.forEach((t) => clearTimeout(t)), []);
  const after = (ms: number, fn: () => void) => {
    timers.current.push(window.setTimeout(fn, ms));
  };

  // Depth slider = source-of-truth resize: grow/shrink the TAIL only (append fresh
  // cycled entries / trim the deepest slots), leaving any in-flight animation —
  // always at/near the head — untouched. Trim + grow key off SLOT, not array
  // index, since the two are decoupled; slots stay contiguous 0…count−1 either way.
  useEffect(() => {
    setEntries((prev) => {
      const count = prev.length;
      if (depth === count) return prev;
      if (depth < count) return prev.filter((e) => e.slot < depth); // drop deepest rows
      const add: TtsEntry[] = [];
      for (let i = count; i < depth; i++) {
        add.push({ key: `a${seqRef.current++}`, item: ttsQueue[i % ttsQueue.length], phase: 'idle', slot: i });
      }
      return [...prev, ...add];
    });
  }, [depth]);

  // Stage 3: after the speak window, collapse + REMOVE the entry, then decrement
  // every deeper slot — so their translateY transitions animate them UP one row
  // (the shuffle-up). The queue drains: depth drops by one (the slider rebuilds it).
  function scheduleSpeak(key: string, durationMs: number) {
    after(durationMs, () => {
      setEntries((prev) => prev.map((e) => (e.key === key ? { ...e, phase: 'dismissing' as TtsPhase } : e)));
      after(DISMISS_MS, () =>
        setEntries((prev) => {
          const gone = prev.find((e) => e.key === key);
          if (!gone) return prev;
          return prev
            .filter((e) => e.key !== key)
            .map((e) => (e.slot > gone.slot ? { ...e, slot: e.slot - 1 } : e));
        }),
      );
    });
  }

  // The play gesture. ONE active speaker: while anything is mid-flight, new promote
  // clicks are ignored — matches the live single serialized speak_tts queue.
  function promote(key: string) {
    if (entries.some((e) => e.phase !== 'idle')) return;
    const clicked = entries.find((e) => e.key === key);
    if (!clicked) return;
    const n = clicked.slot;
    const durationMs = clicked.item.durationMs ?? SPEAK_MS;

    if (n === 0) {
      // Base case: the head plays IN PLACE — no movement, just reverberate + speak.
      setEntries((prev) => prev.map((e) => (e.key === key ? { ...e, phase: 'speaking' as TtsPhase } : e)));
      scheduleSpeak(key, durationMs);
      return;
    }

    // n ≥ 1: MUTATE slots in place (array order untouched — see the TtsEntry note).
    // First, mark the clicked dial promoting + move it to slot 0; it rides the
    // L-path keyframe (--n = n) so it visually travels right → up → left.
    setEntries((prev) =>
      prev.map((e) => (e.key === key ? { ...e, slot: 0, promoteN: n, phase: 'promoting' as TtsPhase } : e)),
    );
    // Then, a beat later (once x has cleared right), shift every dial in slots
    // 0…n−1 down +1 — their translateY transitions animate them straight DOWN one
    // unit, clearing slot 0 for x's landing. Deeper slots are untouched.
    after(SHIFT_DELAY_MS, () => {
      setEntries((prev) => prev.map((e) => (e.key !== key && e.slot < n ? { ...e, slot: e.slot + 1 } : e)));
    });
    // When the L-path lands the dial is already at slot 0 (base transform
    // translate(0,0) == the keyframe's end), so dropping `promoting` for `speaking`
    // is jump-free; then the speak window opens.
    after(PROMOTE_MS, () => {
      setEntries((prev) => prev.map((e) => (e.key === key ? { ...e, phase: 'speaking' as TtsPhase } : e)));
      scheduleSpeak(key, durationMs);
    });
  }

  // Flag any persona that repeats in the queue (2nd+ occurrence, by slot order) —
  // a DB-invariant breach the stack surfaces rather than hides (see the helper).
  const dupKeys = duplicatePersonaKeys(entries, (e) => e.slot, (e) => e.item.persona, (e) => e.key);

  return (
    <div
      className="tts-stack"
      aria-label={`TTS queue · ${entries.length} (languish threshold ${ttsLanguishThreshold})`}
      // --tts-unit is the single geometry source (shared with the promote keyframe);
      // scaling it flows the whole column + L-path shrink through one value.
      style={{ ['--tts-unit']: `${TTS_ROW * uiScale}px` } as React.CSSProperties}
    >
      {entries.map((e) => (
        <Dial
          key={e.key}
          dial={{ ...ttsItemToDial(e.item), id: e.key }}
          icon={personaIcon(e.item.persona)}
          className={`${TTS_PHASE_CLASS[e.phase]}${dupKeys.has(e.key) ? ' ring--dup' : ''}`}
          style={{
            width: TTS_DIAL_PX * uiScale,
            height: TTS_DIAL_PX * uiScale,
            left: TTS_LEFT * uiScale,
            top: TTS_TOP * uiScale,
            // §2 — slot (NOT array index) is expressed PURELY as a transform, so
            // any slot reassignment (shift-down, shuffle-up) animates for free.
            transform: `translateY(${e.slot * TTS_ROW * uiScale}px)`,
            ['--n']: e.promoteN ?? 0,
          } as React.CSSProperties}
          onOpenDrawer={onOpenDrawer}
          onActivate={promote}
        />
      ))}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// Dials drawer — minimal functional stub. Slides in from the right and lists
// every dial with its glyph, label, value and subtitle line. This proves the
// default click has a real destination; the full catalog (subheaders, fleet /
// evidence surfaces) is the next "website structure" plan. Close via the button,
// Escape, or a backdrop click.
// ═══════════════════════════════════════════════════════════════════════════
function DialsDrawer({
  open,
  focusedId,
  onClose,
}: {
  open: boolean;
  focusedId: string | null;
  onClose: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="drawer-scrim" onClick={onClose}>
      <aside
        className="drawer"
        role="dialog"
        aria-label="State dials"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="drawer__head">
          <h2 className="drawer__title">State dials</h2>
          <button className="drawer__close" onClick={onClose} aria-label="Close drawer">
            ✕
          </button>
        </header>
        <ul className="drawer__list">
          {dials.map((d) => (
            <li key={d.id} className={`drawer__row${d.id === focusedId ? ' drawer__row--focus' : ''}`}>
              <span className={`drawer__glyph ring--${d.tone}`}>{d.glyph}</span>
              <div className="drawer__body">
                <div className="drawer__rowhead">
                  <span className="drawer__label">{d.label}</span>
                  <span className={`drawer__value ring--${d.tone}`}>{d.value}</span>
                </div>
                <div className="drawer__sub">{d.subtitle}</div>
              </div>
            </li>
          ))}
        </ul>
      </aside>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// The big corner dial — a fraction gauge in the reserved top-right nook.
//
// Two numbers stacked as a fraction (focus on top, distraction below). Its
// colour is a pure function of the signed LEAD (focus − distraction): grey at a
// tie, warming LINEARLY toward teal (the favourite) as focus pulls ahead, and
// toward red as distraction does — each step worth an even quarter up to ±4.
// ═══════════════════════════════════════════════════════════════════════════
const FRAC_GREY: [number, number, number] = [126, 119, 144]; // --idle #7e7790
const FRAC_TEAL: [number, number, number] = [86, 194, 214]; // --cyan #56c2d6 (favourite)
const FRAC_RED: [number, number, number] = [255, 91, 61]; // --hazard #ff5b3d

// Brightness tracks the signed LEAD (focus − distraction), capped at ±4. The old
// version normalized by the total — (top−bottom)/total — which slams to full
// brightness at the very first 1/0 and never moves again (and a power-0.7 ease
// front-loaded it further: concave, so it READ as logarithmic). Here each step of
// lead adds an even, LINEAR quarter of the ramp: a subtle first step (±1 → 25%)
// climbing straight to full colour at the ±4 cap, symmetric in both directions.
const LEAD_CAP = 4;
function fractionColor(top: number, bottom: number): string {
  const lead = top - bottom; // signed: + favours focus (teal), − distraction (red)
  if (lead === 0) return `rgb(${FRAC_GREY.join(', ')})`; // tie (incl. 0/0) → grey
  const target = lead > 0 ? FRAC_TEAL : FRAC_RED;
  const mag = Math.min(1, Math.abs(lead) / LEAD_CAP); // linear, capped at ±4
  const mix = FRAC_GREY.map((g, i) => Math.round(g + (target[i] - g) * mag));
  return `rgb(${mix.join(', ')})`;
}

function CornerDial({ top, bottom }: { top: number; bottom: number }) {
  const tone = fractionColor(top, bottom);
  return (
    <div
      className="corner-dial"
      style={{ '--tone': tone } as React.CSSProperties}
      role="img"
      aria-label={`Focus ${top} over distraction ${bottom}`}
    >
      <span className="corner-dial__num">{top}</span>
      <span className="corner-dial__bar" aria-hidden />
      <span className="corner-dial__num">{bottom}</span>
    </div>
  );
}

// A compact up/down stepper for the demo bar — drives one number of the big
// corner dial's fraction. Down is clamped at zero (no negative counts).
function Stepper({ label, value, onChange }: { label: string; value: number; onChange: (v: number) => void }) {
  return (
    <div className="stepper">
      <span className="stepper__label">{label}</span>
      <b className="stepper__val">{value}</b>
      <span className="stepper__arrows">
        <button className="stepper__btn" onClick={() => onChange(value + 1)} aria-label={`Increment ${label}`}>▲</button>
        <button className="stepper__btn" onClick={() => onChange(Math.max(0, value - 1))} aria-label={`Decrement ${label}`}>▼</button>
      </span>
    </div>
  );
}

// The number band beside a demo slider: click (or focus + Enter/Space) to swap the
// readout for a text field and type a precise value — generic to every slider, so
// dragging gets you close and typing nails the exact number. `value` is the raw
// number being edited; `display` is the optional formatted label shown at rest
// (e.g. a break-time string) while the field still edits the underlying number.
// Commit on Enter or blur; Escape (or a non-numeric entry) cancels back to `value`.
function EditableNum({
  value,
  onCommit,
  display,
  step,
}: {
  value: number;
  onCommit: (v: number) => void;
  display?: string;
  step?: number;
}) {
  const [draft, setDraft] = useState<string | null>(null); // null ⇒ not editing
  const commit = () => {
    if (draft !== null) {
      const n = Number(draft);
      if (draft.trim() !== '' && Number.isFinite(n)) onCommit(n);
    }
    setDraft(null);
  };
  if (draft !== null) {
    return (
      <input
        className="demobar__num-edit"
        type="number"
        step={step ?? 'any'}
        autoFocus
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') commit();
          else if (e.key === 'Escape') setDraft(null);
        }}
      />
    );
  }
  return (
    <b
      className="demobar__num"
      tabIndex={0}
      role="button"
      title="Click to type an exact value"
      onClick={() => setDraft(String(value))}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          setDraft(String(value));
        }
      }}
    >
      {display ?? value}
    </b>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// The connecting arc — a static shape-finding study (NOT wired to real data).
//
// A single curve that springs off the break dial's rim and sweeps across to the
// LEFT border of the page, the first thread tying the top-right instrument into
// the rest of the cockpit. Three defining points, all in absolute viewport px:
//
//   L = (0, leftY)         — touches the left border (x=0) at a slider-driven y.
//   R = a rim point at polar angle θ_arc along the SAME track the break-time ball
//       walks (THETA_TOP … THETA_RIGHT). The hub centre sits at (W, −Y_SHIFT), so
//       R = (W − HUB_R·cosθ, HUB_R·sinθ − Y_SHIFT).
//   A = (maximaX, apexY)   — the local maximum. apexY = chordY(maximaX) − amplitude,
//       where chordY is the straight L→R interpolation at that x; amplitude is the
//       px of upward bow (0 → straight line, larger → taller hump).
//
// Path = two cubic segments meeting at A with a HORIZONTAL tangent there, so A is
// a true local maxima:
//   M L C (L.x+k1,L.y)(A.x−k1,A.y) A  C (A.x+k2,A.y)(R.x−k2,R.y) R
// with k1 = 0.4·(A.x−L.x), k2 = 0.4·(R.x−A.x).
//
// This reuses the rim-angle math but is DELIBERATELY OUTSIDE the breakHubView
// contract — thetaFrac is its own debug knob; the ball/trails/glow are untouched.
// ═══════════════════════════════════════════════════════════════════════════
// ── The arc as a reusable curve contract ────────────────────────────────────
// The connecting arc's geometry, promoted out of the render body into a small
// analytic object so EVERY consumer reads the exact same curve: the arc stroke,
// its opaque fill, the agent dials that ride it, and any future section border
// that wants to run parallel to it. One source of truth for f(x) — no polyline
// re-derivation, no second geometry to drift.
//
// It exposes not just the height f(x) but the LOCAL FRAME at each x — the
// analytic slope f'(x), the unit tangent, and the unit normal — so callers can
// place things TANGENT to the curve (a dial whose rim kisses the arc) or trace a
// PARALLEL offset curve (a border a fixed distance off the arc). Arc-length
// helpers give even visual spacing along the bend, not even spacing in x.
type Pt = { x: number; y: number };

interface ArcCurve {
  readonly domain: { x0: number; x1: number }; // x range the curve is defined on
  readonly L: Pt; // left contact (x = 0)
  readonly R: Pt; // right contact (x = W)
  readonly A: Pt; // apex (local maximum)
  f(x: number): number; // height at x
  slope(x: number): number; // f'(x) — analytic, not sampled
  point(x: number): Pt; // { x, f(x) }
  tangent(x: number): Pt; // unit tangent (dx, dy)
  normal(x: number, side?: number): Pt; // unit normal; side=+1 → the page (below) side
  offsetPoint(x: number, dist: number, side?: number): Pt; // point + dist·normal
  totalLength(): number; // arc length over the whole domain
  lengthAt(x: number): number; // cumulative arc length 0…x
  xAtLength(s: number): number; // inverse of lengthAt
  sampleByArcLength(count: number, x0: number, x1: number): number[]; // even-arc-len xs
  polyline(): string; // SVG path along the curve
  offsetPolyline(dist: number, side?: number): string; // SVG path along the offset curve
  discEntryX(cx: number, cy: number, r: number): number; // first x that enters a disc
}

// Build the arc from the same three defining inputs ArcLayer always used: the
// apex bow `amplitude`, its x as `maximaFrac`, and the two side-contact fractions.
// The contacts' y's are measured in the top BAND (HUB_BOTTOM), so the dialed-in
// shape is width-locked. Single cubic y=f(x)=a·x³+b·x²+c·x+d pinned by four
// constraints (through L and R, flat crest at the apex → true local max); Cramer
// on the 3×3, straight-chord fallback for degenerate geometry.
function createArc({
  W,
  amplitude,
  maximaFrac,
  rightYFrac,
  leftYFrac,
  samples = 96,
  scale = 1,
}: {
  W: number;
  amplitude: number;
  maximaFrac: number;
  rightYFrac: number;
  leftYFrac: number;
  samples?: number;
  scale?: number; // scales the HUB_BOTTOM-measured side contacts; caller passes
  //                already-scaled `amplitude` (the px bow). Fracs stay invariant.
}): ArcCurve {
  const L: Pt = { x: 0, y: leftYFrac * HUB_BOTTOM * scale };
  const R: Pt = { x: W, y: rightYFrac * HUB_BOTTOM * scale };
  const Rx = R.x;

  const Ax = maximaFrac * W;
  const chordT = R.x !== L.x ? (Ax - L.x) / (R.x - L.x) : 0;
  const chordY = L.y + (R.y - L.y) * chordT;
  const A: Pt = { x: Ax, y: chordY - amplitude };

  const det3 = (m: number[][]) =>
    m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) -
    m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0]) +
    m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]);
  const M = [
    [3 * Ax * Ax, 2 * Ax, 1], // f'(Ax) = 0
    [Ax * Ax * Ax, Ax * Ax, Ax], // f(Ax) = A.y  (minus d)
    [Rx * Rx * Rx, Rx * Rx, Rx], // f(Rx) = R.y  (minus d)
  ];
  const rhs = [0, A.y - L.y, R.y - L.y];
  const D = det3(M);

  // f(x) and its ANALYTIC derivative f'(x) — the slope feeds tangent/normal, so
  // it must be exact, not a finite difference. Degenerate ⇒ the straight chord.
  let f: (x: number) => number;
  let slope: (x: number) => number;
  if (!(Math.abs(D) > 1e-6) || Rx <= 0) {
    const m = Rx !== 0 ? (R.y - L.y) / Rx : 0;
    f = (x) => L.y + m * x;
    slope = () => m;
  } else {
    const solve = (col: number) => det3(M.map((row, i) => row.map((v, k) => (k === col ? rhs[i] : v)))) / D;
    const a = solve(0);
    const b = solve(1);
    const c = solve(2);
    const d0 = L.y;
    f = (x) => ((a * x + b) * x + c) * x + d0;
    slope = (x) => (3 * a * x + 2 * b) * x + c;
  }

  const point = (x: number): Pt => ({ x, y: f(x) });
  const tangent = (x: number): Pt => {
    const m = slope(x);
    const len = Math.hypot(1, m);
    return { x: 1 / len, y: m / len };
  };
  // Unit normal. side=+1 gives the DOWNWARD normal (+y, the page side the dials
  // hang on); side=−1 flips to the upward normal. Rotating the tangent (1,m) by
  // 90° → (−m,1), whose y is +1 so it points down in screen space.
  const normal = (x: number, side = 1): Pt => {
    const m = slope(x);
    const len = Math.hypot(1, m);
    return { x: (side * -m) / len, y: (side * 1) / len };
  };
  // A point offset `dist` off the curve along the normal — the atom both tangent
  // dial placement and parallel section borders are built from.
  const offsetPoint = (x: number, dist: number, side = 1): Pt => {
    const n = normal(x, side);
    return { x: x + dist * n.x, y: f(x) + dist * n.y };
  };

  // Arc-length table over the domain (x is evenly spaced, so index math is direct).
  const xs: number[] = [];
  const seg: number[] = [0];
  for (let i = 0; i <= samples; i++) xs.push(Rx > 0 ? (Rx * i) / samples : 0);
  for (let i = 1; i < xs.length; i++) {
    seg.push(seg[i - 1] + Math.hypot(xs[i] - xs[i - 1], f(xs[i]) - f(xs[i - 1])));
  }
  const total = seg[seg.length - 1];
  const lengthAt = (xq: number): number => {
    if (Rx <= 0 || xq <= 0) return 0;
    if (xq >= Rx) return total;
    const t = (xq / Rx) * samples;
    const i = Math.floor(t);
    return seg[i] + (seg[i + 1] - seg[i]) * (t - i);
  };
  const xAtLength = (sq: number): number => {
    if (sq <= 0) return 0;
    if (sq >= total) return Rx;
    let i = 1;
    while (i < seg.length && seg[i] < sq) i++;
    const s0 = seg[i - 1];
    const s1 = seg[i];
    const frac = s1 > s0 ? (sq - s0) / (s1 - s0) : 0;
    return xs[i - 1] + (xs[i] - xs[i - 1]) * frac;
  };
  const sampleByArcLength = (count: number, x0: number, x1: number): number[] => {
    if (count <= 0) return [];
    const s0 = lengthAt(x0);
    const s1 = lengthAt(x1);
    const out: number[] = [];
    for (let k = 0; k < count; k++) {
      const frac = count === 1 ? 0.5 : k / (count - 1);
      out.push(xAtLength(s0 + (s1 - s0) * frac));
    }
    return out;
  };
  const polyline = (): string => {
    let d = '';
    for (let i = 0; i < xs.length; i++) d += `${i === 0 ? 'M' : 'L'}${xs[i].toFixed(1)},${f(xs[i]).toFixed(1)} `;
    return d.trim();
  };
  const offsetPolyline = (dist: number, side = 1): string => {
    let d = '';
    for (let i = 0; i < xs.length; i++) {
      const p = offsetPoint(xs[i], dist, side);
      d += `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.y.toFixed(1)} `;
    }
    return d.trim();
  };
  const discEntryX = (cx: number, cy: number, r: number): number => {
    for (let i = 0; i < xs.length; i++) {
      const dx = xs[i] - cx;
      const dy = f(xs[i]) - cy;
      if (dx * dx + dy * dy < r * r) return xs[i];
    }
    return Rx;
  };

  return {
    domain: { x0: 0, x1: Rx },
    L,
    R,
    A,
    f,
    slope,
    point,
    tangent,
    normal,
    offsetPoint,
    totalLength: () => total,
    lengthAt,
    xAtLength,
    sampleByArcLength,
    polyline,
    offsetPolyline,
    discEntryX,
  };
}

// ═══════════════════════════════════════════════════════════════════════════
// SHARED LEMON GEOMETRY + FLOOR ENVELOPE — one source, ArcLayer + the worker layer.
//
// lemonGeometry factors the arc + lens span + bottom-boundary quadratic `wy` into
// a single helper so ArcLayer (which renders the lemon) and the worker layer (which
// rides its underside) read the SAME numbers and can never drift apart. It reads the
// frozen ARC / LEMON_WIDTH_INSET / LEMON_DEPTH module constants directly (all settled,
// no longer knobs), so it needs only the measured width + the one uiScale factor.
// ═══════════════════════════════════════════════════════════════════════════
interface LemonGeometry {
  arc: ArcCurve; // the connecting arc as one analytic y=f(x) curve
  Rx: number; // arc domain right edge (= measured W)
  xL: number; // left lens tip x
  xR: number; // right lens tip x
  xMid: number; // span centre — the lemon apex + T-rail stem x
  xCtr: number; // arc-LENGTH centre — the true middle section divider (bxs[3])
  hasSpan: boolean; // false when the lens degenerates (tiny width)
  wy: (x: number) => number; // lemon BOTTOM boundary y (falls back to arc.f off-span)
}

function lemonGeometry(W: number, uiScale: number): LemonGeometry {
  const arc = createArc({
    W,
    amplitude: ARC.amplitude * uiScale,
    maximaFrac: ARC.maximaFrac,
    rightYFrac: ARC.rightYFrac,
    leftYFrac: ARC.leftYFrac,
    scale: uiScale,
  });
  const Rx = arc.domain.x1;

  // Lemon SPAN — each tip INDEPENDENTLY clamped to [xLo, W − RIGHT_INSET] (see the
  // ArcLayer lemon note). xLo clears a persona ring off the left border; RIGHT_INSET
  // clears the hub disc. LEMON_WIDTH_INSET is pulled off each side (0 = full width).
  const RIM_DIAL_R = 45 * uiScale; // former "1" persona MEDIUM radius — left clearance
  const AGENT_GRAPH_INSET = 18 * uiScale;
  const xLo = AGENT_GRAPH_INSET + RIM_DIAL_R;
  const RIGHT_INSET = 40 * uiScale;
  const xHi = W - RIGHT_INSET;
  const inset = LEMON_WIDTH_INSET * uiScale;
  const xL = clamp(xLo, xLo + inset, xHi);
  const xR = clamp(xLo, xHi - inset, xHi);
  const hasSpan = Rx > 0 && xR > xL;
  const xMid = (xL + xR) / 2;
  // Arc-LENGTH centre — the same midpoint ArcLayer's equal-arc-length dividers use
  // (bxs[3], since the tip inset is symmetric in arc-length). On a curved/asymmetric
  // arc this differs from the x-midpoint, so stem + dial fan + bar axis all read THIS
  // to land on the lemon's true middle divider rather than beside it.
  const xCtr = hasSpan ? arc.xAtLength((arc.lengthAt(xL) + arc.lengthAt(xR)) / 2) : xMid;

  // Bottom boundary — symmetric quadratic through the two tips (on the arc) + a
  // centre apex LEMON_DEPTH below the crest. Off-span it degenerates to the arc.
  let wy: (x: number) => number;
  if (hasSpan) {
    const halfSpan = (xR - xL) / 2;
    const tipLY = arc.f(xL);
    const tipRY = arc.f(xR);
    const c0 = arc.f(xMid) + LEMON_DEPTH * uiScale;
    const a0 = (tipLY + tipRY - 2 * c0) / (2 * halfSpan * halfSpan);
    const b0 = (tipRY - tipLY) / (2 * halfSpan);
    wy = (x: number): number => { const dx = x - xMid; return a0 * dx * dx + b0 * dx + c0; };
  } else {
    wy = (x: number): number => arc.f(x);
  }

  return { arc, Rx, xL, xR, xMid, xCtr, hasSpan, wy };
}

// Fixed corner-dial inset — mirrors .corner-dial { top: 7px; right: 7px } (NOT
// scaled). Its diameter is CORNER_DIAL_PX·uiScale, so its bottom edge sits at
// CORNER_DIAL_INSET + CORNER_DIAL_PX·uiScale over x-span [W−7−d, W−7].
const CORNER_DIAL_INSET = 7;

// The worker FLOOR — the LOWEST (max-y) boundary line at x among the arc, the
// lemon bottom (only across the lens span), and the corner dial's bottom edge (only
// under its x-span). Chips ride this line + a gap, so they tuck just under whichever
// instrument hangs lowest instead of floating below everything.
function workerFloorY(x: number, geo: LemonGeometry, uiScale: number): number {
  let y = geo.arc.f(x); // the arc is always in play
  if (geo.hasSpan && x >= geo.xL && x <= geo.xR) y = Math.max(y, geo.wy(x));
  const dialD = CORNER_DIAL_PX * uiScale;
  const dialL = geo.Rx - CORNER_DIAL_INSET - dialD;
  const dialR = geo.Rx - CORNER_DIAL_INSET;
  if (x >= dialL && x <= dialR) y = Math.max(y, CORNER_DIAL_INSET + dialD);
  return y;
}

// ═══════════════════════════════════════════════════════════════════════════
// Worker-queue path — the line the worker chips ride. It FOLLOWS the floor envelope:
// sampled from the stem base (the lemon's centre divider xCtr) outward to the arm end
// (inset short of the screen edge) at workerFloorY(x) + gap, into a cumulative chord-
// length table so chips space EVENLY regardless of the floor's curvature — the chips hug
// the lemon + arc underside. pointAt(s) maps arc-length s (from the stem) to (x, y); past
// totalLen it continues STRAIGHT DOWN so a long queue drains down the edge without
// crossing it. The curved gold crossbar (see the rail memo) sits BELOW this row as a
// separate baseline — the table's top edge — it is not the line the chips ride.
// ═══════════════════════════════════════════════════════════════════════════
type QueuePt = { x: number; y: number };
interface QueuePath {
  pointAt(s: number): QueuePt; // s = arc-length from the stem base
  totalLen: number; // arm length (the straight-down tail continues past it)
}

// Locked worker-queue layout — px authored at DESIGN_W (1440), × uiScale at consumption.
// Dialed in by eye then frozen; the demo-bar sliders that tuned them are retired.
const W_DROP_PX = 53; // chip-row drop below the header floor — dials clear of the lemon/arc
const W_SPACE_PX = 99; // queue spacing: chip centre-to-centre so the 90px dials never overlap
const W_INSET_PX = 50; // drains hug the very edges — columns sit flush at the sides
const W_SPLIT_PX = 93; // slot-0 gap from centre stem (centre clearance for the fat chips)

function makeQueuePath(side: number, geo: LemonGeometry, uiScale: number, gap: number, inset: number): QueuePath {
  const s = uiScale;
  const gapPx = gap * s;
  // From the stem base (the lemon's true centre divider) outward to the arm end, short
  // of the edge — riding the floor envelope (lemon + arc underside) + gap. Both columns
  // fan symmetrically from geo.xCtr so stem, fan, and bar axis stay coherent.
  const xStart = geo.xCtr;
  const xEnd = side < 0 ? inset * s : geo.Rx - inset * s;
  const N = 96;
  const pts: QueuePt[] = [];
  const seg: number[] = [0];
  for (let i = 0; i <= N; i++) {
    const x = xStart + (xEnd - xStart) * (i / N);
    pts.push({ x, y: workerFloorY(x, geo, uiScale) + gapPx });
  }
  for (let i = 1; i < pts.length; i++) seg.push(seg[i - 1] + Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y));
  const totalLen = seg[seg.length - 1];
  const end = pts[pts.length - 1];
  const pointAt = (sq: number): QueuePt => {
    if (sq <= 0) return pts[0];
    // Past the arm end the tail drops STRAIGHT DOWN so the queue drains down the edge
    // without crossing it (the floor line's slope isn't extended outward).
    if (sq >= totalLen) return { x: end.x, y: end.y + (sq - totalLen) };
    let i = 1;
    while (i < seg.length && seg[i] < sq) i++;
    const s0 = seg[i - 1], s1 = seg[i];
    const frac = s1 > s0 ? (sq - s0) / (s1 - s0) : 0;
    return {
      x: pts[i - 1].x + (pts[i].x - pts[i - 1].x) * frac,
      y: pts[i - 1].y + (pts[i].y - pts[i - 1].y) * frac,
    };
  };
  return { pointAt, totalLen };
}

// ═══════════════════════════════════════════════════════════════════════════
// SEGMENT — the shared "lit compartment" atom. Both the lemon's persona sections and
// the worker hourglass cells are Segments: a closed `region` fill that, when `glow` is
// on, lights with an inner rim glow + a soft central core in `tone`. On the lemon the
// glow is a per-persona indicator (toggleable off); on the hourglass it's the reservist
// indicator (load-bearing). cx/cy/gr place the core radial. Because both layers speak
// this one type, the same SegmentGlowLayer renders both.
// ═══════════════════════════════════════════════════════════════════════════
interface Segment {
  region: string; // closed fill path (the compartment outline)
  tone: string; // glow colour
  glow: boolean; // interior glow on/off (reservist / persona indicator)
  cx: number; // core radial centre x
  cy: number; // core radial centre y
  gr: number; // section width metric → core radius (× 0.5)
}

// Renders the interior glow for a set of Segments: a shared soft blur, one region clip +
// core gradient per LIT segment, then the border-in rim + central core, screen-blended.
// `idPrefix` namespaces the SVG ids so multiple layers (lemon "sec", hourglass "hour")
// coexist in one document. Segments with glow:false are skipped (that IS the disable).
function SegmentGlowLayer({ segments, idPrefix, blur, rimW }: {
  segments: Segment[]; idPrefix: string; blur: number; rimW: number;
}) {
  const lit = segments.map((s, i) => ({ s, i })).filter((e) => e.s.glow);
  if (!lit.length) return null;
  return (
    <>
      <filter id={`${idPrefix}-blur`} x="-50%" y="-50%" width="200%" height="200%">
        <feGaussianBlur stdDeviation={blur.toFixed(2)} />
      </filter>
      {lit.map(({ s, i }) => (
        <clipPath key={`c${i}`} id={`${idPrefix}-clip-${i}`}><path d={s.region} /></clipPath>
      ))}
      {lit.map(({ s, i }) => (
        <radialGradient key={`g${i}`} id={`${idPrefix}-core-${i}`} gradientUnits="userSpaceOnUse"
          cx={s.cx.toFixed(1)} cy={s.cy.toFixed(1)} r={(s.gr * 0.5).toFixed(1)}>
          <stop offset="0%" stopColor={s.tone} stopOpacity={0.32} />
          <stop offset="100%" stopColor={s.tone} stopOpacity={0} />
        </radialGradient>
      ))}
      {lit.map(({ s, i }) => (
        <g key={`p${i}`} clipPath={`url(#${idPrefix}-clip-${i})`} style={{ color: s.tone }}>
          <path className="section-rim" d={s.region} strokeWidth={rimW} filter={`url(#${idPrefix}-blur)`} />
          <path className="section-core" d={s.region} fill={`url(#${idPrefix}-core-${i})`} />
        </g>
      ))}
    </>
  );
}

function ArcLayer({ uiScale }: {
  uiScale: number; // one viewport-derived factor scaling every instrument length
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [dims, setDims] = useState({ w: 1000, h: 800 });

  // Measure the overlay in real px so the 1:1 viewBox never distorts the curve.
  useEffect(() => {
    const el = wrapRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (r && r.width && r.height) setDims({ w: Math.floor(r.width), h: Math.floor(r.height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const { w: W, h: H } = dims;

  // The shared lemon geometry — the SAME helper the worker layer reads, so the arc,
  // lens span, and bottom-boundary quadratic can never diverge between the two. The
  // stroke, the fill, the section dividers, and the worker floor all source from here.
  const geo = lemonGeometry(W, uiScale);
  const arc = geo.arc;
  const Rx = geo.Rx;

  // The break wheel disc, in this layer's coords (shared top-right origin) — its
  // centre + radius match the .break-hub circle exactly. It punches a disc-shaped
  // hole in BOTH the opaque fill and the arc stroke (via #arc-wheel-clip), so the
  // now-opaque dial shows through that hole and covers whatever the arc runs
  // behind it — the solid disc does the visual cutoff, no per-point truncation.
  const clipR = HUB_R * uiScale;
  const clipCx = W;
  const clipCy = -Y_SHIFT * uiScale;

  // The single polyline: both the fill's top edge and the stroke. The wheel-disc
  // clip removes the run behind the now-opaque dial, so the dial does the cutoff.
  const d = arc.polyline();

  // The arc is the TOP EDGE of an opaque mask: close the outline straight down to
  // the layer's bottom and back, filling everything UNDER the curve with page
  // metal so the timer graph never shows through below the arc (it reads only
  // above the line). The fill shares the wheel clip below, so the break hub disc
  // stays punched out and visible.
  const fillD = `${d} L${Rx.toFixed(1)},${H.toFixed(1)} L0,${H.toFixed(1)} Z`;

  // evenodd disc-punch clip builder (full rect MINUS a disc of radius `r`). The
  // OPAQUE fill uses the true rim radius so its panel butts exactly to the rim.
  const discPunchClip = (r: number) =>
    `M-10000,-10000 H10000 V10000 H-10000 Z ` +
    `M${(clipCx - r).toFixed(1)},${clipCy.toFixed(1)} ` +
    `a${r},${r} 0 1,0 ${(2 * r).toFixed(1)},0 ` +
    `a${r},${r} 0 1,0 ${(-2 * r).toFixed(1)},0 Z`;
  const clipPath = discPunchClip(clipR);
  // The arc LINE gets a clip a hair SMALLER than the disc so its rounded end runs
  // ~2.5px INTO the rim instead of butt-cutting exactly on the disc boundary — the
  // round cap then lands ON the rim stroke and the two read as one curve branching
  // off the circle (no gap, no visible clip edge). Fill keeps the true radius so
  // its opaque panel still butts cleanly to the rim.
  const LINE_RIM_OVERLAP = 2.5;
  const lineClipPath = discPunchClip(clipR - LINE_RIM_OVERLAP);

  // ── Persona dials riding the arc ────────────────────────────────────────────
  // The five STATIC persona dials are welded to the SAME `arc` curve above — one
  // curve, every consumer, so they track it at every viewport width with no second
  // geometry to drift. Each dial's contact point is sampled on the arc; its CENTRE
  // is that point pushed off the curve along the NORMAL, so the ring sits parallel
  // to the bend (not hung straight down across it).
  //
  // Placement is a RIGHT-ANCHORED lattice: slot 0 ("1", Custodes) is pinned in the
  // pocket and each further slot steps LEFT by one CONSTANT pitch (PERSONA_PITCH) —
  // a fixed centre-to-centre, NOT a width-derived span split — so the roster keeps
  // its spacing at every width (fullscreen never spreads it); only the pocket anchor
  // slides. Exactly PERSONA_COUNT dials. The generic worker dials are a SEPARATE flat
  // row below the arc (see the worker block); this replaces the old count-driven
  // AGENT_SLOTS stack, which is gone — personas are a fixed roster, not a knob.
  //
  // Per-slot size class (k=0 → "1" rightmost … k=5 → "6" leftmost). Current call:
  //   LARGE  → "2" / "3" / "4"
  //   MEDIUM → "1" / "5"  (the inner pair flanking the large centre trio)
  //   SMALL  → "6"        (the newest, leftmost dial — the row tapers to it)
  // The size classes step DOWN toward the left tail: the roster reads heaviest at
  // the centre, lighter at the "1"/"5" ends, lightest at the new "6".

  // ── Section-icon roster (replaces the 1–6 number dials) ──────────────────────
  // The lemon is divided into PERSONA_COUNT equal-arc-length sections, each with a
  // single persona icon centred in it and a full divider line — spanning between the
  // top arc and the bottom lemon arc — at every interior boundary. COLOUR lives ONLY
  // in the icons (per-section tone below); both arcs and the dividers stay brass
  // (--instrument). The roster is the six standing command personas. Sections build
  // left→right along the arc, so k=0 is the MOST-LEFT tip and k=5 the FAR-RIGHT:
  //   Malcador · Fabricator-General · Custodes · CI · Pax · Administratum.
  // (Custodes — the custodian-helmet glyph — sits center-left at k=2; CI and Pax
  // follow at k=3/k=4.)
  // Three of them (Malcador, Pax, CI) are FULL-COLOUR brand images (personaImage), not tintable
  // glyphs — the render branches on that below. The tone palette still lights each
  // section's glow (curated later); it just no longer recolours the image personas.
  const SECTION_PERSONAS = ['malcador', 'fabricator-general', 'custodes', 'ci', 'pax', 'administratum'];
  const SECTION_TONES = ['var(--good)', 'var(--warn)', 'var(--bad)', 'var(--neutral)', 'var(--idle)', 'var(--brass-bright)'];
  const ICON_PX = 40 * uiScale; // rendered icon box (the glyph's 512 viewBox scaled to this)
  const IMG_PX = 52 * uiScale; // image-persona box — brand art carries its own padding,
  //                              so it rides a touch larger to match the glyphs' weight.
  // Small fixed downward nudge off the lemon midline — icons read best a hair below
  // dead-centre between the two arcs (dialed in by eye; no longer a live knob).
  const ICON_NUDGE = 2 * uiScale;
  // The six-section band is pulled IN from each tip by this fraction of the span's
  // arc-length, so the end sections sit where the lens is tall enough for an icon
  // (not jammed into the pinched tips). The tapered ends outside the band stay empty.
  const SECTION_TIP_INSET_FRAC = 0.1;
  // Glow shaping. Each segment lights from TWO soft sources: a rim glow that hugs the
  // segment border and fades inward (a blurred stroke clipped to the region), plus a
  // smaller, gentler central core. Blur + rim width × uiScale so the glow tracks scale.
  const LEMON_BLUR = 9 * uiScale; // gaussian blur stdDeviation for both rim glows
  const RIM_W = 11 * uiScale; // rim-stroke width; clipped to the region → inner glow

  // ── Lemon layout ─────────────────────────────────────────────────────────────
  // The connecting arc is the UPPER boundary of a lens ("lemon"); a mirrored INVERTED
  // arc (the bottom-boundary quadratic wy) closes it below, the two meeting on a shared
  // pair of tips. Span (xL/xR/xMid), hasSpan, and wy all come from the SHARED helper
  // (lemonGeometry) so the render here and the worker floor below never diverge. The
  // top arc between the tips is divided into six icon sections; both boundaries + the
  // section dividers stay brass, colour lives only in the icons.
  const { xL, xR, hasSpan, wy } = geo;
  const sections: { cx: number; cy: number; inner: string; img?: string | undefined; tone: string; region: string; gr: number; glow: boolean }[] = [];
  const dividers: { x1: number; y1: number; x2: number; y2: number }[] = [];
  const caps: string[] = []; // the two tapered tip regions, filled solid gold
  let lemonArcD = '';
  if (hasSpan) {
    // Bottom lemon boundary — the solid gold inverted arc closing the lens.
    const N = 64;
    const pts: string[] = [];
    for (let i = 0; i <= N; i++) {
      const x = xL + (i / N) * (xR - xL);
      pts.push(`${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${wy(x).toFixed(1)}`);
    }
    lemonArcD = pts.join(' ');

    // A closed fill region bounded by the top arc (xa→xb) and the bottom arc (xb→xa) —
    // the atom for both the gold tip caps and the per-section colour glows.
    const regionPath = (xa: number, xb: number): string => {
      const M = 24;
      const seg: string[] = [];
      for (let i = 0; i <= M; i++) { const x = xa + (i / M) * (xb - xa); seg.push(`${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${arc.f(x).toFixed(1)}`); }
      for (let i = M; i >= 0; i--) { const x = xa + (i / M) * (xb - xa); seg.push(`L${x.toFixed(1)},${wy(x).toFixed(1)}`); }
      return seg.join(' ') + ' Z';
    };

    // Section boundaries — equal-arc-length over an INSET slice of the top arc, pulled
    // in from the tips (SECTION_TIP_INSET_FRAC) so the end sections clear the pinch.
    const sL = arc.lengthAt(xL);
    const sR = arc.lengthAt(xR);
    const sIn0 = sL + (sR - sL) * SECTION_TIP_INSET_FRAC;
    const inSpan = (sR - sL) * (1 - 2 * SECTION_TIP_INSET_FRAC);
    const bxs: number[] = [];
    for (let k = 0; k <= PERSONA_COUNT; k++) bxs.push(arc.xAtLength(sIn0 + inSpan * (k / PERSONA_COUNT)));

    // Interior sections — each carries its region (for a tone-tinted glow), an icon
    // centred on the lemon MIDLINE at its arc-length midpoint (+ ICON_NUDGE), and a glow
    // radius sized to the section width.
    for (let k = 0; k < PERSONA_COUNT; k++) {
      const xm = arc.xAtLength(sIn0 + inSpan * ((k + 0.5) / PERSONA_COUNT));
      const cy = (arc.f(xm) + wy(xm)) / 2 + ICON_NUDGE;
      // Image personas (Pax, CI) resolve to a brand-asset URL; glyph personas to a
      // single-path currentColor SVG. `img` wins in the render (the <image> branch).
      const img = personaImage(SECTION_PERSONAS[k]);
      sections.push({
        cx: xm, cy,
        inner: img ? '' : personaIconInner(SECTION_PERSONAS[k]) ?? '',
        img,
        tone: SECTION_TONES[k],
        region: regionPath(bxs[k], bxs[k + 1]),
        gr: (bxs[k + 1] - bxs[k]) * 0.62,
        glow: true, // per-persona indicator — flip off to disable this segment's glow
      });
    }
    // Dividers run full-height between the arcs at every boundary (band edges + interior).
    for (let k = 0; k <= PERSONA_COUNT; k++) {
      dividers.push({ x1: bxs[k], y1: arc.f(bxs[k]), x2: bxs[k], y2: wy(bxs[k]) });
    }
    // Tip caps — the tapered ends outside the band, filled solid gold like the arcs.
    caps.push(regionPath(xL, bxs[0]));
    caps.push(regionPath(bxs[PERSONA_COUNT], xR));
  }

  return (
    <div className="arc-layer" ref={wrapRef} aria-hidden>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} style={{ overflow: 'visible' }}>
        <defs>
          <clipPath id="arc-wheel-clip">
            <path d={clipPath} clipRule="evenodd" />
          </clipPath>
          <clipPath id="arc-line-clip">
            <path d={lineClipPath} clipRule="evenodd" />
          </clipPath>
          {/* shared soft blur for the rim glows (segments + gold caps). */}
          <filter id="lemon-glow-blur" x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation={LEMON_BLUR.toFixed(2)} />
          </filter>
          {/* tip-cap region clips — the gold rim glow stays inside each tapered end. */}
          {caps.map((d, k) => (
            <clipPath key={`cc${k}`} id={`cap-clip-${k}`}><path d={d} /></clipPath>
          ))}
        </defs>
        <path className="arc-fill" d={fillD} clipPath="url(#arc-wheel-clip)" />
        {/* gold tip caps — a gold rim glow hugging the tapered end, fading to a dark
            centre (was a solid gold fill). */}
        {caps.map((d, k) => (
          <g key={`cap${k}`} clipPath={`url(#cap-clip-${k})`}>
            <path className="lemon-cap-rim" d={d} strokeWidth={RIM_W} filter="url(#lemon-glow-blur)" />
          </g>
        ))}
        {/* per-section glows — a border-in rim glow plus a smaller central core, both in
            the segment's own tone, screen-blended onto the dark metal. Shared renderer,
            same one the worker hourglass uses (Segment). */}
        <SegmentGlowLayer segments={sections} idPrefix="sec" blur={LEMON_BLUR} rimW={RIM_W} />
        {/* lower lemon boundary — the inverted arc, a SOLID gold instrument line that
            mirrors the connecting arc and closes the lens between the shared tips. */}
        {lemonArcD && <path className="lemon-arc" d={lemonArcD} />}
        {/* agent dials — placeholder fleet rings riding the SAME f(x) as the arc.
            Between the opaque .arc-fill (painted first, so the rings read on the
            page metal instead of being masked by it) and the .arc-line (painted
            last, so the arc threads OVER their top edge). Welded to the arc's own
            coord space, so they stay on the curve at every width. */}
        <g className="agent-dials">
          {/* interior section dividers — full brass lines spanning between the arcs */}
          {dividers.map((t, k) => (
            <line key={`dv${k}`} className="section-divider"
              x1={t.x1.toFixed(1)} y1={t.y1.toFixed(1)} x2={t.x2.toFixed(1)} y2={t.y2.toFixed(1)} />
          ))}
          {/* per-section icons. GLYPH personas are natively embedded (their 512 viewBox
              scaled to ICON_PX, re-centred on the origin) and tinted to the section tone.
              IMAGE personas (Malcador portrait, Pax avatar, CI monogram) render as a full-colour <image>,
              centred on the same point at a slightly larger box (brand art carries its
              own padding), and are NOT tinted — they keep their own colours. */}
          {sections.map((sc, k) =>
            sc.img ? (
              <image key={`sec${k}`} className="section-image" href={sc.img}
                x={(sc.cx - IMG_PX / 2).toFixed(1)} y={(sc.cy - IMG_PX / 2).toFixed(1)}
                width={IMG_PX.toFixed(1)} height={IMG_PX.toFixed(1)}
                preserveAspectRatio="xMidYMid meet" />
            ) : (
              <g key={`sec${k}`} className="section-icon" style={{ color: sc.tone }}
                transform={`translate(${sc.cx.toFixed(1)} ${sc.cy.toFixed(1)}) scale(${(ICON_PX / 512).toFixed(4)}) translate(-256 -256)`}
                dangerouslySetInnerHTML={{ __html: sc.inner }} />
            ),
          )}
        </g>
        {/* full curve, clipped a hair INSIDE the wheel disc — the arc's round cap
            overlaps onto the rim so it reads as one line forking off the circle;
            the opaque dial still covers the deeper run behind it (no truncation). */}
        <path className="arc-line" d={d} clipPath="url(#arc-line-clip)" />
      </svg>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// WORKER QUEUES — two icon-chip stacks BELOW the lemon that grow OUTWARD from
// screen centre then trail DOWN the two edges (a soft "M" with the lemon between).
// Each queue is INDEPENDENT (its own entries + seqRef): clicking a chip pops it
// off and the rest scoot up to close the gap. Position is driven ONLY by `slot`
// via a transform + a CSS `transition: transform 320ms ease`, so every slot
// change animates for free — the exact reflow model TtsStack uses (see its note).
// ═══════════════════════════════════════════════════════════════════════════

// Placeholder worker roster — persona (→ icon) + tint cycled per chip. Sourced
// from the full Astartes faction set so the worker row exercises the whole
// heraldry registry as the count grows (i % length cycles it below). Colours
// keep their own independent 6-tone cycle (WORKER_TONES), curated later.
const WORKER_PERSONAS = FACTION_PERSONAS;
const WORKER_TONES = ['var(--brass-bright)', 'var(--good)', 'var(--warn)', 'var(--bad)', 'var(--neutral)', 'var(--idle)'];
const WORKER_CHIP_PX = 90; // chip diameter, px @1440 (× uiScale) — big worker dials (75% of the first 120px pass)
const WORKER_BAR_MARGIN = 30; // gap between the chip-row bottom and the gold crossbar below
const WORKER_DISMISS_MS = 300; // matches the worker-dismiss keyframe

type WorkerPhase = 'idle' | 'dismissing';
interface WorkerEntry {
  key: string;
  persona: string;
  tone: string;
  slot: number; // visual row (0 = head, nearest centre); the sole position driver
  phase: WorkerPhase;
}

// One side of the "M": a self-contained reflowing queue riding `path`. Mirrors
// TtsStack — stable-order array, monotonic keys, slot-only positioning — minus the
// promote/speak gesture (a click is a plain remove).
function WorkerColumn({ side, count, geo, uiScale, gap, pitch, inset, split }: {
  side: number; count: number; geo: LemonGeometry; uiScale: number;
  gap: number; pitch: number; inset: number; split: number;
}) {
  const seqRef = useRef(0);
  const [entries, setEntries] = useState<WorkerEntry[]>(() =>
    Array.from({ length: count }, (_, i) => ({
      key: `w${seqRef.current++}`,
      persona: WORKER_PERSONAS[i % WORKER_PERSONAS.length],
      tone: WORKER_TONES[i % WORKER_TONES.length],
      slot: i,
      phase: 'idle' as WorkerPhase,
    })),
  );

  // Timers tracked so an unmount / hot-reload can't fire setState on a dead node.
  const timers = useRef<number[]>([]);
  useEffect(() => () => timers.current.forEach((t) => clearTimeout(t)), []);
  const after = (ms: number, fn: () => void) => {
    timers.current.push(window.setTimeout(fn, ms));
  };

  // Depth slider = grow/shrink the TAIL only, keyed off SLOT (see TtsStack). Fresh
  // cycled entries append on grow; the deepest slots drop on shrink.
  useEffect(() => {
    setEntries((prev) => {
      const n = prev.length;
      if (count === n) return prev;
      if (count < n) return prev.filter((e) => e.slot < count); // drop deepest rows
      const add: WorkerEntry[] = [];
      for (let i = n; i < count; i++) {
        add.push({
          key: `w${seqRef.current++}`,
          persona: WORKER_PERSONAS[i % WORKER_PERSONAS.length],
          tone: WORKER_TONES[i % WORKER_TONES.length],
          slot: i,
          phase: 'idle',
        });
      }
      return [...prev, ...add];
    });
  }, [count]);

  // Click-to-remove (ANY element): mark dismissing (scale→0 + fade keyframe), then
  // after the exit filter the entry out AND decrement every deeper slot — that one
  // step is the whole reflow, and it works for any position. The survivors animate
  // up-path via the transform transition.
  function remove(key: string) {
    const target = entries.find((e) => e.key === key);
    if (!target || target.phase === 'dismissing') return;
    setEntries((prev) => prev.map((e) => (e.key === key ? { ...e, phase: 'dismissing' as WorkerPhase } : e)));
    after(WORKER_DISMISS_MS, () =>
      setEntries((prev) => {
        const gone = prev.find((e) => e.key === key);
        if (!gone) return prev;
        return prev
          .filter((e) => e.key !== key)
          .map((e) => (e.slot > gone.slot ? { ...e, slot: e.slot - 1 } : e));
      }),
    );
  }

  const path = useMemo(() => makeQueuePath(side, geo, uiScale, gap, inset), [side, geo, uiScale, gap, inset]);
  const chip = WORKER_CHIP_PX * uiScale;

  // A persona must be unique within this queue — the DB's invariant. When the data
  // breaks it (WORKER_PERSONAS cycles once count exceeds the roster), the repeats
  // are surfaced with a red error glow, not silently collapsed (see the helper).
  const dupKeys = duplicatePersonaKeys(entries, (e) => e.slot, (e) => e.persona, (e) => e.key);

  return (
    <>
      {entries.map((e) => {
        const isDup = dupKeys.has(e.key);
        // slot 0 sits `split` out from the stem (centre clearance); each further slot
        // marches one `pitch` outward along the arm.
        const p = path.pointAt((split + e.slot * pitch) * uiScale);
        return (
          <button
            key={e.key}
            type="button"
            className="worker-chip"
            aria-label={`Worker ${e.persona}${isDup ? ' — DUPLICATE (singleton breach)' : ''} — dismiss`}
            style={{
              width: chip,
              height: chip,
              // slot expressed PURELY as a transform → any slot change animates for free.
              transform: `translate(${p.x.toFixed(1)}px, ${p.y.toFixed(1)}px) translate(-50%, -50%)`,
            }}
            onClick={() => remove(e.key)}
          >
            <span
              className={`worker-chip__disc${e.phase === 'dismissing' ? ' worker-chip__disc--out' : ''}${isDup ? ' worker-chip__disc--dup' : ''}`}
              style={{ color: e.tone }}
            >
              {personaIcon(e.persona)}
            </span>
          </button>
        );
      })}
    </>
  );
}

// ── LOCKED: Rail shape constants ────────────────────────────────────────────
// The by-eye shape constants for the worker-rail crossbar + centre hourglass, now
// FROZEN at the operator's settled values (the demo-bar sliders + `shape` prop have
// been retired, matching the lemon/layout knobs before them — LEMON_WIDTH_INSET etc.).
// WorkerQueues reads RAIL_SHAPE_DEFAULTS directly. Two unit families: the px@1440
// fields (hgLift/nestClear/hgFoot/capInset) are base numbers multiplied by `s` at
// use; the rest are unitless ratios/fractions.
interface RailShape {
  barExag: number;      // BAR_EXAG — bar bow (ratio)
  hgLift: number;       // HG_LIFT — centre lift (px@1440)
  hgLiftSpan: number;   // HG_LIFT_SPAN — lift spread (frac)
  endRiseMult: number;  // HG_END_RISE_MULT — end round amount (ratio on auto rise)
  hgEndSpan: number;    // HG_END_SPAN — end round span (frac)
  hgTipFrac: number;    // HG_TIP_FRAC — mouth width (frac)
  nestClear: number;    // nestClear — worker hug (px@1440)
  hgFoot: number;       // HG_FOOT — foot width (px@1440)
  hgCf: number;         // HG_CF — wall verticality (frac)
  // ── Table-edge BAND (the crossbar as a closed ribbon, not a single line) ──
  // The bottom edge stays the symmetric `crossY` (the hourglass plants on it,
  // unchanged); a NEW top edge hugs the worker dials, and the two enclose a filled
  // table-lip. topFollow blends the top edge from symmetric (0) to dial-hugging (1).
  topHug: number;       // gap from the chip bottoms to the band's TOP edge (px@1440)
  topFollow: number;    // 0 = symmetric top edge, 1 = follows the worker dials (frac)
  bandFill: number;     // ribbon interior fill opacity (0 = hollow outline)
  capInset: number;     // pull each lobe's OUTER terminus (line-stop + cap) in from the ditch (px@1440)
}
const RAIL_SHAPE_DEFAULTS: RailShape = {
  barExag: 1.05,
  hgLift: 24,
  hgLiftSpan: 0.68,
  endRiseMult: 1.1,
  hgEndSpan: 0.72,
  hgTipFrac: 0.68,  // mouth width — dialed in
  nestClear: 16,    // worker hug — dialed in
  hgFoot: 0,
  hgCf: 0.36,
  topHug: 13,       // band top tucks just under the dials
  topFollow: 1.0,   // top edge follows the dials
  bandFill: 0.14,   // faint brass table surface
  capInset: 55,     // RIGHT cap pulled in 55px to clear the drain; left stays at its terminus
};
// The reservist hourglass (centre glow cells + I-walls) is LOCKED temporarily at the
// operator's settled shape — leave its geometry as-is; ongoing tuning is the crossbar.

// ── Compass dial (RHS cap bulge) ───────────────────────────────────────────
// A compass rose inscribed in the crossbar's right-hand cap: a rim circle carrying
// interior radial ticks — four LONG cardinal ticks (N/E/S/W) and four SHORT ordinal
// ticks (NE/SE/SW/NW) pointing inward from the rim — a two-sided N/S needle (red
// north, white south) on the pivot hub, and a tiny GLOWING star at each of the eight
// rim ticks. By-eye fractions of the cap radius; tune these to seat the dial.
const COMPASS_R_FRAC = 1.0;       // rim radius = the cap radius — the compass IS the endcap circle
const COMPASS_CARD_FRAC = 0.11;   // cardinal tick length as a fraction of the rim radius (small nub)
const COMPASS_ORD_FRAC = 0.07;    // ordinal tick length as a fraction of the rim radius (smaller nub)
const COMPASS_POINTER_FRAC = 1.9;  // outer pointer size as a fraction of the rim radius (its 100-box scale)
const COMPASS_POINTER_NEST = 0.6;  // inner (smaller, on-top) pointer size as a fraction of the outer
const COMPASS_HUB_FRAC = 0.028;    // brass hub radius on the intersection, fraction of the outer pointer size
const COMPASS_SPIN_SEC = 120;      // seconds per full needle revolution (0 = static) — 2 min, 30s per cardinal
// Rim stars are no longer authored as raw positions — they're the canonical
// output of the star-reduction algebra (see compass.ts). Each rendered star's
// glow hue maps from its resolved colour to the matching CSS var.
const STAR_FILL: Record<StarColor, string> = {
  red: 'var(--star-red)',
  blue: 'var(--star-blue)',
  purple: 'var(--star-purple)',
};
// Authored demo spec — exercises the rules by eye on :5199. NW red + NE blue +
// SE red is the contested-ordinal case: NE hydrates BOTH N and E (rule 4), so
// N and E render purple and all three ordinals vanish. S red is a plain lone
// cardinal for contrast.
const DEMO_COMPASS_STARS: CompassStar[] = [
  { dir: 'NW', color: 'red' },
  { dir: 'NE', color: 'blue' },
  { dir: 'SE', color: 'red' },
  { dir: 'S', color: 'red' },
];
function CompassDial({ cx, cy, capR, rimD, uiScale, stars }: { cx: number; cy: number; capR: number; rimD: string; uiScale: number; stars: readonly CompassStar[] }) {
  const R = capR * COMPASS_R_FRAC;
  const cardL = R * COMPASS_CARD_FRAC;
  const ordL = R * COMPASS_ORD_FRAC;
  const f = (n: number) => n.toFixed(1);
  const rad = (deg: number) => ((deg - 90) * Math.PI) / 180; // deg 0 = North (up), clockwise
  // Tick runs inward from the rim by `len`.
  const tick = (deg: number, len: number): string => {
    const a = rad(deg);
    const ox = cx + R * Math.cos(a), oy = cy + R * Math.sin(a);
    const ix = cx + (R - len) * Math.cos(a), iy = cy + (R - len) * Math.sin(a);
    return `M${f(ox)},${f(oy)} L${f(ix)},${f(iy)}`;
  };
  const cardinals = [0, 90, 180, 270].map((d) => tick(d, cardL)).join(' ');
  // Stars are the payload; the dial is just chrome. Size them to read as the
  // primary information — a broad colour halo, a fat same-colour core, and a
  // white-hot pinpoint so each gem reads bright rather than tinted.
  const haloR = Math.max(6.8, 12.0 * uiScale);
  const coreR = Math.max(3.8, 6.4 * uiScale);
  const hotR = Math.max(1.4, 2.4 * uiScale);
  // The dial only ever renders the REDUCED star set — resolveCompass mints the
  // branded ResolvedCompass, positions come from DIR_DEGREES.
  const resolved: ResolvedCompass = useMemo(() => resolveCompass(stars), [stars]);
  const renderStars = resolved.map((st) => {
    const a = rad(DIR_DEGREES[st.dir]);
    // Seat each star ON its interior tick — a touch inside the rim, centred on the
    // tick's midpoint (cardinals sit deeper since their tick is longer).
    const isCard = st.dir.length === 1;
    const rr = R - (isCard ? cardL : ordL) / 2;
    return { x: cx + rr * Math.cos(a), y: cy + rr * Math.sin(a), color: STAR_FILL[st.color] };
  });
  return (
    <g className="worker-compass" aria-hidden>
      <defs>
        <filter id="compass-star-glow" x="-400%" y="-400%" width="900%" height="900%">
          <feGaussianBlur stdDeviation={Math.max(1.8, 3.2 * uiScale)} />
        </filter>
      </defs>
      {/* West half of the rim — the endcap's east semicircle already draws the rest,
          so together they close the circle (cut on the NNW where the top line bites). */}
      <path className="worker-compass__rim" d={rimD} />
      <path className="worker-compass__tick worker-compass__tick--card" d={cardinals} />
      {/* glowing rim stars — a blurred colour halo under a crisp same-colour core. */}
      {renderStars.map((st, i) => (
        <g key={i}>
          <circle cx={st.x} cy={st.y} r={haloR} fill={st.color} filter="url(#compass-star-glow)" opacity={1} />
          <circle cx={st.x} cy={st.y} r={coreR} fill={st.color} />
          <circle cx={st.x} cy={st.y} r={hotR} fill="#fff" opacity={0.85} />
        </g>
      ))}
      {/* bespoke compass pointer over the ticks — two concentric red-north /
          white-south diamonds (a larger outline with a smaller one nested on
          top), spinning as one around the pivot; a single flat brass hub crowns
          the intersection and stays put as the pivot. */}
      <g>
        <CompassPointer cx={cx} cy={cy} size={R * COMPASS_POINTER_FRAC} uid="outer" />
        <CompassPointer cx={cx} cy={cy} size={R * COMPASS_POINTER_FRAC * COMPASS_POINTER_NEST} uid="inner" />
        {COMPASS_SPIN_SEC > 0 && (
          <animateTransform attributeName="transform" type="rotate"
            from={`0 ${cx} ${cy}`} to={`360 ${cx} ${cy}`}
            dur={`${COMPASS_SPIN_SEC}s`} repeatCount="indefinite" />
        )}
      </g>
      <circle className="worker-compass__hub" cx={cx} cy={cy} r={R * COMPASS_POINTER_FRAC * COMPASS_HUB_FRAC} fill="var(--instrument)" />
    </g>
  );
}

// ── Compass pointer element ─────────────────────────────────────────────────
// A bespoke compass diamond drawn in a fixed 100×100 space (viewBox), dropped as
// a nested <svg> centred on the dial pivot at any pixel size via cx/cy/size. Each
// half is a FULL diamond: the original triangle (long, outer N or S tip) mirrored
// across its waist into a shorter inverted copy pointing back at the centre. The
// two short inner tips touch at the centre. North diamond red, south diamond
// white, edges bowing INWARD toward the centre. Thin gold outlines with an
// interior glow supplying the colour, and a flat brass hub on the intersection.
const PTR_OUTER_FRAC = 0.62; // waist → outer (N/S) tip, fraction of half-extent (50)
const PTR_INNER_FRAC = 0.28; // waist → inner tip (the shorter, inverted copy); centre = this off 0
const PTR_EW_FRAC = 0.17;    // half-width at the waist, fraction of half-extent
const PTR_BOW = 0.3;         // how far each edge is pulled toward centre (0 = straight)
const PTR_STROKE = 1.0;      // thin traced outline, in the 100-box
const PTR_GLOW = 3.4;        // inner-glow blur radius, in the 100-box
const PTR_GLOW_W = 9;        // inner-glow band width — a fat stroke clipped to the interior
const PTR_GLOW_OP = 0.72;    // inner-glow opacity — layering keeps it bright, more inter-layer contrast
function CompassPointer({ cx, cy, size, uid, north = 'var(--error)', south = '#f4f1ea' }: { cx: number; cy: number; size: number; uid: string; north?: string; south?: string }) {
  const c = 50;                          // centre of the 100-box
  const outer = 50 * PTR_OUTER_FRAC, inner = 50 * PTR_INNER_FRAC, ew = 50 * PTR_EW_FRAC;
  // The inner (inverted) tip sits `inner` off the waist toward centre; the waist
  // itself is `inner` off centre, so the inner tip lands exactly on the centre —
  // red's and white's inner tips meet there.
  const f = (n: number) => n.toFixed(2);
  // A concave edge P→Q: a quadratic whose control point is the P·Q midpoint
  // pulled toward the centre by PTR_BOW, so the edge caves inward.
  const seg = (P: [number, number], Q: [number, number]): string => {
    const mx = (P[0] + Q[0]) / 2, my = (P[1] + Q[1]) / 2;
    const kx = mx + PTR_BOW * (c - mx), ky = my + PTR_BOW * (c - my);
    return `Q${f(kx)},${f(ky)} ${f(Q[0])},${f(Q[1])}`;
  };
  // A full diamond for `sign` = -1 (north/up) or +1 (south/down): outer tip,
  // E waist, inner tip (at centre), W waist — closed, concave edges.
  const diamond = (sign: number): string => {
    const waistY = c + sign * inner;
    const outerTip: [number, number] = [c, waistY + sign * outer];
    const eWaist: [number, number] = [c + ew, waistY];
    const innerTip: [number, number] = [c, c];
    const wWaist: [number, number] = [c - ew, waistY];
    return `M${f(outerTip[0])},${f(outerTip[1])} ${seg(outerTip, eWaist)} ${seg(eWaist, innerTip)} ${seg(innerTip, wWaist)} ${seg(wWaist, outerTip)} Z`;
  };
  const dN = diamond(-1), dS = diamond(1);
  const gid = `ptr-glow-${uid}`, cnid = `ptr-clip-n-${uid}`, csid = `ptr-clip-s-${uid}`;
  return (
    <svg x={cx - size / 2} y={cy - size / 2} width={size} height={size} viewBox="0 0 100 100" style={{ overflow: 'visible' }} aria-hidden>
      <defs>
        <filter id={gid} x="-60%" y="-60%" width="220%" height="220%">
          <feGaussianBlur stdDeviation={PTR_GLOW} />
        </filter>
        <clipPath id={cnid}><path d={dN} /></clipPath>
        <clipPath id={csid}><path d={dS} /></clipPath>
      </defs>
      {/* interior glow — a fat blurred colour stroke clipped to each diamond's
          interior, so it lights the borders and fades inward. This IS the colour
          of each diamond; the outlines are gold. */}
      <g clipPath={`url(#${cnid})`}>
        <path d={dN} fill="none" stroke={north} strokeWidth={PTR_GLOW_W} opacity={PTR_GLOW_OP} filter={`url(#${gid})`} />
      </g>
      <g clipPath={`url(#${csid})`}>
        <path d={dS} fill="none" stroke={south} strokeWidth={PTR_GLOW_W} opacity={PTR_GLOW_OP} filter={`url(#${gid})`} />
      </g>
      {/* thin gold outlines on both diamonds — same brass as the arc */}
      <path d={dN} fill="none" stroke="var(--instrument)" strokeWidth={PTR_STROKE} strokeLinejoin="round" strokeLinecap="round" />
      <path d={dS} fill="none" stroke="var(--instrument)" strokeWidth={PTR_STROKE} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

// ── Clock instrument (idle-worker-queue) ────────────────────────────────────
// The whimsical clock is a fork of the compass: same rim / tick / hub chrome, but
// the 8 rim stars become 6 Roman numerals on a regular hexagon, and the single
// spinning needle becomes an hour + minute hand pair. Numerals I..N render gold
// where N = the idle-worker-queue depth, the rest grey — so the FACE encodes the
// data (colour), while the hands are purely decorative (they do NOT tell real
// time). Roman numerals on a hexagon, 60° apart: I at top (0°), IV at bottom.
const CLOCK_NUMERALS = ['I', 'II', 'III', 'IV', 'V', 'VI'] as const;
const CLOCK_HEX_DEGREES = [0, 60, 120, 180, 240, 300]; // I top, IV bottom, cw
const CLOCK_NUM_FRAC = 0.72;       // numeral-ring radius as a fraction of the rim radius
const CLOCK_NUM_SIZE_FRAC = 0.3;   // numeral font-size as a fraction of the rim radius
const CLOCK_TICK_FRAC = 0.1;       // hexagon tick-nub length as a fraction of the rim radius
const CLOCK_MIN_FRAC = 1.6;        // minute (long) hand size as a fraction of the rim radius (100-box)
const CLOCK_HOUR_FRAC = 1.05;      // hour (short) hand size as a fraction of the rim radius
// Shared waist half-width for BOTH hands, as an absolute fraction of R — so width
// is decoupled from length and the longer minute hand renders the SAME pixel width
// as the hour hand (only longer). Set to the hour hand's current px half-width
// (50·PTR_EW_FRAC · CLOCK_HOUR_FRAC/100 ≈ 0.089·R) so the hour hand is unchanged.
const CLOCK_HAND_HALFWIDTH_FRAC = 0.089;
const CLOCK_HUB_FRAC = 0.06;       // brass hub radius as a fraction of the rim radius
const CLOCK_MIN_SEC = 60;          // minute hand — 60s per revolution
const CLOCK_HOUR_SEC = 300;        // hour hand — 300s per revolution

// Respect the OS reduced-motion preference — the clock hands (SMIL) are the only
// motion this section adds, so pause them when the user asks for stillness.
function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    if (typeof matchMedia === 'undefined') return;
    const mq = matchMedia('(prefers-reduced-motion: reduce)');
    const read = () => setReduced(mq.matches);
    read();
    mq.addEventListener('change', read);
    return () => mq.removeEventListener('change', read);
  }, []);
  return reduced;
}

// ── Clock hand ──────────────────────────────────────────────────────────────
// A single-sided fork of CompassPointer: only the outward (north) diamond, drawn
// gold instead of the compass's red/white pair — same 100-box, gold outline +
// interior glow. Dropped at the hub and spun by its own <animateTransform>; the
// hour + minute hands share this shape at different sizes / durations.
function ClockHand({ cx, cy, size, halfWidthPx, uid, durSec, animate }: {
  cx: number; cy: number; size: number; halfWidthPx: number; uid: string; durSec: number; animate: boolean;
}) {
  const c = 50;
  // Length (reach) still scales with `size`; width does NOT. The box maps `size` px
  // → 100 units, so a shared absolute px half-width becomes `halfWidthPx·100/size`
  // in box units — the longer hand ends up thinner, both hands the same pixel width.
  const outer = 50 * PTR_OUTER_FRAC, inner = 50 * PTR_INNER_FRAC, ew = halfWidthPx * 100 / size;
  const f = (n: number) => n.toFixed(2);
  const seg = (P: [number, number], Q: [number, number]): string => {
    const mx = (P[0] + Q[0]) / 2, my = (P[1] + Q[1]) / 2;
    const kx = mx + PTR_BOW * (c - mx), ky = my + PTR_BOW * (c - my);
    return `Q${f(kx)},${f(ky)} ${f(Q[0])},${f(Q[1])}`;
  };
  // The single outward diamond (sign = -1, pointing up out of the hub at centre).
  const waistY = c - inner;
  const outerTip: [number, number] = [c, waistY - outer];
  const eWaist: [number, number] = [c + ew, waistY];
  const innerTip: [number, number] = [c, c];
  const wWaist: [number, number] = [c - ew, waistY];
  const d = `M${f(outerTip[0])},${f(outerTip[1])} ${seg(outerTip, eWaist)} ${seg(eWaist, innerTip)} ${seg(innerTip, wWaist)} ${seg(wWaist, outerTip)} Z`;
  const gid = `clk-glow-${uid}`, cid = `clk-clip-${uid}`;
  return (
    <svg x={cx - size / 2} y={cy - size / 2} width={size} height={size} viewBox="0 0 100 100" style={{ overflow: 'visible' }} aria-hidden>
      <defs>
        <filter id={gid} x="-60%" y="-60%" width="220%" height="220%">
          <feGaussianBlur stdDeviation={PTR_GLOW} />
        </filter>
        <clipPath id={cid}><path d={d} /></clipPath>
      </defs>
      {/* the whole hand spins about the 100-box centre (= the hub) */}
      <g>
        <g clipPath={`url(#${cid})`}>
          <path d={d} fill="none" stroke="var(--brass-bright)" strokeWidth={PTR_GLOW_W} opacity={PTR_GLOW_OP} filter={`url(#${gid})`} />
        </g>
        <path d={d} fill="none" stroke="var(--instrument)" strokeWidth={PTR_STROKE} strokeLinejoin="round" strokeLinecap="round" />
        {animate && (
          <animateTransform attributeName="transform" type="rotate"
            from="0 50 50" to="360 50 50" dur={`${durSec}s`} repeatCount="indefinite" />
        )}
      </g>
    </svg>
  );
}

// ── Clock face ──────────────────────────────────────────────────────────────
// A fork of CompassDial: the rim + tick + hub scaffolding is kept, but the eight
// reduced compass stars give way to six upright Roman numerals on a hexagon and
// the spinning needle to an hour + minute hand pair. `queueValue` numerals light
// gold, the rest grey. `flip` counter-rotates the whole face 180° so it stays
// upright when the host crossbar assembly is flipped into a bottom rail.
function ClockDial({ cx, cy, capR, rimD, uiScale, queueValue, flip, animate }: {
  cx: number; cy: number; capR: number; rimD: string; uiScale: number;
  queueValue: number; flip: boolean; animate: boolean;
}) {
  void uiScale;
  const R = capR * COMPASS_R_FRAC;
  const tickL = R * CLOCK_TICK_FRAC;
  const f = (n: number) => n.toFixed(1);
  const rad = (deg: number) => ((deg - 90) * Math.PI) / 180; // deg 0 = up (I), clockwise
  // Tick nub running inward from the rim at each hexagon vertex.
  const tick = (deg: number): string => {
    const a = rad(deg);
    const ox = cx + R * Math.cos(a), oy = cy + R * Math.sin(a);
    const ix = cx + (R - tickL) * Math.cos(a), iy = cy + (R - tickL) * Math.sin(a);
    return `M${f(ox)},${f(oy)} L${f(ix)},${f(iy)}`;
  };
  const ticks = CLOCK_HEX_DEGREES.map(tick).join(' ');
  const numR = R * CLOCK_NUM_FRAC;
  const fontPx = Math.max(9, R * CLOCK_NUM_SIZE_FRAC);
  const numerals = CLOCK_HEX_DEGREES.map((deg, i) => {
    const a = rad(deg);
    return { x: cx + numR * Math.cos(a), y: cy + numR * Math.sin(a), label: CLOCK_NUMERALS[i], on: i < queueValue };
  });
  const hubR = Math.max(2, R * CLOCK_HUB_FRAC);
  // Shared absolute waist half-width (px) handed to both hands so they render the
  // same width regardless of length (see CLOCK_HAND_HALFWIDTH_FRAC).
  const halfWidthPx = R * CLOCK_HAND_HALFWIDTH_FRAC;
  return (
    <g className="worker-clock" aria-hidden>
      {/* Rim stays OUTSIDE the counter-rotation. rimD is the endcap circle's WEST
          half (the rail cap edge draws the EAST half); both live in the rail frame
          and are only CSS-flipped, so together they close the circle exactly like the
          compass. Counter-rotating it (as the numerals need) would swing this half
          onto the rail cap's side, leaving the interior boundary bare — the bug. */}
      <path className="worker-compass__rim" d={rimD} />
      {/* Everything the flip would leave UPSIDE-DOWN counter-rotates to stay upright.
          (Ticks are 180°-symmetric and the hands merely spin, but they ride the same
          group harmlessly — only the numeral glyphs strictly require it.) */}
      <g transform={flip ? `rotate(180 ${f(cx)} ${f(cy)})` : undefined}>
        <path className="worker-compass__tick worker-compass__tick--card" d={ticks} />
        {/* Roman numerals — gold up to the queue depth, grey beyond. Upright. */}
        {numerals.map((n, i) => (
          <text key={i} className="worker-clock__numeral"
            x={f(n.x)} y={f(n.y)} fontSize={fontPx.toFixed(1)}
            textAnchor="middle" dominantBaseline="central"
            fill={n.on ? 'var(--brass-bright)' : 'var(--faint)'}>{n.label}</text>
        ))}
        {/* hour (short) under minute (long) — both spin about the shared hub */}
        <ClockHand cx={cx} cy={cy} size={R * CLOCK_HOUR_FRAC} halfWidthPx={halfWidthPx} uid="hour" durSec={CLOCK_HOUR_SEC} animate={animate} />
        <ClockHand cx={cx} cy={cy} size={R * CLOCK_MIN_FRAC} halfWidthPx={halfWidthPx} uid="min" durSec={CLOCK_MIN_SEC} animate={animate} />
        <circle className="worker-compass__hub" cx={f(cx)} cy={f(cy)} r={hubR.toFixed(1)} />
      </g>
    </g>
  );
}

function WorkerQueues({ count, uiScale, gap, pitch, inset, split, variant = 'compass', queueValue = 0, flip = false, animate = true }: {
  count: number; uiScale: number; gap: number; pitch: number; inset: number; split: number;
  variant?: 'compass' | 'clock'; queueValue?: number; flip?: boolean; animate?: boolean;
}) {
  // Crossbar + hourglass shape is LOCKED — read straight from the frozen constants.
  // The by-eye dev-tuning sliders and the `shape` prop have been retired.
  const shape = RAIL_SHAPE_DEFAULTS;
  // Self-measure W (like ArcLayer) — the layer is inset:0 full-viewport, so the
  // floor envelope + T-rail geometry are read straight off the real width.
  const wrapRef = useRef<HTMLDivElement>(null);
  const [W, setW] = useState(1000);
  useEffect(() => {
    const el = wrapRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (r && r.width) setW(Math.floor(r.width));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // The SHARED lemon geometry — same helper ArcLayer renders from, so the chips ride
  // the exact underside the lemon draws. The T-rail + both columns all read this.
  const geo = useMemo(() => lemonGeometry(W, uiScale), [W, uiScale]);

  // The ⊥/T rail: a gold crossbar (the table's top edge) that FLOWS UNDER the left dial
  // row — one margin below the line the left dials ride — then MIRRORS about the lemon's
  // true centre (geo.xCtr) to form the right half. Because the right dials ride the arc's
  // asymmetric underside, the mirrored bar pulls AWAY from them — expected. The bar reaches
  // outward only as far as it can without bumping the drainage ditches (the vertical drain
  // columns), stopping a chip-clear inside each. Its bow is EXAGGERATED (BAR_EXAG) about the
  // outer ends so the curve reads clearly. At the centre, an HOURGLASS motif straddles the
  // stem: two concave walls pinch to a waist on the crossbar and flare up to NEST the first
  // worker chip on each side, split by the central dividing line (the stem). (Below chips.)
  const rail = useMemo(() => {
    if (!geo.hasSpan) return null;
    const s = uiScale;
    const f = (n: number) => n.toFixed(1);
    const chipR = (WORKER_CHIP_PX * s) / 2;
    const ditchClear = chipR + WORKER_BAR_MARGIN * s;
    const xCtr = geo.xCtr;
    // Raw bar height at x — parallels the left dials, one margin below their bottom edge.
    const barYraw = (x: number): number => workerFloorY(x, geo, s) + W_DROP_PX * s + chipR + WORKER_BAR_MARGIN * s;
    // Ditches = the drain columns (= each makeQueuePath xEnd). Stop a chip-clear inside.
    const xDitchL = W_INSET_PX * s;
    const xDitchR = geo.Rx - W_INSET_PX * s;
    const xBarL = xDitchL + ditchClear;
    // Exaggerate the bow about the outer end (deepest at centre) so the curve reads.
    const BAR_EXAG = shape.barExag;
    const yEnd = barYraw(xBarL);
    const baseBarY = (x: number): number => yEnd + (barYraw(x) - yEnd) * BAR_EXAG;

    // Measure the REAL first-worker point off the left queue path so the hourglass nests
    // it accurately (and so the centre lift knows where the feet plant).
    const lpath = makeQueuePath(-1, geo, s, gap, inset);
    const w0 = lpath.pointAt(split * s); // left first-worker centre
    const dx0 = xCtr - w0.x; // its x-offset from centre
    const cy0 = w0.y; // worker-row height (mirror-equal on the right)
    const nestClear = shape.nestClear * s; // gap between the bow's belly and the worker rim
    // Tip x — where each wall plants its foot on the lemon (top) and crossbar (bottom),
    // pulled IN from the worker centre toward the middle so the hourglass mouth narrows.
    const HG_TIP_FRAC = shape.hgTipFrac;
    const tipX = dx0 * HG_TIP_FRAC;
    const bellyOff = dx0 - chipR - nestClear; // belly x-offset — nests the worker rim

    // Our OWN crossbar arc — no longer the left lemon underside mirrored across centre.
    // One symmetric curve of x: deepest under the centre (clearing the lemon apex), its
    // CENTRE pulled up into the divider peak that flows OUT across the span (not a local
    // bump), and its ENDS rounding back up to the outer clearance with a horizontal tangent
    // at the terminus — so the T's extremities round off instead of ending on a corner.
    const half = xCtr - xBarL; // left reaches a chip-clear inside its ditch; the arc mirrors
    const yDeep = baseBarY(xCtr); // deepest clearance, under the lemon apex
    const yEndClear = baseBarY(xBarL); // clearance at the outer end
    const HG_LIFT = shape.hgLift * s; // centre pull-up — the divider peak
    const HG_LIFT_SPAN = shape.hgLiftSpan; // how far out the lift flows (fraction of the half-span)
    // Auto rise anchors the ends to the outer clearance; HG_END_RISE_MULT scales it so
    // the ends round up more/less/none by eye without losing that clearance anchor.
    const HG_END_RISE = (yDeep - yEndClear) * shape.endRiseMult; // ends rise back up to the outer clearance
    const HG_END_SPAN = shape.hgEndSpan; // outer fraction over which the ends round up
    const crossY = (x: number): number => {
      const u = Math.min(1, Math.abs(x - xCtr) / half); // 0 centre → 1 end
      const liftT = Math.min(1, u / HG_LIFT_SPAN);
      const centreLift = HG_LIFT * 0.5 * (1 + Math.cos(Math.PI * liftT)); // peak centre → 0
      const et = Math.min(1, Math.max(0, (u - (1 - HG_END_SPAN)) / HG_END_SPAN));
      const endRise = HG_END_RISE * 0.5 * (1 - Math.cos(Math.PI * et)); // 0 → rounded top
      return yDeep - centreLift - endRise;
    };

    // The right end mirrors to xCtr + half, clamped a chip-clear inside the right ditch
    // (it usually falls short — the accepted pull-away from the right). N samples span the
    // whole arc for lobe detection; the visible bottom line `barD` is trimmed to the lobe
    // cap-starts (built AFTER the loop) so the bottom never pokes out past a cap.
    const xBarR = Math.min(xCtr + half, xDitchR - ditchClear);
    const N = 96;

    // ── Table-edge BAND top edge — hugs just under the worker dials, so the crossbar
    // reads as a solid lip (top follows the dials, bottom `crossY` stays symmetric).
    // chipBottomY = the underside of the chip row at x (floor envelope + drop + radius);
    // topFollow blends the true (asymmetric) dial hug against its own left/right AVERAGE
    // (a symmetric top) so the operator can dial how hard the top tracks the dials.
    const chipBottomY = (x: number): number => workerFloorY(x, geo, s) + gap * s + chipR;
    const topHugPx = shape.topHug * s;
    const topFollow = Math.min(1, Math.max(0, shape.topFollow));
    const topEdgeY = (x: number): number => {
      const d = x - xCtr;
      const follow = chipBottomY(x);
      const sym = 0.5 * (chipBottomY(xCtr + d) + chipBottomY(xCtr - d));
      return sym + (follow - sym) * topFollow + topHugPx;
    };
    // The band exists only where the top edge sits ABOVE the bottom bar (positive
    // thickness). The centre lift and the rounded ends raise the bottom bar past the
    // top edge (the hourglass notch; the terminals) — those stretches are CLIPPED so
    // the top line never crosses below the bar. Each surviving stretch is one closed
    // lobe: top L→R, a straight END CAP down to the bar, the bar R→L, a cap back up.
    // Lobes are cut where the gap thins to CAP_MIN, leaving a small flat cap rather
    // than a sharp sliver at each terminus.
    const CAP_MIN = 4 * s; // band thinner than this is clipped; also the cap height there
    const gapAt = (x: number): number => crossY(x) - topEdgeY(x); // >0 ⇒ top above bar
    const step = (xBarR - xBarL) / N;
    // Bisect for the x where gapAt(x) === CAP_MIN between a bracketing pair (one side
    // above CAP_MIN, the other below) so lobe ends land on a clean constant thickness.
    const edgeX = (xa: number, xb: number): number => {
      let lo = xa, hi = xb;
      for (let k = 0; k < 22; k++) {
        const m = (lo + hi) / 2;
        if ((gapAt(m) >= CAP_MIN) === (gapAt(lo) >= CAP_MIN)) lo = m; else hi = m;
      }
      return (lo + hi) / 2;
    };
    const runs: Array<{ xa: number; xb: number }> = [];
    let prevX = xBarL, prevOn = gapAt(xBarL) >= CAP_MIN;
    let startX: number | null = prevOn ? xBarL : null;
    for (let i = 1; i <= N; i++) {
      const x = xBarL + step * i;
      const on = gapAt(x) >= CAP_MIN;
      if (on && !prevOn) startX = edgeX(prevX, x);
      if (!on && prevOn && startX !== null) { runs.push({ xa: startX, xb: edgeX(prevX, x) }); startX = null; }
      prevX = x; prevOn = on;
    }
    if (prevOn && startX !== null) runs.push({ xa: startX, xb: xBarR });

    // Solve gapAt(x) === target between a straddling pair (p, q) — the generalised
    // sibling of edgeX. Used to run the inner end to the TRUE crossing (target 0).
    const solveGap = (p: number, q: number, target: number): number => {
      let lo = p, hi = q;
      const loBelow = gapAt(lo) - target < 0;
      for (let k = 0; k < 30; k++) {
        const m = (lo + hi) / 2;
        if ((gapAt(m) - target < 0) === loBelow) lo = m; else hi = m;
      }
      return (lo + hi) / 2;
    };
    // Each lobe gets TWO distinct end treatments: the OUTER end (toward the ditch)
    // rounds off with an elliptical-arc cap; the INNER end (toward the hourglass)
    // runs to the true top∩bottom crossing and blends the junction with a cubic that
    // follows each edge's local slope — a rounded taper pointing at the hourglass,
    // not a 90° riser. The full-width `barD` draws the bottom bar, so the per-lobe
    // stroke covers only the top edge + inner taper + outer cap (no double line).
    const bandSegs: string[] = [];
    const edgeSegs: string[] = [];
    const capStarts: number[] = []; // each lobe's outer terminus — where the cap begins
    // The RHS cap bulge (right lobe's rounded outer end) hosts the compass dial: a
    // semicircle of radius r whose centre of curvature is (xOuter, midY). Captured in
    // the loop below and handed out so a compass can be inscribed centred in it.
    let compass: { cx: number; cy: number; capR: number; rimD: string } | null = null;
    for (const { xa, xb } of runs) {
      const innerIsB = Math.abs(xb - xCtr) < Math.abs(xa - xCtr);
      let xOuter = innerIsB ? xa : xb;
      const xInner0 = innerIsB ? xb : xa;
      const dirIn = Math.sign(xInner0 - xOuter) || 1; // outer → inner along x
      // Inner end → the true crossing where the top edge meets the bottom bar.
      const xInner = solveGap(xInner0, xCtr, 0);
      // Pull the outer terminus IN from the ditch by the cap-inset knob — this is
      // where the crossbar lines stop and the cap begins (the cap's roundness still
      // derives from the gap at whatever point it lands). Only the RIGHT lobe insets
      // (the left already sits a chip-clear inside its ditch). Clamped to keep a lobe.
      const insetHere = xOuter > xCtr ? shape.capInset : 0;
      const insetPx = Math.max(0, Math.min(insetHere * s, Math.abs(xInner - xOuter) - 6 * s));
      xOuter += dirIn * insetPx;
      capStarts.push(xOuter);
      // Back the nose off that tip so the merge is a rounded taper, not a cusp.
      const tb = Math.min(8 * s, Math.abs(xInner - xOuter) * 0.25);
      const xTip = xInner - dirIn * tb;
      // Inner merge cubic — controls extend along each edge's tangent toward the
      // crossing, so top → bottom is curvature-smooth (a nose bulging at the centre).
      const ds = Math.max(0.5, tb * 0.5);
      const tanTx = dirIn * ds, tanTy = topEdgeY(xTip + tanTx) - topEdgeY(xTip);
      const tanBx = dirIn * ds, tanBy = crossY(xTip + tanBx) - crossY(xTip);
      const lT = Math.hypot(tanTx, tanTy) || 1;
      const lB = Math.hypot(tanBx, tanBy) || 1;
      const c1x = xTip + tb * tanTx / lT, c1y = topEdgeY(xTip) + tb * tanTy / lT;
      const c2x = xTip + tb * tanBx / lB, c2y = crossY(xTip) + tb * tanBy / lB;
      const cubic = `C${f(c1x)},${f(c1y)} ${f(c2x)},${f(c2y)} ${f(xTip)},${f(crossY(xTip))}`;
      // Outer end → elliptical-arc cap, radius = half the residual gap, bulging AWAY
      // from centre (sweep flipped per side). A CAP_MIN gap gives it a radius to work with.
      const r = Math.max(0.5, (crossY(xOuter) - topEdgeY(xOuter)) / 2);
      // Right lobe → its cap is the RHS bulge. Centre of the cap semicircle is
      // (xOuter, midY); midY = crossY − r = ½(top+bottom). Rightmost run wins.
      if (xOuter > xCtr && (!compass || xOuter > compass.cx)) {
        // The cap's east semicircle IS the compass rim's east half. Complete the
        // circle by tracing the WEST half (bottom → west → north), stopping if the
        // arc climbs back above the crossbar top line — the accepted NNW cutoff, so
        // the rim reads as one big endcap circle rather than a separate inscribed dial.
        const Cx = xOuter, Cy = crossY(xOuter) - r;
        const rimPts: string[] = [];
        for (let a = 180; a <= 360; a += 2) {
          const rad = (a * Math.PI) / 180;
          const px = Cx + r * Math.sin(rad);
          const py = Cy - r * Math.cos(rad);
          if (a > 182 && py < topEdgeY(px) - 0.5) break; // climbed past the top line → cut here
          rimPts.push(`${f(px)},${f(py)}`);
        }
        compass = { cx: Cx, cy: Cy, capR: r, rimD: `M${rimPts.join(' L')}` };
      }
      const sweep = xOuter < xCtr ? 1 : 0; // bottom→top, bulge outward
      const outBotY = crossY(xOuter);
      // Top-edge polyline (outer → tip) and bottom-bar polyline (tip → outer).
      const nSeg = Math.max(2, Math.round(Math.abs(xTip - xOuter) / step));
      const topC: string[] = [], botC: string[] = [];
      for (let i = 0; i <= nSeg; i++) {
        const t = i / nSeg;
        const xt = xOuter + (xTip - xOuter) * t;
        topC.push(`${f(xt)},${f(topEdgeY(xt))}`);
        const xb2 = xTip + (xOuter - xTip) * t;
        botC.push(`${f(xb2)},${f(crossY(xb2))}`);
      }
      const arc = `A${f(r)},${f(r)} 0 0 ${sweep}`;
      // Fill: top(outer→tip) → inner nose → bottom(tip→outer) → outer cap arc → Z.
      bandSegs.push(
        `M${topC[0]} ${topC.slice(1).map((c) => `L${c}`).join(' ')} ${cubic} ` +
        `${botC.slice(1).map((c) => `L${c}`).join(' ')} ${arc} ${topC[0]} Z`,
      );
      // Stroke: outer cap arc → top edge → inner nose (bottom bar stays with barD).
      edgeSegs.push(
        `M${f(xOuter)},${f(outBotY)} ${arc} ${topC[0]} ` +
        `${topC.slice(1).map((c) => `L${c}`).join(' ')} ${cubic}`,
      );
    }
    const bandD = bandSegs.join(' ');
    const edgeD = edgeSegs.join(' ');
    // The visible bottom line runs ONLY between the outermost cap-starts (the caps
    // close each end), so it never pokes out past a cap. "Go until touching the cap"
    // is symmetric — the left/right asymmetry lives entirely in the per-side inset.
    const barLo = capStarts.length ? Math.min(...capStarts) : xBarL;
    const barHi = capStarts.length ? Math.max(...capStarts) : xBarR;
    const barPts: string[] = [];
    for (let i = 0; i <= N; i++) {
      const x = barLo + (barHi - barLo) * (i / N);
      barPts.push(`${i === 0 ? 'M' : 'L'}${f(x)},${f(crossY(x))}`);
    }
    const barD = barPts.join(' ');

    // Hourglass centre — the central cell is bounded by the LEMON BOTTOM (top) and the
    // CROSSBAR (bottom); the centre stem splits it. Each outer wall runs lemon→crossbar
    // as a smooth CUBIC (less triangular than a pinched quad — a capital-I stroke), bowed
    // inward so its belly nests the innermost slot-0 worker rim, planting a small serif
    // FOOT on the lemon and crossbar. The cubic controls sit at cbxOff so B(0.5).x lands
    // on the belly regardless of how far the tips are pulled in.
    const HG_FOOT = shape.hgFoot * s; // serif half-width — the little feet of the "I"
    const HG_CF = shape.hgCf; // control-point height fraction → near-vertical wall ends
    const wallGeom = (sign: number) => {
      const xw = xCtr + sign * tipX;
      const yTop = geo.wy(xw);
      const yBot = crossY(xw);
      const h = yBot - yTop;
      const cbx = xCtr + (sign * (4 * bellyOff - tipX)) / 3; // cubic control x → belly at mid
      return { xw, yTop, yBot, c1x: cbx, c1y: yTop + h * HG_CF, c2x: cbx, c2y: yBot - h * HG_CF };
    };
    const sideWall = (sign: number): string => {
      const g = wallGeom(sign);
      // Foot ONLY where the wall plants on the CROSSBAR (bottom) — the operator wants the
      // serif on the crossbar tip, not up at the lemon/hourglass tip. So no top serif; the
      // wall meets the lemon underside cleanly and only the crossbar end gets its foot.
      return (
        `M${f(g.xw)},${f(g.yTop)} C${f(g.c1x)},${f(g.c1y)} ${f(g.c2x)},${f(g.c2y)} ${f(g.xw)},${f(g.yBot)}` + // I-stroke
        ` M${f(g.xw - HG_FOOT)},${f(g.yBot)} L${f(g.xw + HG_FOOT)},${f(g.yBot)}` // crossbar foot
      );
    };
    const hourD = `${sideWall(-1)} ${sideWall(1)}`;

    // Hourglass SEGMENTS — one lit cell per side (lemon top, wall outer, crossbar bottom,
    // stem inner via Z). Same Segment type as the lemon persona sections → shared gold
    // interior glow, the load-bearing reservist indicator.
    const M = 20;
    const hourRegion = (sign: number): string => {
      const g = wallGeom(sign);
      const seg: string[] = [];
      for (let i = 0; i <= M; i++) { const x = xCtr + (g.xw - xCtr) * (i / M); seg.push(`${i === 0 ? 'M' : 'L'}${f(x)},${f(geo.wy(x))}`); }
      seg.push(`C${f(g.c1x)},${f(g.c1y)} ${f(g.c2x)},${f(g.c2y)} ${f(g.xw)},${f(g.yBot)}`); // down the outer wall
      for (let i = 1; i <= M; i++) { const x = g.xw + (xCtr - g.xw) * (i / M); seg.push(`L${f(x)},${f(crossY(x))}`); } // back along the crossbar
      seg.push('Z'); // up the centre stem
      return seg.join(' ');
    };
    const sections: Segment[] = [-1, 1].map((sign) => ({
      region: hourRegion(sign),
      tone: 'var(--brass-bright)', // gold reservist glow
      glow: true, // reservist indicator — flip off when the side has no reservists
      cx: xCtr + sign * tipX * 0.55,
      cy: cy0,
      gr: tipX,
    }));

    return {
      barD,
      edgeD,
      bandD,
      hourD,
      sections,
      compass,
      // Central dividing line — the lemon's centre divider down to the (lifted) crossbar.
      stemD: `M${f(xCtr)},${f(geo.wy(xCtr))} L${f(xCtr)},${f(crossY(xCtr))}`,
    };
  }, [geo, uiScale, gap, inset, split, shape]);

  // Split the depth half/half — left queue takes the odd chip.
  const leftCount = Math.ceil(count / 2);
  const rightCount = Math.floor(count / 2);

  return (
    <div className={`worker-queues${flip ? ' worker-queues--flip' : ''}`} ref={wrapRef}
      aria-label={variant === 'clock' ? `Idle worker queue · ${count}` : `Worker queue · ${count}`}>
      {/* ⊥ rail — flat crossbar + centre stem, stroked like the lemon dividers. Below
          the chips (drawn first); pointer-events:none so clicks fall through. */}
      {rail && (
        <svg className="worker-rail" width={W} height="100%" aria-hidden>
          {/* Table-lip fill — the ribbon interior between the dial-hugging top edge and
              the symmetric bottom bar. Drawn first (behind glow + strokes). */}
          <path className="worker-rail__band" d={rail.bandD} style={{ fillOpacity: shape.bandFill }} />
          {/* reservist glow — behind the gold lines so the strokes read on top. Same
              Segment renderer the lemon persona sections use. */}
          <SegmentGlowLayer segments={rail.sections} idPrefix="hour" blur={9 * uiScale} rimW={11 * uiScale} />
          <path className="worker-rail__line" d={rail.stemD} />
          <path className="worker-rail__line" d={rail.barD} />
          <path className="worker-rail__line" d={rail.edgeD} />
          <path className="worker-rail__line" d={rail.hourD} />
          {/* Instrument inscribed in the RHS cap bulge — on top of the rail lines.
              The compass (mid-page status read) or the clock (idle-worker-queue). */}
          {rail.compass && (
            variant === 'clock'
              ? <ClockDial cx={rail.compass.cx} cy={rail.compass.cy} capR={rail.compass.capR} rimD={rail.compass.rimD} uiScale={uiScale} queueValue={queueValue} flip={flip} animate={animate} />
              : <CompassDial cx={rail.compass.cx} cy={rail.compass.cy} capR={rail.compass.capR} rimD={rail.compass.rimD} uiScale={uiScale} stars={DEMO_COMPASS_STARS} />
          )}
        </svg>
      )}
      <WorkerColumn side={-1} count={leftCount} geo={geo} uiScale={uiScale} gap={gap} pitch={pitch} inset={inset} split={split} />
      <WorkerColumn side={1} count={rightCount} geo={geo} uiScale={uiScale} gap={gap} pitch={pitch} inset={inset} split={split} />
    </div>
  );
}

// ── Idle worker queue — the flipped bottom rail hosting the clock ────────────
// A standalone bottom section: the SAME crossbar assembly (rail + hourglass + cap
// + chips) reused from WorkerQueues, but flipped 180° so it reads as a bottom rail
// (cap swings LEFT, chips hang BELOW the bar), with the clock inscribed in the cap
// instead of the compass. The clock face counter-rotates to stay upright. Chips
// The clock face (numeral fill) is DECOUPLED from the chip count: `clockValue`
// (0–6, its own placeholder demo source) drives the numerals, `idleCount` drives
// the chips (unbounded — grows up the ditches and is meant to clobber above).
function IdleWorkerQueue({ clockValue, idleCount, uiScale, animate }: {
  clockValue: number; idleCount: number; uiScale: number; animate: boolean;
}) {
  return (
    <section className="idle-worker-queue"
      aria-label={`Idle worker queue — ${idleCount} idle worker${idleCount === 1 ? '' : 's'}`}>
      <WorkerQueues count={idleCount} uiScale={uiScale}
        gap={W_DROP_PX} pitch={W_SPACE_PX} inset={W_INSET_PX} split={W_SPLIT_PX}
        variant="clock" queueValue={clockValue} flip animate={animate} />
    </section>
  );
}

// The timer graph is locked to a bounded rectangle in the top-left — its right
// edge at the (retired) vertical guide, its bottom edge at the horizontal guide.
// These were the operator's dialed-in guide values; the guide rulers themselves
// are gone. As CSS lengths on the .timerfield box (% of viewport).
const GRAPH_H_VH = 27; // graph-box height in vh — the timer band's floor

// The timer band is part of the composition, not standalone data: on mobile it
// tracks the cluster via a single `graphScale` (= uiScale, optionally softened).
// GRAPH_SHRINK is the ONE dial for how hard the band follows the cluster:
//   graphScale = 1 − (1 − uiScale)·GRAPH_SHRINK
//   0 = band stays desktop-locked (27vh, old behaviour); 1 = full uiScale.
// At uiScale === 1 (desktop) graphScale === 1 for any GRAPH_SHRINK ⇒ unchanged.
// Start at full uiScale; drop toward 0 by eye on the phone if the midline
// overshoots and reads too high.
const GRAPH_SHRINK = 1;

// The graph's RIGHT true-border is the seam where the break-even MIDLINE (the
// horizon, y=0) meets the dial rim — that intersection IS the end of the graph, so
// the data line and the horizon both terminate on the SAME x at every viewport
// size. A fixed % (was 77%) let the data float free of the dial at fullscreen: the
// hub is a fixed HUB_R disc pinned to the top-right corner (centre (vw, −Y_SHIFT)),
// so only a dial-anchored border keeps the timer end tucked under it. The horizon
// sits at horizonY = (Y_MAX/(Y_MAX−Y_MIN))·plotH, plotH = the 27vh box height
// (width-independent), and the rim's LEFT intersection at that y is
// vw − √(HUB_R² − (horizonY+Y_SHIFT)²). Falls back to the old 77% when the horizon
// rides clear of the disc (very tall viewport → no intersection).
function graphRightBorderPx(vw: number, vh: number, scale: number): number {
  const plotH = ((GRAPH_H_VH * scale) / 100) * vh; // scaled band — matches --graph-h publish
  const horizonY = (Y_MAX / (Y_MAX - Y_MIN)) * plotH;
  // The border tracks where the SCALED hub rim crosses the scaled horizon. With
  // graphScale === uiScale, horizonY, Y_SHIFT and HUB_R share one factor, so
  // half = scale²·(desktop half) stays positive and the graph keeps tucking
  // under the rim on mobile instead of hitting the vw·0.77 fallback.
  const rimDy = horizonY + Y_SHIFT * scale;
  const half = HUB_R * scale * (HUB_R * scale) - rimDy * rimDy;
  return half > 0 ? vw - Math.sqrt(half) : vw * 0.77;
}

// ── THE ARC, FROZEN ─────────────────────────────────────────────────────────
// The connecting-arc shape is settled — no longer a tuning knob. These are the
// operator's dialed-in values (amplitude bumped 85 → 93 to lock the crest);
// the debug sliders + localStorage persistence that used to drive them are gone.
// lemonGeometry reads these directly (ArcLayer + the worker floor both source from
// it), and TimerField's bgBottomPx couples off leftYFrac.
const ARC = { amplitude: 93, maximaFrac: 0.68, rightYFrac: 1.0, leftYFrac: 1.57 } as const;

// ── THE LEMON, LOCKED ───────────────────────────────────────────────────────
// The lens shape is settled too — the old "Lemon width" / "Lemon depth" demo
// sliders are retired and their values frozen here at the operator's dialed-in
// numbers (px @1440; ArcLayer multiplies by uiScale). WIDTH_INSET is pulled off
// each side of the full span (0 = full width, larger = narrower); DEPTH is the
// bottom bulge below the arc crest. The upcoming worker-queue floor-follow reads
// these too, so they live at module scope as the single source.
const LEMON_WIDTH_INSET = 144;
const LEMON_DEPTH = 108;

// A useState that survives a page refresh by mirroring to localStorage. Debug-
// only convenience for the shape-finding knobs — the dialed-in values reload
// instead of snapping back to defaults. Removed with the rest of the debug infra
// when the layout is frozen. SSR/blocked-storage safe (falls back to `initial`).
function usePersistedNumber(key: string, initial: number): [number, (v: number) => void] {
  const [v, setV] = useState<number>(() => {
    try {
      const raw = localStorage.getItem(`ops-mock:${key}`);
      const n = raw == null ? NaN : Number(raw);
      return Number.isFinite(n) ? n : initial;
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(`ops-mock:${key}`, String(v));
    } catch {
      /* storage unavailable — in-memory only */
    }
  }, [key, v]);
  return [v, setV];
}

// The JSON sibling of usePersistedNumber: mirrors an arbitrary serialisable value
// to localStorage so it survives a refresh. Same SSR/blocked-storage safety —
// any read/parse failure falls back to `initial`, any write failure is in-memory
// only. Backs the placement captures (`placedDials`), which must persist so a
// hand-authored layout survives a reload.
function usePersistedJSON<T>(key: string, initial: T): [T, React.Dispatch<React.SetStateAction<T>>] {
  const [v, setV] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(`ops-mock:${key}`);
      return raw == null ? initial : (JSON.parse(raw) as T);
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(`ops-mock:${key}`, JSON.stringify(v));
    } catch {
      /* storage unavailable — in-memory only */
    }
  }, [key, v]);
  return [v, setV];
}

// ═══════════════════════════════════════════════════════════════════════════
// Coordinate-capture placement layer — an AUTHORING tool, not a live display.
//
// A full-bleed overlay that, WHEN ACTIVE, takes the pointer (crosshair) and drops
// a dial — sized like a TTS-queue dial — wherever the operator clicks, recording
// each (x, y). When inactive it's pointer-events:none, so the normal cockpit reads
// and interacts straight through it; the captured dials still RENDER either way
// (they're persisted), so a hand-authored layout stays visible across a toggle.
//
// COORD FRAME: viewport-top px read from this layer's getBoundingClientRect — the
// SAME frame the fixed arc/worker overlays render in at scroll-top (the layer is
// position:fixed inset:0, so its rect origin IS the viewport top-left). Each
// placed[i] is drawn CENTRED on its (x, y), labelled with its 1-based placement
// order; a faint dashed connector threads the drops in that order so the capture
// reads as a queue/path. Drops stay individually addressable (Undo pops the last).
// ═══════════════════════════════════════════════════════════════════════════
const PLACE_DIAL_PX = TTS_DIAL_PX; // placement dials match the TTS queue dial (36px → r18)

function PlaceLayer({
  active,
  placed,
  onDrop,
}: {
  active: boolean;
  placed: { x: number; y: number }[];
  onDrop: (x: number, y: number) => void;
}) {
  // Read the cursor in the layer's own frame (== viewport px, since it's fixed at
  // inset:0) and push the drop. Only wired while active; inert layer never fires.
  function onClick(e: React.MouseEvent<HTMLDivElement>) {
    const rect = e.currentTarget.getBoundingClientRect();
    onDrop(e.clientX - rect.left, e.clientY - rect.top);
  }
  const connector = placed
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.y.toFixed(1)}`)
    .join(' ');
  return (
    <div
      className={`place-layer${active ? ' place-layer--active' : ''}`}
      onClick={active ? onClick : undefined}
      aria-hidden={active ? undefined : true}
      aria-label={active ? 'Placement capture layer — click to drop a dial' : undefined}
    >
      {/* faint connector threading the drops in placement order — cosmetic, so the
          captured sequence reads as a queue/path. Drawn only once there are ≥2. */}
      {placed.length > 1 ? (
        <svg className="place-layer__svg" width="100%" height="100%">
          <path className="place-connector" d={connector} />
        </svg>
      ) : null}
      {placed.map((p, i) => (
        <div
          key={i}
          className="place-dial"
          style={{ left: p.x, top: p.y, width: PLACE_DIAL_PX, height: PLACE_DIAL_PX }}
        >
          <span className="place-dial__idx">{i + 1}</span>
        </div>
      ))}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
export function MockOpsCockpit() {
  const [dialCount, setDialCount] = usePersistedNumber('dialCount', initialDialCount);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [focusedDial, setFocusedDial] = useState<string | null>(null);
  const [fracTop, setFracTop] = usePersistedNumber('fracTop', 4); // big-dial numerator (focus)
  const [fracBot, setFracBot] = usePersistedNumber('fracBot', 1); // big-dial denominator (distraction)
  const [breakMin, setBreakMin] = usePersistedNumber('breakMin', 0); // break-timer elapsed minutes
  // TTS-queue depth — mirrors "State-dial density": drives how many of the left
  // stack's queue dials render, so the study can show the queue growing/shrinking.
  const [ttsDepth, setTtsDepth] = usePersistedNumber('ttsDepth', initialTtsDepth);
  // Worker-queue depth — total chips across the two "M" stacks below the lemon (the
  // 6 persona icons in the lemon are a fixed roster, not a knob). The old lemon
  // width/depth knobs are retired: the lemon is locked (see LEMON_WIDTH_INSET / DEPTH).
  const [workerCount, setWorkerCount] = usePersistedNumber('workerCount', 8);
  // Idle-worker-queue — TWO decoupled demo sources now that the clock is only a
  // placeholder test fixture: `clockValue` (0–6) drives the clock numeral fill, and
  // `idleCount` (0–MAX_IDLE_COUNT) drives the chips that climb up the edge ditches.
  const [clockValue, setClockValue] = usePersistedNumber('clockValue', 4);
  const [idleCount, setIdleCount] = usePersistedNumber('idleCount', 4);
  const reducedMotion = usePrefersReducedMotion();
  // Worker-queue positioning is locked (W_DROP_PX / W_SPACE_PX / W_INSET_PX / W_SPLIT_PX)
  // and fed straight to WorkerQueues — the by-eye tuning sliders are retired.
  // Instrument-line colour — hue locked to brass; saturation + HSL lightness settled
  // (the tuning sliders are retired). Frozen at the dialed-in values; fed to .page as
  // --instrument so the rim, arc, and horizon all follow.
  const [instSat] = usePersistedNumber('instSat', 68);
  const [instLum] = usePersistedNumber('instLum', 30);
  // Generic screen-size resilience: the scale floor is a live demo knob so the
  // phone case can be tuned by eye (persisted like the other knobs). uiScale itself
  // is derived below from vp.w once the viewport is measured.
  const [scaleMin, setScaleMin] = usePersistedNumber('scaleMin', SCALE_MIN);
  // Demo bar collapses to a toggle under the narrow breakpoint (mockup chrome only).
  const [demoOpen, setDemoOpen] = useState(false);

  // ── Coordinate-capture placement system (authoring tool) ────────────────────
  // Place mode is an ephemeral toggle (not persisted — a session gesture), while
  // the captured drops persist so a hand-authored layout survives a reload. The
  // `placed` list IS the queue: order is placement order (PlaceLayer numbers +
  // threads them 1..n).
  const [placeMode, setPlaceMode] = useState(false);
  const [placed, setPlaced] = usePersistedJSON<{ x: number; y: number }[]>('placedDials', []);
  const dropDial = (x: number, y: number) => setPlaced((prev) => [...prev, { x, y }]);
  const undoDrop = () => setPlaced((prev) => prev.slice(0, -1));
  const clearDrops = () => setPlaced([]);

  // Viewport size drives the graph's right true-border: it tracks the dial rim so
  // the timer end stays tucked under the dial at any width (see graphRightBorderPx).
  const [vp, setVp] = useState(() => ({
    w: typeof document !== 'undefined' ? document.documentElement.clientWidth : 1440,
    h: typeof document !== 'undefined' ? document.documentElement.clientHeight : 900,
  }));
  useEffect(() => {
    const read = () =>
      setVp({ w: document.documentElement.clientWidth, h: document.documentElement.clientHeight });
    // Observe the document element, NOT window 'resize': the client area also
    // changes when a scrollbar appears/disappears (which fires no resize event),
    // and the rim/horizon key off clientWidth, so the graph border must track it.
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(read) : null;
    ro?.observe(document.documentElement);
    window.addEventListener('resize', read);
    read();
    return () => { ro?.disconnect(); window.removeEventListener('resize', read); };
  }, []);
  // ── THE ONE SCALE FACTOR ────────────────────────────────────────────────────
  // Derived from the already-tracked viewport width. Capped at 1 (never upscale past
  // the authored 1440 design) and floored at scaleMin (phones stay legible). Every
  // instrument length downstream multiplies by this; published raw as --ui-scale so
  // CSS text/insets scale generically too.
  const uiScale = clamp(scaleMin, vp.w / DESIGN_W, 1);
  // The timer band tracks the cluster: same coherent factor as everything else,
  // optionally softened via GRAPH_SHRINK. uiScale === 1 ⇒ graphScale === 1, so
  // desktop stays pixel-identical. This is the ONE tuning point for the midline.
  const graphScale = 1 - (1 - uiScale) * GRAPH_SHRINK;
  const graphW = `${graphRightBorderPx(vp.w, vp.h, graphScale).toFixed(1)}px`;

  // Placement readout: each drop annotated with xFromRight = W − x, so a right-
  // anchored capture can be re-pinned width-independently when it's consumed later.
  // Rounded to whole px — the copied list is a hand-authoring source, not raw
  // subpixel data. This is the JSON both the readout box and Copy surface.
  const placedAnnotated = placed.map((p) => ({
    x: Math.round(p.x),
    y: Math.round(p.y),
    xFromRight: Math.round(vp.w - p.x),
  }));
  const placedJSON = JSON.stringify(placedAnnotated, null, 2);
  const copyDrops = () => {
    try {
      navigator.clipboard?.writeText(placedJSON);
    } catch {
      /* clipboard unavailable — no-op */
    }
  };

  function openDrawer(id: string) {
    setFocusedDial(id);
    setDrawerOpen(true);
  }

  // The break hub's full visual state, derived from the signed minute balance.
  // uiScale positions the ball marker on the scaled rim (laps ride the scaled SVG).
  const hub = breakHubView(breakMin, uiScale);

  return (
    <div className="page" style={{
      // ── SCALED px bridges: the existing JS→CSS published dims × uiScale, so the
      // hub, break-trail and corner dial (all sized off these vars) shrink for free.
      '--hub-r': `${HUB_R * uiScale}px`,
      '--hub-shift': `${Y_SHIFT * uiScale}px`,
      '--hub-bottom': `${(HUB_R - Y_SHIFT) * uiScale}px`,
      '--graph-w': graphW,
      // Scaled band height, top-anchored: the .timerfield box shrinks with the
      // cluster on mobile so the midline rises. TimerField measures the box, so
      // horizonY and the whole plot auto-track the shorter band.
      '--graph-h': `${GRAPH_H_VH * graphScale}vh`,
      // Corner-dial diameter, published from the single JS source so .corner-dial
      // and the arc-riding agent dials resize together (see CORNER_DIAL_PX).
      '--corner-dial-d': `${CORNER_DIAL_PX * uiScale}px`,
      // The RAW factor — any CSS length/inset/font scales generically via
      // calc(<base>px * var(--ui-scale)). Unitless so calc() can multiply px by it.
      '--ui-scale': uiScale,
      // instrument-line colour, live from the demobar sliders. Hue locked to brass
      // (41°), opacity always 100%; only saturation + HSL lightness ("brightness")
      // move. Set inline HERE (not via --inst-s/-l on a descendant) so it's fully
      // substituted on .page and every consumer of --instrument inherits it.
      '--instrument': `hsl(41 ${instSat}% ${instLum}%)`,
    } as React.CSSProperties}>
      <div className="page__grain" aria-hidden />

      {/* break hub — large circle in the true upper-right of the PAGE. Unlike the
          fixed dials it scrolls with content, so at scroll-top the dials nest
          into its rim and on scroll-down it's left behind. Static-looking for
          now; becomes the break-time action display in a later round. Radius is
          locked at HUB_R (operator-settled), no longer a demo-bar knob. Past ±2h
          the whole rim glows (gold for credit spillover, red for debt). */}
      <div className={`break-hub${hub.glow ? ` break-hub--${hub.glow}` : ''}`} aria-hidden />

      {/* break-trail — the wake the ball leaves along the rim, one arc per lap.
          Fixed and corner-anchored to the SAME footprint as the hub (concentric
          at scroll-top), floating with the ball on scroll. Each lap paints in its
          own tone (later laps over earlier); all retire once the rim glow takes
          over. Stroke tone + glow are inline so the model owns the palette. */}
      <svg className="break-trail" aria-hidden
        viewBox={`0 0 ${2 * HUB_R} ${2 * HUB_R}`}>
        {hub.laps.map((lap, i) => (
          <path key={i} d={lap.d} stroke={lap.tone} strokeWidth={lap.width}
            style={{ filter: `drop-shadow(0 0 5px color-mix(in srgb, ${lap.tone} 65%, transparent))` }} />
        ))}
      </svg>

      {/* break-time marker — the moving head of the active lap's trail. Fixed +
          corner-anchored like the dials; its tone tracks the active lap and it
          retires (unrendered) once the rim glow takes over past ±2h. */}
      {hub.ball && (
        <div className="break-marker" aria-hidden
          style={{ ...hub.ball.off, '--ball': hub.ball.tone } as React.CSSProperties} />
      )}

      {/* connecting arc — static shape study springing off the dial rim to the
          left border. Own debug state (below); outside the breakHubView contract.
          z:3 so it reads over the timer graph and meets the rim. */}
      <ArcLayer uiScale={uiScale} />

      {/* the big fraction dial in the reserved top-right nook */}
      <CornerDial top={fracTop} bottom={fracBot} />

      {/* floating radial dials — fixed to the viewport corner, follow scroll */}
      <Dials count={dialCount} onOpenDrawer={openDrawer} uiScale={uiScale} />

      {/* left-side TTS-queue stack — the top-left mirror of the status fan,
          sharing the Dial/.ring chrome + geometry core, fed a queue model */}
      <TtsStack depth={ttsDepth} onOpenDrawer={openDrawer} uiScale={uiScale} />

      {/* worker queues — two icon-chip stacks below the lemon that grow outward
          from centre then trail down the two edges (a soft "M"). Fed by the
          Workers slider; click a chip to pop it and reflow the rest up-path. */}
      <WorkerQueues count={workerCount} uiScale={uiScale} gap={W_DROP_PX} pitch={W_SPACE_PX} inset={W_INSET_PX} split={W_SPLIT_PX} />

      {/* idle worker queue — the flipped bottom rail whose clock encodes idle-queue
          depth by numeral colour (design study; below the first-screen composition) */}
      <IdleWorkerQueue clockValue={clockValue} idleCount={idleCount} uiScale={uiScale} animate={!reducedMotion} />

      {/* dials drawer — where the default dial click lands (minimal stub) */}
      <DialsDrawer open={drawerOpen} focusedId={focusedDial} onClose={() => setDrawerOpen(false)} />

      {/* full-bleed timer graph — the TRUE BACKGROUND layer. The arc's opaque
          panel (in ArcLayer) covers it below the curve; only the stretch above
          the arc reads through. The hover crosshair is its sole readout. The
          background dressing (bands + gridlines) bleeds DOWN to the arc's left
          contact + 50px; the arc-fill occludes anything past the curve.
          INVARIANT: bgBottomPx is a VIEWPORT-top px value handed straight into
          the timer SVG's local coords — valid only because .timerfield is
          stapled flush at page-top (see the .timerfield INVARIANT in cockpit.css
          and the ArcLayer top-band note). Don't add a top offset above the graph. */}
      <TimerField bgBottomPx={ARC.leftYFrac * HUB_BOTTOM + 50} uiScale={uiScale} />

      {/* coordinate-capture placement layer — an authoring overlay above the
          timer/arc, below the demobar. Inert unless Place mode is on, at which
          point clicks drop TTS-sized numbered rings whose (x, y) are captured. */}
      <PlaceLayer active={placeMode} placed={placed} onDrop={dropDial} />

      {/* demo control (mockup only) — pinned so it stays reachable while scrolling.
          Collapses to a toggle under the narrow breakpoint (see the RESPONSIVE
          OVERRIDES block in cockpit.css); the toggle is hidden on wide screens. */}
      <section className={`demobar${demoOpen ? ' demobar--open' : ''}`} aria-label="Mockup demo controls">
        <button className="demobar__toggle" onClick={() => setDemoOpen((o) => !o)} aria-expanded={demoOpen}>
          {demoOpen ? '▾ controls' : '▸ controls'}
        </button>
        <div className="demobar__body">
        <span className="demobar__tag">design study · static</span>
        <label className="demobar__slider">
          Scale floor
          <input type="range" min={0.2} max={1} step={0.01} value={scaleMin}
            onChange={(e) => setScaleMin(Number(e.target.value))} />
          <EditableNum value={scaleMin} display={scaleMin.toFixed(2)} step={0.01} onCommit={setScaleMin} />
        </label>
        <span className="demobar__scale" aria-label={`Live UI scale ${uiScale.toFixed(2)}`}>uiScale {uiScale.toFixed(2)}</span>
        <label className="demobar__slider">
          State-dial density
          <input type="range" min={0} max={MAX_DIAL_COUNT} value={dialCount}
            onChange={(e) => setDialCount(Number(e.target.value))} />
          <EditableNum value={dialCount} onCommit={setDialCount} />
        </label>
        <label className="demobar__slider">
          TTS queue depth
          <input type="range" min={0} max={MAX_TTS_DEPTH} value={ttsDepth}
            onChange={(e) => setTtsDepth(Number(e.target.value))} />
          <EditableNum value={ttsDepth} onCommit={setTtsDepth} />
        </label>
        <label className="demobar__slider">
          Workers
          <input type="range" min={0} max={MAX_WORKER_COUNT} value={workerCount}
            onChange={(e) => setWorkerCount(Number(e.target.value))} />
          <EditableNum value={workerCount} onCommit={setWorkerCount} />
        </label>
        <label className="demobar__slider">
          Idle workers
          <input type="range" min={0} max={MAX_IDLE_COUNT} value={idleCount}
            onChange={(e) => setIdleCount(Number(e.target.value))} />
          <EditableNum value={idleCount} onCommit={setIdleCount} />
        </label>
        <label className="demobar__slider">
          Clock value
          <input type="range" min={0} max={6} value={clockValue}
            onChange={(e) => setClockValue(Number(e.target.value))} />
          <EditableNum value={clockValue} onCommit={setClockValue} />
        </label>
        <label className="demobar__slider">
          Break time
          <input type="range" min={-BREAK_MAX_MIN} max={BREAK_MAX_MIN} step={1} value={breakMin}
            onChange={(e) => setBreakMin(Number(e.target.value))} />
          <EditableNum value={breakMin} display={fmtBreak(breakMin)} onCommit={setBreakMin} />
        </label>
        <Stepper label="Focus" value={fracTop} onChange={setFracTop} />
        <Stepper label="Distract" value={fracBot} onChange={setFracBot} />

        {/* ── coordinate-capture placement system (authoring tool) ── */}
        <label className="demobar__check">
          <input type="checkbox" checked={placeMode} onChange={(e) => setPlaceMode(e.target.checked)} />
          Place mode
        </label>
        <div className="demobar__place">
          <span className="demobar__placecount">{placed.length} placed</span>
          <textarea
            className="demobar__placeout"
            readOnly
            value={placedJSON}
            aria-label="Captured placement coordinates (JSON)"
            placeholder="[]"
          />
          <span className="demobar__placebtns">
            <button className="demobar__placebtn" onClick={copyDrops} disabled={placed.length === 0}>Copy</button>
            <button className="demobar__placebtn" onClick={undoDrop} disabled={placed.length === 0}>Undo last</button>
            <button className="demobar__placebtn" onClick={clearDrops} disabled={placed.length === 0}>Clear</button>
          </span>
        </div>
        </div>
      </section>
    </div>
  );
}
