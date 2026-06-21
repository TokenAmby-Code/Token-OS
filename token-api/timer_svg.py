"""Pure-Python SVG renderer for the daily timer break-balance graph.

The end-of-day flush (`_sync_generate_daily_analytics`) builds a `summary` dict
with a `balance_timeline` series for the prior day. This module renders that
series into a self-contained `<svg>…</svg>` string — no matplotlib/PIL (neither
is installed), no I/O, no browser. The flush writes the string to
`analytics/timer-<date>.svg` and the daily note embeds it via a wikilink.

The output is a single inline-styled `<svg viewBox=...>` element so Obsidian
renders it from a `![[timer-<date>.svg]]` embed.
"""

from __future__ import annotations

from datetime import datetime
from xml.sax.saxutils import escape

from timer import format_timer_time

# ── Canvas geometry ───────────────────────────────────────────────────────────
_WIDTH = 800
_HEIGHT = 400
_M_LEFT = 70
_M_RIGHT = 20
_M_TOP = 50
_M_BOTTOM = 45
_PLOT_X0 = _M_LEFT
_PLOT_X1 = _WIDTH - _M_RIGHT
_PLOT_Y0 = _M_TOP
_PLOT_Y1 = _HEIGHT - _M_BOTTOM
_PLOT_W = _PLOT_X1 - _PLOT_X0
_PLOT_H = _PLOT_Y1 - _PLOT_Y0

# ── Palette (inline styles only — Obsidian strips <style> in some contexts) ────
_BG = "#1e1e2e"
_FG = "#cdd6f4"
_MUTED = "#6c7086"
_GRID = "#313244"
_POS = "#a6e3a1"  # earned break (positive balance)
_NEG = "#f38ba8"  # backlog (negative balance)
_LINE = "#89b4fa"


