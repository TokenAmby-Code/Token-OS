import { expect, test } from 'bun:test';
import { SEND_PRESENCE_ACTIVITY_WINDOW_MS, type SendReceipt } from '@token-os/contracts';
import { EventStore } from '../src/store.ts';
import { FakeTmux } from '../src/tmux.ts';
import { Daemon } from '../src/core.ts';

// Spec §5 typing-guard: operator presence = a point-in-time READ of
// server-maintained client_activity, taken at BOTH decision points (admission +
// drain, rung 4). No keystroke hook, no shadow model.

function base() {
  const store = new EventStore(`/tmp/k12tg-${crypto.randomUUID()}.sqlite`);
  return store;
}

const SEAT = 'somnium:NE';

// A tmux fake whose presence answer is scripted PER CALL, so a test can prove
// the daemon reads presence at admission AND again at drain (and which read
// drove the gate). Extends FakeTmux to keep createSeat/sendToSeat behaviour.
class SequencedTmux extends FakeTmux {
  calls = 0;
  constructor(private seq: boolean[]) {
    super();
  }
  override async presentSeats(_windowMs: number, _nowMs?: number): Promise<Set<string>> {
    const present = this.seq[Math.min(this.calls, this.seq.length - 1)] ?? false;
    this.calls++;
    return new Set(present ? [SEAT] : []);
  }
}

test('present WITHIN window → gated; window echoed in the decision', async () => {
  const store = base();
  const tmux = new FakeTmux();
  const d = new Daemon(store, tmux);
  await d.launch({ seat_id: SEAT, schema_version: 1 });
  tmux.setPresence(SEAT, Date.now());
  const res = (await d.send({ target: SEAT, text: 'hi', schema_version: 1 })) as SendReceipt;
  expect(res.verdict).toBe('enqueued_gated');
  expect(res.activity_window_ms).toBe(SEND_PRESENCE_ACTIVITY_WINDOW_MS);
});

test('last activity OUTSIDE window → delivers (scrolling long ago does not gate)', async () => {
  const store = base();
  const tmux = new FakeTmux();
  const d = new Daemon(store, tmux);
  await d.launch({ seat_id: SEAT, schema_version: 1 });
  tmux.setPresence(SEAT, Date.now() - SEND_PRESENCE_ACTIVITY_WINDOW_MS - 5_000);
  const res = (await d.send({ target: SEAT, text: 'hi', schema_version: 1 })) as SendReceipt;
  expect(res.verdict).toBe('delivered');
});

test('present at ADMISSION → gated (defer this pass), even if idle by drain', async () => {
  const store = base();
  const tmux = new SequencedTmux([true, false]); // admission present, drain idle
  const d = new Daemon(store, tmux);
  await d.launch({ seat_id: SEAT, schema_version: 1 });
  const res = (await d.send({ target: SEAT, text: 'hi', schema_version: 1 })) as SendReceipt;
  expect(res.verdict).toBe('enqueued_gated'); // the admission read gated it
  expect(tmux.calls).toBe(1); // gated at admission → send returns without the drain read
});

test('idle at admission but present at DRAIN → gated (drain read is consulted)', async () => {
  const store = base();
  const tmux = new SequencedTmux([false, true]); // admission idle, became active by drain
  const d = new Daemon(store, tmux);
  await d.launch({ seat_id: SEAT, schema_version: 1 });
  const res = (await d.send({ target: SEAT, text: 'hi', schema_version: 1 })) as SendReceipt;
  expect(res.verdict).toBe('enqueued_gated'); // the drain read gated it
  expect(tmux.calls).toBe(2); // presence was read at admission AND drain
});

test('idle at BOTH admission and drain → delivered (both decision points read)', async () => {
  const store = base();
  const tmux = new SequencedTmux([false, false]);
  const d = new Daemon(store, tmux);
  await d.launch({ seat_id: SEAT, schema_version: 1 });
  const res = (await d.send({ target: SEAT, text: 'hi', schema_version: 1 })) as SendReceipt;
  expect(res.verdict).toBe('delivered');
  expect(tmux.calls).toBe(2); // read at admission AND drain before delivering
});
