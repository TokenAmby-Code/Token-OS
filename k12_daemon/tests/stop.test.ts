import { expect, test } from 'bun:test';
import { EventStore } from '../src/store.ts';
import { FakeTmux } from '../src/tmux.ts';
import { Daemon } from '../src/core.ts';
import { buildProjections } from '../src/projections.ts';

function setup() {
  const store = new EventStore(`/tmp/k12stop-${crypto.randomUUID()}.sqlite`);
  const tmux = new FakeTmux();
  return { store, tmux, d: new Daemon(store, tmux) };
}

const FULL = { schema_version: 2, identity: 'i1', persona: 'salamander', tint: '#302800' } as const;

// Rung 3: /stop is the stop-hook's door. Three honest outcomes, no blind swallow.

test('fresh stop for a bound live instance is recorded → activity stopped', async () => {
  const { store, d } = setup();
  await d.launch({ seat_id: 'palace:W', ...FULL });
  const res = await d.stop({ instance_id: 'i1', schema_version: 2 });
  expect(res).toEqual({ ok: true, instance_id: 'i1', recorded: true, deduped: false, activity: 'stopped', auto_close: 'none' });
  expect(store.readAll().filter((e) => e.event_type === 'act.stop_reported')).toHaveLength(1);
  expect(buildProjections(store.readAll()).activityBoard.find((r) => r.seat_id === 'palace:W')!.activity).toBe('stopped');
});

test('duplicate stop is deduped (receipt_deduped), not a second stop_reported — no blind swallow', async () => {
  const { store, d } = setup();
  await d.launch({ seat_id: 'palace:W', ...FULL });
  await d.stop({ instance_id: 'i1', schema_version: 2 });
  const res = await d.stop({ instance_id: 'i1', schema_version: 2 });
  expect(res).toMatchObject({ ok: true, recorded: false, deduped: true });
  expect(store.readAll().filter((e) => e.event_type === 'act.stop_reported')).toHaveLength(1);
  expect(store.readAll().filter((e) => e.event_type === 'act.receipt_deduped')).toHaveLength(1);
});

test('GHOST stop — instance never bound — is refused loud; nothing recorded', async () => {
  const { store, d } = setup();
  const res = await d.stop({ instance_id: '77f7cfb4-orphan', schema_version: 2 });
  expect(res).toEqual({ ok: false, refused: true, reason: 'no_such_instance', instance_id: '77f7cfb4-orphan' });
  // The whole point: no phantom row, no stop_reported, no dedupe — zero footprint.
  expect(store.count()).toBe(0);
});

test('a stop AFTER close (bound-then-cleared) is deduped, NOT treated as a ghost', async () => {
  const { store, d } = setup();
  await d.launch({ seat_id: 'palace:W', ...FULL });
  await d.stop({ instance_id: 'i1', schema_version: 2 });
  await d.close({ target: 'i1', schema_version: 2 });
  const res = await d.stop({ instance_id: 'i1', schema_version: 2 }); // late stop, seat already freed
  expect(res).toMatchObject({ ok: true, recorded: false, deduped: true });
  // everBound distinguishes this from a ghost: it is NOT refused.
  expect('refused' in res).toBe(false);
});

test('schema mismatch refuses stop loud', async () => {
  const { store, d } = setup();
  await d.launch({ seat_id: 'palace:W', ...FULL });
  const res = await d.stop({ instance_id: 'i1', schema_version: 999 });
  expect(res).toMatchObject({ ok: false, refused: true, reason: 'schema_version_mismatch' });
  expect(store.readAll().some((e) => e.event_type === 'act.stop_reported')).toBe(false);
});
