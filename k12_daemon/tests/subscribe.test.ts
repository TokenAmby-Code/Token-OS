import { expect, test } from 'bun:test';
import { EventStore } from '../src/store.ts';
import { FakeTmux } from '../src/tmux.ts';
import { Daemon } from '../src/core.ts';
import { buildProjections } from '../src/projections.ts';

function setup() {
  const store = new EventStore(`/tmp/k12sub-${crypto.randomUUID()}.sqlite`);
  const tmux = new FakeTmux();
  return { store, tmux, d: new Daemon(store, tmux) };
}

const FULL = { schema_version: 2, identity: 'i1', persona: 'salamander', tint: '#302800' } as const;

// Rung 3 PR-B: the generic stop-hook subscription system composes with /stop to
// give `final message → auto-close on next stop-hook`. No bespoke latch.

test('subscribe records reg.stop_subscribed for a bound instance', async () => {
  const { store, d } = setup();
  await d.launch({ seat_id: 'palace:W', ...FULL });
  const res = await d.subscribe({ instance_id: 'i1', schema_version: 2, action: 'close' });
  expect(res).toMatchObject({ ok: true, subscribed: true, action: 'close' });
  expect(store.readAll().some((e) => e.event_type === 'reg.stop_subscribed')).toBe(true);
  expect(buildProjections(store.readAll()).openStopSubscriptions.has('i1')).toBe(true);
});

test('subscribe is BOUND-KEYED — an unbound/never-bound instance is refused (ghost cannot subscribe)', async () => {
  const { store, d } = setup();
  const res = await d.subscribe({ instance_id: '77f7cfb4-orphan', schema_version: 2, action: 'close' });
  expect(res).toMatchObject({ ok: false, subscribed: false });
  expect(res.reason).toContain('not_bound');
  expect(store.count()).toBe(0); // nothing recorded — no orphan subscription can exist
});

test('COMPOSE: final message → auto-close on next stop-hook (seat returns to freelist, estate stands)', async () => {
  const { store, tmux, d } = setup();
  await d.launch({ seat_id: 'palace:W', ...FULL });
  await d.subscribe({ instance_id: 'i1', schema_version: 2, action: 'close' });

  // The stop-hook fires → the open subscription reflexively closes the instance.
  const res = await d.stop({ instance_id: 'i1', schema_version: 2 });
  expect(res).toMatchObject({ ok: true, recorded: true, auto_close: 'fired' });

  const types = store.readAll().map((e) => e.event_type);
  expect(types).toContain('act.stop_reported');
  expect(types).toContain('reg.retired');
  expect(types).toContain('reg.process_reaped');
  expect(types).toContain('reg.seat_cleared');

  // Estate seat survives, unbound, back on the freelist (pane still live).
  const p = buildProjections(store.readAll());
  expect(p.currentBindings).toEqual([]);
  expect(p.freelist).toEqual([{ seat_id: 'palace:W', pane_state: 'live' }]);
  expect((await tmux.listSeats()).find((s) => s.seat_id === 'palace:W')!.pane).toBe('live');
});

test('a SECOND stop after auto-close is deduped — the subscription NEVER re-fires', async () => {
  const { store, d } = setup();
  await d.launch({ seat_id: 'palace:W', ...FULL });
  await d.subscribe({ instance_id: 'i1', schema_version: 2, action: 'close' });
  await d.stop({ instance_id: 'i1', schema_version: 2 }); // fires auto-close

  const res = await d.stop({ instance_id: 'i1', schema_version: 2 }); // late/dup stop
  expect(res).toMatchObject({ ok: true, recorded: false, deduped: true, auto_close: 'none' });
  // Exactly ONE retire chain — no re-fire (satiated-once).
  expect(store.readAll().filter((e) => e.event_type === 'reg.retired')).toHaveLength(1);
  expect(store.readAll().filter((e) => e.event_type === 'reg.seat_cleared')).toHaveLength(1);
});

test('a stop with NO subscription does not auto-close (auto_close none, instance just stopped)', async () => {
  const { store, d } = setup();
  await d.launch({ seat_id: 'palace:W', ...FULL });
  const res = await d.stop({ instance_id: 'i1', schema_version: 2 });
  expect(res).toMatchObject({ recorded: true, auto_close: 'none' });
  expect(store.readAll().some((e) => e.event_type === 'reg.seat_cleared')).toBe(false);
  expect(buildProjections(store.readAll()).currentBindings.map((b) => b.seat_id)).toEqual(['palace:W']);
});

test('auto-close whose reap FAILS is loud (auto_close reap_failed), instance left stopped+bound — no silent leak', async () => {
  const { store, tmux, d } = setup();
  await d.launch({ seat_id: 'palace:N', ...FULL });
  await d.subscribe({ instance_id: 'i1', schema_version: 2, action: 'close' });
  tmux.failReapSeat('palace:N');

  const res = await d.stop({ instance_id: 'i1', schema_version: 2 });
  expect(res).toMatchObject({ ok: true, recorded: true, auto_close: 'reap_failed' });
  // Stop is recorded, but the close chain never wrote — binding stands (visible).
  const p = buildProjections(store.readAll());
  expect(store.readAll().some((e) => e.event_type === 'reg.seat_cleared')).toBe(false);
  expect(p.currentBindings.map((b) => b.seat_id)).toEqual(['palace:N']);
});

test('schema mismatch refuses subscribe loud', async () => {
  const { store, d } = setup();
  await d.launch({ seat_id: 'palace:W', ...FULL });
  const res = await d.subscribe({ instance_id: 'i1', schema_version: 999, action: 'close' });
  expect(res).toMatchObject({ ok: false, subscribed: false });
  expect(res.reason).toContain('schema_version_mismatch');
  expect(store.readAll().some((e) => e.event_type === 'reg.stop_subscribed')).toBe(false);
});
