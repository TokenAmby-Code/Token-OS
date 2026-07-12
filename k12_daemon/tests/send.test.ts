import { expect, test } from 'bun:test';
import { SEND_PRESENCE_ACTIVITY_WINDOW_MS, type SendReceipt } from '@token-os/contracts';
import { EventStore } from '../src/store.ts';
import { FakeTmux } from '../src/tmux.ts';
import { Daemon } from '../src/core.ts';

function setup() {
  const tmux = new FakeTmux();
  const store = new EventStore(`/tmp/k12send-${crypto.randomUUID()}.sqlite`);
  return { tmux, store, d: new Daemon(store, tmux) };
}

// A bare seat: launch with NO attestations creates the seat (pane_created) but
// never binds — resolvable as an unbound live seat.
async function bareSeat(d: Daemon, seat: string) {
  await d.launch({ seat_id: seat, schema_version: 1 });
}
async function boundSeat(d: Daemon, seat: string, identity: string) {
  await d.launch({ seat_id: seat, schema_version: 1, identity, persona: 'salamander', tint: '#302800' });
}

// Spec §5: ONE chokepoint; enqueue-by-default; typed gate/refusal causes; the
// receipt carries the SAME resolution the send used (never re-derived).

test('bare pane, operator idle → enqueue-by-default then delivered', async () => {
  const { store, d } = setup();
  await bareSeat(d, 'somnium:NE');
  const res = (await d.send({ target: 'somnium:NE', text: 'hello', schema_version: 1 })) as SendReceipt;
  expect(res.verdict).toBe('delivered');
  expect(res.gate_reason).toBe(null);
  expect(res.bytes_delivered).toBe(5);
  const types = store.readAll().map((e) => e.event_type);
  expect(types).toContain('act.send_enqueued'); // enqueue-by-default, always
  expect(types).toContain('act.send_delivered');
});

test('unresolved target REFUSED at admission — never gated, never enqueued', async () => {
  const { store, d } = setup();
  const res = await d.send({ target: 'ghost:X', text: 'hi', schema_version: 1 });
  // The #699 class is unrepresentable: an unresolved target is a typed REFUSAL,
  // never a typing_guard gate.
  expect(res).toMatchObject({ ok: false, refused: true, reason: 'pane_unresolved', target: 'ghost:X' });
  expect(store.count()).toBe(0); // nothing admitted to the queue
});

test('operator present → gated typing_guard, window echoed, STAYS enqueued (not delivered)', async () => {
  const { tmux, store, d } = setup();
  await bareSeat(d, 'somnium:NE');
  tmux.setPresence('somnium:NE', Date.now()); // active within the window
  const res = (await d.send({ target: 'somnium:NE', text: 'hello', schema_version: 1 })) as SendReceipt;
  expect(res.verdict).toBe('enqueued_gated');
  expect(res.gate_reason).toBe('typing_guard'); // TRUE cause, not pane_unresolved
  expect(res.activity_window_ms).toBe(SEND_PRESENCE_ACTIVITY_WINDOW_MS); // no buried magic number
  const types = store.readAll().map((e) => e.event_type);
  expect(types).toContain('act.send_enqueued');
  expect(types).toContain('act.send_gated');
  expect(types).not.toContain('act.send_delivered'); // blocks-to-ENQUEUE, never delivered while gated
  const gated = store.readAll().find((e) => e.event_type === 'act.send_gated')!;
  expect(gated.payload).toMatchObject({ reason: 'typing_guard', activity_window_ms: SEND_PRESENCE_ACTIVITY_WINDOW_MS });
});

test('receipt carries the send OWN resolution (never re-derived) — bound_seq parity', async () => {
  const { store, d } = setup();
  await boundSeat(d, 'palace:W', 'i-42');
  const boundSeq = store.readAll().find((e) => e.event_type === 'reg.bound')!.seq;
  // Resolve by the INSTANCE id — a different surface than the seat id, so a
  // re-derivation would diverge. It must not.
  const res = (await d.send({ target: 'i-42', text: 'yo', schema_version: 1 })) as SendReceipt;
  expect(res.verdict).toBe('delivered');
  expect(res.resolution.target).toBe('i-42');
  expect(res.resolution.seat_id).toBe('palace:W');
  expect(res.resolution.bound_seq).toBe(boundSeq);
  const delivered = store.readAll().find((e) => e.event_type === 'act.send_delivered')!;
  expect(delivered.payload.resolved_seq).toBe(boundSeq); // the SAME seq the send resolved against
});

test('schema_version mismatch REFUSED loud, nothing admitted', async () => {
  const { store, d } = setup();
  await bareSeat(d, 'somnium:NE');
  const before = store.count();
  const res = await d.send({ target: 'somnium:NE', text: 'x', schema_version: 999 });
  expect(res).toMatchObject({ refused: true, reason: 'schema_version_mismatch' });
  expect(store.count()).toBe(before); // no enqueue on a rejected schema
});
