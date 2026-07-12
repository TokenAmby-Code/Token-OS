import { expect, test } from 'bun:test';
import { EventStore } from '../src/store.ts';
import { FakeTmux } from '../src/tmux.ts';
import { Daemon } from '../src/core.ts';
import { buildRoutes, makeServer } from '../src/server.ts';

function daemon() {
  return new Daemon(new EventStore(`/tmp/k12route-${crypto.randomUUID()}.sqlite`), new FakeTmux());
}
const build = { version: '0.1.0', git_sha: 'test', bun: '1.0' };

// The `/api/instances/all` lesson: a collection route must be registered before
// a parameterized route that could shadow it.
test('collection /entities is ordered before parameterized /entities/:id/events', () => {
  const routes = buildRoutes(daemon(), build, 'test');
  const collectionIdx = routes.findIndex((r) => r.label === 'GET /entities');
  const paramIdx = routes.findIndex((r) => r.label === 'GET /entities/:id/events');
  expect(collectionIdx).toBeGreaterThanOrEqual(0);
  expect(paramIdx).toBeGreaterThanOrEqual(0);
  expect(collectionIdx).toBeLessThan(paramIdx);
});

test('GET /entities resolves to the collection route, not the events route', () => {
  const routes = buildRoutes(daemon(), build, 'test');
  const first = routes.find((r) => r.method === 'GET' && r.match('/entities'));
  expect(first!.label).toBe('GET /entities');
  // And the param route does NOT match the bare collection path.
  const paramRoute = routes.find((r) => r.label === 'GET /entities/:id/events')!;
  expect(paramRoute.match('/entities')).toBeNull();
  expect(paramRoute.match('/entities/somnium:NE/events')).toEqual({ id: 'somnium:NE' });
});

test('server serves collection at /entities and event stream at /entities/:id/events', async () => {
  const d = daemon();
  await d.launch({ seat_id: 'somnium:NE', schema_version: 1, identity: 'i1', persona: 'p', tint: '#1' });
  const srv = makeServer({ bind: '127.0.0.1', port: 21000 + Math.floor(Math.random() * 9000), daemon: d, build, machine: 'test' });
  try {
    const coll = await fetch(`http://127.0.0.1:${srv.port}/entities`);
    const collBody = await coll.json();
    expect(Array.isArray(collBody.rows)).toBe(true);
    expect(collBody.rows[0].seat_id).toBe('somnium:NE');

    const stream = await fetch(`http://127.0.0.1:${srv.port}/entities/${encodeURIComponent('somnium:NE')}/events`);
    const streamBody = await stream.json();
    expect(streamBody.entity_id).toBe('somnium:NE');
    expect(streamBody.events.some((e: { event_type: string }) => e.event_type === 'reg.bound')).toBe(true);
  } finally {
    srv.stop(true);
  }
});
