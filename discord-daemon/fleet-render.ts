// fleet-render.ts — pure OpsState → #fleet-status message body. No I/O:
// the publisher owns polling/editing; this owns only the rendering contract
// (every active instance named, content within Discord's 2000-char limit).

export const DISCORD_CONTENT_LIMIT = 2000;

const STATUS_ICONS = {
  processing: '🟢',
  working: '🟢',
  reviewing: '🟡',
  waiting: '🟡',
  planning: '🔵',
  stopped: '⚫',
  victorious: '🏆',
};

function statusIcon(status) {
  return STATUS_ICONS[status] || '⚪';
}

function instanceLine(inst) {
  const name = inst.display_name || inst.id;
  const parts = [`${statusIcon(inst.status)} **${name}**`];
  if (name !== inst.id) parts.push(`(${inst.id})`);
  parts.push(`— ${inst.status || 'unknown'}`);
  if (inst.engine) parts.push(`· ${inst.engine}`);
  return parts.join(' ');
}

export function renderFleetStatus(opsState) {
  const active = opsState?.instances?.active ?? [];
  const counts = opsState?.instances?.counts ?? {};
  const generatedAt = opsState?.generated_at || '';
  const timerMode = opsState?.timer?.mode;

  const header = [
    `**Fleet Status** — ${active.length} active` +
      (typeof counts.stale === 'number' && counts.stale > 0 ? ` · ${counts.stale} stale` : ''),
    timerMode ? `mode: ${timerMode}` : null,
    generatedAt ? `as of ${generatedAt}` : null,
  ]
    .filter(Boolean)
    .join(' · ');

  const lines = [header, ''];
  let omitted = 0;
  for (const inst of active) {
    const line = instanceLine(inst);
    // Reserve room for a trailing "… +N more" marker so the cut is honest.
    const used = lines.join('\n').length;
    if (used + line.length + 24 > DISCORD_CONTENT_LIMIT) {
      omitted += 1;
      continue;
    }
    lines.push(line);
  }
  if (omitted > 0) lines.push(`… +${omitted} more`);
  if (active.length === 0) lines.push('_no active instances_');

  return { content: lines.join('\n') };
}
