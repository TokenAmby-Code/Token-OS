// Pure formatting helpers. No semantic inference — display only.

export function formatDuration(ms: number): string {
  const sign = ms < 0 ? '-' : '';
  const totalSeconds = Math.round(Math.abs(ms) / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${sign}${hours}h ${minutes.toString().padStart(2, '0')}m`;
  if (minutes > 0) return `${sign}${minutes}m ${seconds.toString().padStart(2, '0')}s`;
  return `${sign}${seconds}s`;
}

// Signed minutes with explicit + / - prefix, for break balance readouts.
export function formatSignedDuration(ms: number): string {
  if (Math.abs(ms) < 1000) return '0';
  const body = formatDuration(Math.abs(ms));
  return ms < 0 ? `-${body}` : `+${body}`;
}

export function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—';
  return formatDuration(seconds * 1000);
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return parsed.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export function formatClock(value: string | null | undefined): string {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return parsed.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function compactPath(value: string | null | undefined): string {
  if (!value) return '—';
  const parts = value.split('/').filter(Boolean);
  if (parts.length <= 2) return value;
  return `…/${parts.slice(-2).join('/')}`;
}

// Best-effort one-line summary of an opaque event `details` blob.
export function summarizeDetails(details: unknown): string {
  if (details == null) return '';
  if (typeof details === 'string') return details;
  if (typeof details !== 'object') return String(details);
  const obj = details as Record<string, unknown>;
  for (const key of ['message', 'reason', 'name', 'tab_name', 'app', 'mode', 'status']) {
    const v = obj[key];
    if (typeof v === 'string' && v.trim()) return v;
  }
  const firstScalar = Object.entries(obj).find(
    ([, v]) => typeof v === 'string' || typeof v === 'number',
  );
  return firstScalar ? `${firstScalar[0]}: ${firstScalar[1]}` : '';
}
