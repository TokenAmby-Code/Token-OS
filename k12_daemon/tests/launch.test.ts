import { expect, test } from 'bun:test';
import { EventStore } from '../src/store.ts';
import { FakeTmux } from '../src/tmux.ts';
import { Daemon } from '../src/core.ts';

function setup() {
  const store = new EventStore(`/tmp/k12launch-${crypto.randomUUID()}.sqlite`);
  return { store, d: new Daemon(store, new FakeTmux()) };
}

// Spec §4: reg-audit is a LAUNCH PHASE. The endpoint creates a seat but refuses
// handover unless every attestation-defined-so-far is present. Binding is atomic.

test('missing attestation refuses handover — seat created, NO bound event', async () => {
  const { store, d } = setup();
  const res = await d.launch({ seat_id: 'somnium:NE', schema_version: 1, identity: 'i1', persona: 'p' }); // tint missing
  expect(res.handover).toBe(false);
  expect(res.missing_attestations).toEqual(['tint']);
  const types = store.readAll().map((e) => e.event_type);
  expect(types).toContain('reg.pane_created'); // seat WAS created (scaffold)
  expect(types).not.toContain('reg.bound'); // ...but never half-bound
});

test('full attestation tuple hands over with ONE atomic bound event', async () => {
  const { store, d } = setup();
  const res = await d.launch({ seat_id: 'palace:W', schema_version: 1, identity: 'i1', persona: 'salamander', tint: '#302800' });
  expect(res.handover).toBe(true);
  expect(res.missing_attestations).toEqual([]);
  const bound = store.readAll().filter((e) => e.event_type === 'reg.bound');
  expect(bound).toHaveLength(1);
  expect(bound[0]!.payload).toMatchObject({ instance_id: 'i1', persona: 'salamander', tint: '#302800' });
});

test('schema_version mismatch refuses loud, no seat, no bind', async () => {
  const { store, d } = setup();
  const res = await d.launch({ seat_id: 'x', schema_version: 999, identity: 'i', persona: 'p', tint: '#1' });
  expect(res.handover).toBe(false);
  expect(res.reason).toContain('schema_version_mismatch');
  expect(store.count()).toBe(0);
});
