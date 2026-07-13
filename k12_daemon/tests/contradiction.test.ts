import { expect, test } from 'bun:test';
import { EventStore } from '../src/store.ts';
import { FakeTmux } from '../src/tmux.ts';
import { Daemon } from '../src/core.ts';

function setup() {
  const tmux = new FakeTmux();
  const store = new EventStore(`/tmp/k12contra-${crypto.randomUUID()}.sqlite`);
  return { tmux, store, d: new Daemon(store, tmux) };
}

// Spec §6 rung 5: an out-of-band pane kill (the retire/reap/clear chain never
// ran) is a REAL contradiction. Reconcile observes tmux and emits a typed
// contradiction_flagged — NEVER a synthesized lifecycle event. Bring-up mode:
// any open contradiction is p0, fail loud, /health ok=false.

test('out-of-band pane kill on a bound seat → contradiction_flagged, p0, health ok=false', async () => {
  const { tmux, store, d } = setup();
  await d.launch({ seat_id: 'palace:W', schema_version: 1, identity: 'i-1', persona: 'salamander', tint: '#302800' });

  // Raw kill below the daemon — no teardown_started/process_reaped/seat_cleared attested.
  tmux.killOutOfBand('palace:W');

  const rec = await d.reconcile();
  expect(rec.p0).toBe(true); // bring-up: every contradiction is p0
  expect(rec.ok).toBe(false);
  const flagged = rec.new_contradictions.find((c) => c.kind === 'bound_pane_dead');
  expect(flagged).toBeDefined();
  expect(flagged!.entity_id).toBe('palace:W');
  expect(flagged!.missing_attestation).toBe('seat_cleared'); // names the exact missing attestation

  // A contradiction FLAG is written to the stream; no lifecycle event is synthesized.
  const types = store.readByEntity('palace:W').map((e) => e.event_type);
  expect(types).toContain('reg.contradiction_flagged');
  expect(types).not.toContain('reg.retired');
  expect(types).not.toContain('reg.seat_cleared');

  // Honest health: ok=false while any contradiction is open.
  const h = await d.health('k12-personal', { version: '0', git_sha: 'x', bun: 'y' });
  expect(h.ok).toBe(false);
  expect(h.open_contradictions).toBeGreaterThan(0);
});

test('re-reconcile does not double-flag an already-open contradiction', async () => {
  const { tmux, d } = setup();
  await d.launch({ seat_id: 'palace:W', schema_version: 1, identity: 'i-1', persona: 'salamander', tint: '#302800' });
  tmux.killOutOfBand('palace:W');

  const first = await d.reconcile();
  expect(first.new_contradictions).toHaveLength(1);

  const second = await d.reconcile();
  expect(second.new_contradictions).toHaveLength(0); // already flagged & still open — not re-emitted
  expect(second.open_contradictions.length).toBeGreaterThan(0); // still open
  expect(second.p0).toBe(true);
});
