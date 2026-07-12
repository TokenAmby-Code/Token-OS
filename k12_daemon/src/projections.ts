// Projections (spec §10) — the three day-one read models, ALL rebuilt purely by
// replay. Nobody writes them; they are a fold over the event stream. Open
// contradictions get NO table — "currently contradicted" is a stream filter.
//
// Payload conventions (dumb facts; the fold denormalizes on the read side):
//   reg.pane_created   (seat)     payload.pane_state? = 'live' | 'empty'   (default 'live')
//   reg.bound          (seat)     payload {wrapper_id, instance_id, persona, tint}  — bound_seq = event.seq
//   reg.seat_cleared   (seat)     clears the binding (pane axis untouched)
//   reg.teardown_started(seat)    pane → 'dead' (teardown kills the pane)
//   reg.process_reaped (seat)     pane → 'dead'
//   act.prompt_submitted(instance) activity → 'working'
//   act.stop_reported  (instance) activity → 'stopped'
//   reg.retired        (instance) activity → 'retired'
//   act.send_enqueued  (send)     payload.target = seat canonical id → queue_depth +1
//   act.send_delivered (send)     payload.target → queue_depth -1   (gated is a no-op: still enqueued)
//   reg.contradiction_flagged     open unless a later event exists on the same entity_id

import { PANE_STATES } from '@token-os/contracts';
import type {
  ActivityBoardRow,
  ActivityState,
  CurrentBinding,
  EventRecord,
  FreelistEntry,
  OpenContradiction,
  PaneState,
} from '@token-os/contracts';

export type Projections = {
  currentBindings: CurrentBinding[];
  freelist: FreelistEntry[];
  activityBoard: ActivityBoardRow[];
  openContradictions: OpenContradiction[];
};

function str(v: unknown): string | null {
  return typeof v === 'string' && v.length > 0 ? v : null;
}

// Only accept a declared PaneState; an unexpected/typo'd payload string must not
// slip through as a bogus state and corrupt the freelist/board reads.
function paneState(v: unknown): PaneState {
  const s = str(v);
  return s && (PANE_STATES as readonly string[]).includes(s) ? (s as PaneState) : 'live';
}

export function buildProjections(events: EventRecord[]): Projections {
  const paneBySeat = new Map<string, PaneState>();
  const bindingBySeat = new Map<string, CurrentBinding>();
  const activityByInstance = new Map<string, ActivityState>();
  const queueByTarget = new Map<string, number>();
  // entity_id -> highest seq seen, to supersede stale contradiction flags.
  const lastSeqByEntity = new Map<string, number>();
  const contradictions: OpenContradiction[] = [];

  for (const e of events) {
    lastSeqByEntity.set(e.entity_id, e.seq);
    switch (e.event_type) {
      case 'reg.pane_created':
        paneBySeat.set(e.entity_id, paneState(e.payload.pane_state));
        break;
      case 'reg.teardown_started':
      case 'reg.process_reaped':
        if (paneBySeat.has(e.entity_id)) paneBySeat.set(e.entity_id, 'dead');
        break;
      case 'reg.bound':
        bindingBySeat.set(e.entity_id, {
          seat_id: e.entity_id,
          wrapper_id: str(e.payload.wrapper_id),
          instance_id: str(e.payload.instance_id),
          persona: str(e.payload.persona),
          tint: str(e.payload.tint),
          bound_seq: e.seq,
        });
        break;
      case 'reg.seat_cleared':
        bindingBySeat.delete(e.entity_id);
        break;
      case 'act.prompt_submitted':
        activityByInstance.set(e.entity_id, 'working');
        break;
      case 'act.stop_reported':
        activityByInstance.set(e.entity_id, 'stopped');
        break;
      case 'reg.retired':
        activityByInstance.set(e.entity_id, 'retired');
        break;
      case 'act.send_enqueued': {
        const t = str(e.payload.target);
        if (t) queueByTarget.set(t, (queueByTarget.get(t) ?? 0) + 1);
        break;
      }
      case 'act.send_delivered': {
        const t = str(e.payload.target);
        if (t) queueByTarget.set(t, Math.max(0, (queueByTarget.get(t) ?? 0) - 1));
        break;
      }
      case 'reg.contradiction_flagged':
        contradictions.push({
          seq: e.seq,
          entity_type: e.entity_type,
          entity_id: e.entity_id,
          kind: str(e.payload.kind) ?? 'unknown',
          missing_attestation: str(e.payload.missing_attestation),
          detail: str(e.payload.detail),
          occurred_at: e.occurred_at,
        });
        break;
      default:
        break; // launch-chain rungs, sends' gated, dedupe — no projection effect here
    }
  }

  const currentBindings = [...bindingBySeat.values()];

  const freelist: FreelistEntry[] = [];
  for (const [seat, pane] of paneBySeat) {
    if ((pane === 'live' || pane === 'empty') && !bindingBySeat.has(seat)) {
      freelist.push({ seat_id: seat, pane_state: pane });
    }
  }

  const activityBoard: ActivityBoardRow[] = [];
  for (const [seat, pane] of paneBySeat) {
    const binding = bindingBySeat.get(seat);
    const instanceId = binding?.instance_id ?? null;
    const entity_id = instanceId ?? seat;
    activityBoard.push({
      entity_id,
      entity_type: instanceId ? 'instance' : 'seat',
      seat_id: seat,
      pane,
      binding: binding ? 'bound' : 'unbound',
      activity: instanceId ? activityByInstance.get(instanceId) ?? 'idle' : 'idle',
      // Sends target the seat's canonical id.
      queue_depth: queueByTarget.get(seat) ?? 0,
      persona: binding?.persona ?? null,
      tint: binding?.tint ?? null,
    });
  }

  // A contradiction is OPEN unless a later event moved its entity (re-observe to
  // re-flag). Pure stream filter — no resolve event, no fourth table.
  const openContradictions = contradictions.filter(
    (c) => (lastSeqByEntity.get(c.entity_id) ?? c.seq) <= c.seq,
  );

  return { currentBindings, freelist, activityBoard, openContradictions };
}
