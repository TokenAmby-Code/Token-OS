import { expect, test } from 'bun:test';
import { SCHEMA_VERSION } from '@token-os/contracts';
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

// Seed a seat as present-AND-attested the way constructEstate itself would (pane
// on tmux + a reg.pane_created fact in the stream) — the true "already done" state.
function seedAttested(store: EventStore, tmux: FakeTmux, seat: string) {
  return Promise.resolve()
    .then(() => tmux.createSeat(seat))
    .then(() =>
      store.append({
        entity_type: 'seat',
        entity_id: seat,
        event_type: 'reg.pane_created',
        payload: { pane_state: 'live' },
        provenance: { source: 'observer', transport_receipt: null, emitter_version: SCHEMA_VERSION },
        occurred_at: new Date().toISOString(),
      }),
    );
}

// Rung 2: the typed constructor stands the canonical estate declaratively and
// idempotently. NO manual `tmux new-session` — the constructor IS the deliverable.

test('stands the full estate from empty — one pane_created per seat', async () => {
  const { store, d } = setup();
  const res = await d.constructEstate();

  expect(res.created).toEqual([...K12_ESTATE]);
  expect(res.existing).toEqual([]);
  expect(res.backfilled).toEqual([]);
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
  expect(res.backfilled).toEqual([]);
  expect(res.failed).toEqual([]);
  expect(store.count()).toBe(afterFirst); // zero new events on a full, attested estate
});

test('skips seats already present AND attested; creates only the rest', async () => {
  const { store, tmux, d } = setup();
  const pre = [K12_ESTATE[0]!, K12_ESTATE[5]!, K12_ESTATE[10]!];
  for (const seat of pre) await seedAttested(store, tmux, seat);
  const before = store.count();

  const res = await d.constructEstate();
  expect(res.existing.sort()).toEqual([...pre].sort());
  expect(res.created).toEqual(K12_ESTATE.filter((s) => !pre.includes(s)));
  expect(res.backfilled).toEqual([]);
  expect(res.failed).toEqual([]);
  // Only the absent seats appended a new event.
  expect(store.count()).toBe(before + (K12_ESTATE.length - pre.length));
});

test('backfills the torn state — pane present but its pane_created fact was lost', async () => {
  const { store, tmux, d } = setup();
  // Pane on tmux with NO event in the stream = a prior boot that committed
  // createSeat but not its append. Invisible to projections until repaired.
  const torn = [K12_ESTATE[1]!, K12_ESTATE[7]!];
  for (const seat of torn) await tmux.createSeat(seat);

  const res = await d.constructEstate();
  expect(res.backfilled.sort()).toEqual([...torn].sort());
  expect(res.existing).toEqual([]);
  expect(res.created).toEqual(K12_ESTATE.filter((s) => !torn.includes(s)));
  expect(res.failed).toEqual([]);

  // Repaired seats now carry their fact and appear on the board.
  const attested = new Set(
    store.readAll().filter((e) => e.event_type === 'reg.pane_created').map((e) => e.entity_id),
  );
  for (const seat of torn) expect(attested.has(seat)).toBe(true);
  expect(d.entities()).toHaveLength(K12_ESTATE.length);

  // Re-run is a full idempotent skip — the backfilled seats are now attested.
  const rerun = await d.constructEstate();
  expect(rerun.existing).toEqual([...K12_ESTATE]);
  expect(rerun.backfilled).toEqual([]);
  expect(rerun.created).toEqual([]);
});

test('a failing seat is isolated — lands in failed, others proceed, no throw', async () => {
  const { tmux, d } = setup();
  const bad = K12_ESTATE[3]!;
  tmux.failCreateSeat(bad);

  const res = await d.constructEstate();
  expect(res.failed).toEqual([bad]);
  expect(res.created).toEqual(K12_ESTATE.filter((s) => s !== bad));
  expect(res.backfilled).toEqual([]);

  // The failed seat has no pane and no fact — absent from the board; the rest stand.
  const board = d.entities();
  expect(board).toHaveLength(K12_ESTATE.length - 1);
  expect(board.some((r) => r.seat_id === bad)).toBe(false);

  // Estate stays healthy despite the isolated failure (no contradiction).
  const h = await d.health('k12-personal', BUILD);
  expect(h.ok).toBe(true);
  expect(h.open_contradictions).toBe(0);
});

test('bare unbound seats are healthy — ok, zero contradictions', async () => {
  const { d } = setup();
  await d.constructEstate();

  const h = await d.health('k12-personal', BUILD);
  expect(h.ok).toBe(true);
  expect(h.open_contradictions).toBe(0);
  expect(d.entities().every((r) => r.binding === 'unbound')).toBe(true);
});
