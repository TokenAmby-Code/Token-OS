import { expect, test } from 'bun:test';
import { Database } from 'bun:sqlite';
import { EventStore } from '../src/store.ts';
import type { EventInput } from '@token-os/contracts';

function tmpDb() {
  return `/tmp/k12test-${crypto.randomUUID()}.sqlite`;
}

function ev(over: Partial<EventInput> = {}): EventInput {
  return {
    entity_type: 'seat',
    entity_id: 'somnium:NE',
    event_type: 'reg.pane_created',
    payload: { pane_state: 'live' },
    provenance: { source: 'wrapper', transport_receipt: 'edge_proxy', emitter_version: 1 },
    occurred_at: '2026-07-12T00:00:00.000Z',
    ...over,
  };
}

test('append assigns monotonic seq and a daemon recorded_at', () => {
  let tick = 0;
  const store = new EventStore(tmpDb(), () => `2026-07-12T00:00:0${tick++}.000Z`);
  const a = store.append(ev());
  const b = store.append(ev({ event_type: 'reg.bound', payload: { instance_id: 'i', persona: 'p', tint: '#111' } }));
  expect(a.seq).toBe(1);
  expect(b.seq).toBe(2);
  expect(a.recorded_at).toBe('2026-07-12T00:00:00.000Z');
  expect(b.recorded_at).toBe('2026-07-12T00:00:01.000Z');
  expect(store.count()).toBe(2);
  store.close();
});

test('events table is structurally append-only (UPDATE/DELETE raise)', () => {
  const path = tmpDb();
  const store = new EventStore(path);
  store.append(ev());
  // Reach the underlying db via a fresh handle on the same file.
  const raw = new Database(path);
  expect(() => raw.exec("UPDATE events SET entity_id = 'x'")).toThrow(/append-only/);
  expect(() => raw.exec('DELETE FROM events')).toThrow(/append-only/);
  raw.close();
  store.close();
});

test('readByEntity returns only that entity in seq order', () => {
  const store = new EventStore(tmpDb());
  store.append(ev({ entity_id: 'seatA' }));
  store.append(ev({ entity_id: 'seatB' }));
  store.append(ev({ entity_id: 'seatA', event_type: 'reg.seat_cleared', payload: {} }));
  const a = store.readByEntity('seatA');
  expect(a.map((e) => e.event_type)).toEqual(['reg.pane_created', 'reg.seat_cleared']);
  expect(a.every((e) => e.entity_id === 'seatA')).toBe(true);
  store.close();
});

test('provenance round-trips as structured JSON', () => {
  const store = new EventStore(tmpDb());
  const rec = store.append(ev());
  const back = store.readAll()[0]!;
  expect(back.provenance).toEqual({ source: 'wrapper', transport_receipt: 'edge_proxy', emitter_version: 1 });
  expect(rec.provenance.source).toBe('wrapper');
  store.close();
});
