import { expect, test } from 'bun:test';
import { EventStore } from '../src/store.ts';
import { FakeTmux } from '../src/tmux.ts';
import { Daemon } from '../src/core.ts';
import { buildProjections } from '../src/projections.ts';

function setup() {
  const store = new EventStore(`/tmp/k12close-${crypto.randomUUID()}.sqlite`);
  const tmux = new FakeTmux();
  return { store, tmux, d: new Daemon(store, tmux) };
}

const FULL = { schema_version: 1, identity: 'i1', persona: 'salamander', tint: '#302800' } as const;

// Rung 3: /close is the generic "close this instance" system. For the persistent
// estate it REAPS the agent process, KEEPS the pane (respawned bare), and returns
// the seat to the freelist — retired + process_reaped + seat_cleared, atomic.

test('close reaps the process, keeps the pane, returns the seat to the freelist', async () => {
  const { store, tmux, d } = setup();
  await d.launch({ seat_id: 'palace:W', ...FULL });
  const res = await d.close({ target: 'palace:W', schema_version: 1 });
  expect(res).toMatchObject({ ok: true, closed: true, seat_id: 'palace:W', instance_id: 'i1' });

  const types = store.readAll().map((e) => e.event_type);
  expect(types).toContain('reg.retired');
  expect(types).toContain('reg.process_reaped');
  expect(types).toContain('reg.seat_cleared');

  // Estate seat survives: pane still live, back on the freelist, unbound.
  const p = buildProjections(store.readAll());
  expect(p.currentBindings).toEqual([]);
  expect(p.freelist).toEqual([{ seat_id: 'palace:W', pane_state: 'live' }]);
  expect((await tmux.listSeats()).find((s) => s.seat_id === 'palace:W')!.pane).toBe('live');
});

test('close resolves by instance id as well as seat id', async () => {
  const { d } = setup();
  await d.launch({ seat_id: 'somnium:NE', ...FULL });
  const res = await d.close({ target: 'i1', schema_version: 1 }); // by instance id
  expect(res).toMatchObject({ ok: true, closed: true, seat_id: 'somnium:NE', instance_id: 'i1' });
});

test('close of a non-bound target refuses loud — no events, never a silent no-op', async () => {
  const { store, d } = setup();
  const res = await d.close({ target: 'palace:W', schema_version: 1 }); // never launched
  expect(res).toMatchObject({ ok: false, closed: false });
  expect(res.reason).toContain('no_binding');
  expect(store.count()).toBe(0);
});

test('a failed reap refuses loud and writes NO retire chain (retire-with-live-process unspellable)', async () => {
  const { store, tmux, d } = setup();
  await d.launch({ seat_id: 'palace:N', ...FULL });
  tmux.failReapSeat('palace:N');
  const before = store.count();
  const res = await d.close({ target: 'palace:N', schema_version: 1 });
  expect(res).toMatchObject({ ok: false, closed: false });
  expect(res.reason).toContain('reap_failed');
  // Nothing appended: the binding stands, no retired/process_reaped/seat_cleared.
  expect(store.count()).toBe(before);
  const p = buildProjections(store.readAll());
  expect(p.currentBindings.map((b) => b.seat_id)).toEqual(['palace:N']);
});

test('schema mismatch refuses close loud', async () => {
  const { store, d } = setup();
  await d.launch({ seat_id: 'palace:E', ...FULL });
  const res = await d.close({ target: 'palace:E', schema_version: 999 });
  expect(res).toMatchObject({ ok: false, closed: false });
  expect(res.reason).toContain('schema_version_mismatch');
});