def _f(value: float) -> str:
    """Format a coordinate compactly (no trailing-zero noise)."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _parse_time(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _placeholder_svg(message: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_WIDTH} {_HEIGHT}" '
        f'width="{_WIDTH}" height="{_HEIGHT}" role="img" '
        f'aria-label="{escape(message)}">'
        f'<rect width="{_WIDTH}" height="{_HEIGHT}" fill="{_BG}"/>'
        f'<text x="{_WIDTH / 2}" y="{_HEIGHT / 2}" fill="{_MUTED}" '
        f'font-family="sans-serif" font-size="22" text-anchor="middle" '
        f'dominant-baseline="middle">{escape(message)}</text>'
        f"</svg>"
    )


def _x_fractions(timeline: list[dict]) -> list[float]:
    """Map each point to a 0..1 horizontal fraction.

    Uses wall-clock spacing when every timestamp parses and the day spans a
    non-zero range; otherwise falls back to evenly-spaced indices.
    """
    n = len(timeline)
    times = [_parse_time(pt.get("time")) for pt in timeline]
    if all(t is not None for t in times):
        epochs = [t.timestamp() for t in times]  # type: ignore[union-attr]
        lo, hi = min(epochs), max(epochs)
        span = hi - lo
        if span > 0:
            return [(e - lo) / span for e in epochs]
    if n == 1:
        return [0.0]
    return [i / (n - 1) for i in range(n)]


def render_timer_svg(summary: dict) -> str:
    """Render the day's break-balance timeline as a self-contained SVG string.

    ``summary`` is the dict built by ``_sync_generate_daily_analytics``:
    ``balance_timeline`` (``[{"time": iso, "balance_ms": int}, ...]``),
    ``date``, ``peak_break_balance_ms``, ``min_break_balance_ms``. Degenerate
    input (missing/short timeline) renders a valid "no timer data" placeholder
    rather than raising.
    """
    date_str = str(summary.get("date") or "")
    timeline = summary.get("balance_timeline") or []
    if not isinstance(timeline, list) or len(timeline) < 2:
        return _placeholder_svg(f"No timer data · {date_str}".strip(" ·"))

    balances = [int(pt.get("balance_ms") or 0) for pt in timeline]
    peak = int(summary.get("peak_break_balance_ms") or max(balances))
    trough = int(summary.get("min_break_balance_ms") or min(balances))

    # Y domain always includes the zero baseline (positive = earned, negative = backlog).
    y_lo = min(0, trough, min(balances))
    y_hi = max(0, peak, max(balances))
    span = y_hi - y_lo
    if span == 0:
        # Flat-at-zero (or single-value) line: pad so the baseline sits mid-plot.
        y_lo, y_hi, span = y_lo - 1, y_hi + 1, 2
    pad = span * 0.08
    y_lo_p = y_lo - pad
    y_hi_p = y_hi + pad

    def y_of(value: float) -> float:
        return _PLOT_Y1 - (value - y_lo_p) / (y_hi_p - y_lo_p) * _PLOT_H

    fractions = _x_fractions(timeline)
    pts = [
        (_PLOT_X0 + frac * _PLOT_W, y_of(bal))
        for frac, bal in zip(fractions, balances, strict=False)
    ]
    zero_y = y_of(0)

    line_pts = " ".join(f"{_f(x)},{_f(y)}" for x, y in pts)
    # Area polygon: line, then back along the zero baseline to close.
    area_pts = f"{line_pts} {_f(pts[-1][0])},{_f(zero_y)} {_f(pts[0][0])},{_f(zero_y)}"

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_WIDTH} {_HEIGHT}" '
        f'width="{_WIDTH}" height="{_HEIGHT}" role="img" '
        f'aria-label="Break-balance timer graph for {escape(date_str)}">'
    )
    parts.append(f'<rect width="{_WIDTH}" height="{_HEIGHT}" fill="{_BG}"/>')

    # Split the area into above/below the baseline via clip rects so earned time
    # reads green and backlog reads red.
    above_h = max(0.0, zero_y - _PLOT_Y0)
    below_h = max(0.0, _PLOT_Y1 - zero_y)
    parts.append(
        f'<clipPath id="pos"><rect x="{_PLOT_X0}" y="{_f(_PLOT_Y0)}" '
        f'width="{_PLOT_W}" height="{_f(above_h)}"/></clipPath>'
        f'<clipPath id="neg"><rect x="{_PLOT_X0}" y="{_f(zero_y)}" '
        f'width="{_PLOT_W}" height="{_f(below_h)}"/></clipPath>'
    )
    parts.append(
        f'<polygon points="{area_pts}" fill="{_POS}" fill-opacity="0.28" clip-path="url(#pos)"/>'
    )
    parts.append(
        f'<polygon points="{area_pts}" fill="{_NEG}" fill-opacity="0.28" clip-path="url(#neg)"/>'
    )

    # Zero baseline.
    parts.append(
        f'<line x1="{_PLOT_X0}" y1="{_f(zero_y)}" x2="{_PLOT_X1}" y2="{_f(zero_y)}" '
        f'stroke="{_MUTED}" stroke-width="1" stroke-dasharray="4 3"/>'
    )

    # Balance line.
    parts.append(
        f'<polyline points="{line_pts}" fill="none" stroke="{_LINE}" '
        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
    )

    # Peak / min markers (point of extreme balance).
    peak_idx = max(range(len(balances)), key=lambda i: balances[i])
    min_idx = min(range(len(balances)), key=lambda i: balances[i])
    for idx, color in ((peak_idx, _POS), (min_idx, _NEG)):
        mx, my = pts[idx]
        label = format_timer_time(balances[idx])
        ty = my - 8 if idx == peak_idx else my + 18
        parts.append(f'<circle cx="{_f(mx)}" cy="{_f(my)}" r="3.5" fill="{color}"/>')
        parts.append(
            f'<text x="{_f(mx)}" y="{_f(ty)}" fill="{color}" font-family="sans-serif" '
            f'font-size="13" text-anchor="middle">{escape(label)}</text>'
        )

    # Y-axis labels: peak (top), zero, min (bottom).
    parts.append(
        f'<text x="{_M_LEFT - 8}" y="{_f(y_of(y_hi))}" fill="{_FG}" '
        f'font-family="sans-serif" font-size="13" text-anchor="end" '
        f'dominant-baseline="middle">{escape(format_timer_time(y_hi))}</text>'
    )
    parts.append(
        f'<text x="{_M_LEFT - 8}" y="{_f(zero_y)}" fill="{_MUTED}" '
        f'font-family="sans-serif" font-size="13" text-anchor="end" '
        f'dominant-baseline="middle">{escape(format_timer_time(0))}</text>'
    )
    parts.append(
        f'<text x="{_M_LEFT - 8}" y="{_f(y_of(y_lo))}" fill="{_FG}" '
        f'font-family="sans-serif" font-size="13" text-anchor="end" '
        f'dominant-baseline="middle">{escape(format_timer_time(y_lo))}</text>'
    )

    # X-axis end labels (HH:MM when the timestamps parse).
    first_t = _parse_time(timeline[0].get("time"))
    last_t = _parse_time(timeline[-1].get("time"))
    if first_t and last_t:
        parts.append(
            f'<text x="{_PLOT_X0}" y="{_PLOT_Y1 + 20}" fill="{_MUTED}" '
            f'font-family="sans-serif" font-size="12" text-anchor="start">'
            f"{escape(first_t.strftime('%H:%M'))}</text>"
        )
        parts.append(
            f'<text x="{_PLOT_X1}" y="{_PLOT_Y1 + 20}" fill="{_MUTED}" '
            f'font-family="sans-serif" font-size="12" text-anchor="end">'
            f"{escape(last_t.strftime('%H:%M'))}</text>"
        )

    # Title.
    parts.append(
        f'<text x="{_PLOT_X0}" y="30" fill="{_FG}" font-family="sans-serif" '
        f'font-size="18" font-weight="bold">Break balance · {escape(date_str)}</text>'
    )

    parts.append("</svg>")
    return "".join(parts)
