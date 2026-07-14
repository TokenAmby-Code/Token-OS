import { expect, test } from 'bun:test';
import { EventStore } from '../src/store.ts';
import { FakeTmux } from '../src/tmux.ts';
import { Daemon } from '../src/core.ts';
import { K12_ESTATE } from '../src/estate.ts';

function setup() {
  const store = new EventStore(`/tmp/k12estate-${crypto.randomUUID()}.sqlite`);
  const tmux = new FakeTmux();
  return { store, tmux, d: new Daemon(store, tmux) };
}

const BUILD = { version: '0', git_sha: 'x', bun: 'y' };

// Rung 2: the typed constructor stands the canonical estate declaratively and
// idempotently. NO manual `tmux new-session` — the constructor IS the deliverable.

test('stands the full estate from empty — one pane_created per seat', async () => {
  const { store, d } = setup();
  const res = await d.constructEstate();

  expect(res.created).toEqual([...K12_ESTATE]);
  expect(res.existing).toEqual([]);
  expect(res.failed).toEqual([]);

  const created = store.readAll().filter((e) => e.event_type === 'reg.pane_created');
  expect(created).toHaveLength(K12_ESTATE.length);

  // Every seat surfaces as an unbound row on the activity board.
  const board = d.entities();
  expect(board).toHaveLength(K12_ESTATE.length);
  expect(board.map((r) => r.seat_id).sort()).toEqual([...K12_ESTATE].sort());
  expect(board.every((r) => r.binding === 'unbound')).toBe(true);
});

test('idempotent re-run — second pass creates nothing, appends no events', async () => {
  const { store, d } = setup();
  await d.constructEstate();
  const afterFirst = store.count();

  const res = await d.constructEstate();
  expect(res.created).toEqual([]);
  expect(res.existing).toEqual([...K12_ESTATE]);
  expect(res.failed).toEqual([]);
  expect(store.count()).toBe(afterFirst); // zero new events on a full estate
});

test('creates only the missing seats when a subset already exists', async () => {
  const { tmux, d } = setup();
  const pre = [K12_ESTATE[0]!, K12_ESTATE[5]!, K12_ESTATE[10]!];
  for (const seat of pre) await tmux.createSeat(seat); // seeded out-of-band, not via constructEstate

  const res = await d.constructEstate();
  expect(res.existing.sort()).toEqual([...pre].sort());
  expect(res.created).toEqual(K12_ESTATE.filter((s) => !pre.includes(s)));
  expect(res.failed).toEqual([]);
});

test('bare unbound seats are healthy — ok, zero contradictions', async () => {
  const { d } = setup();
  await d.constructEstate();

  const h = await d.health('k12-personal', BUILD);
  expect(h.ok).toBe(true);
  expect(h.open_contradictions).toBe(0);
  expect(d.entities().every((r) => r.binding === 'unbound')).toBe(true);
});
