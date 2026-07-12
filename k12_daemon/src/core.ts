// Daemon core — the domain logic behind the API (spec §4, §5, §6).
//
// Single writer: every mutating path runs under one async mutex so seq order
// and read-modify-write sequences never interleave. Truth is the event stream;
// this class only APPENDS facts and READS projections — it never mutates a
// projection directly.

import {
  SCHEMA_VERSION,
  SEND_PRESENCE_ACTIVITY_WINDOW_MS,
  type DeliveryVerdict,
  type EventInput,
  type LaunchRequest,
  type LaunchResponse,
  type OpenContradiction,
  type Provenance,
  type ProvenanceSource,
  type ReconcileResponse,
  type SendReceipt,
  type SendRefusal,
  type SendRefusalReason,
  type SendRequest,
  type SendResolution,
} from '@token-os/contracts';
import { EventStore } from './store.ts';
import { buildProjections, type Projections } from './projections.ts';
import type { TmuxControlPlane } from './tmux.ts';

// Reg-audit attestation set DEFINED SO FAR (door step 1). The refusal machinery
// is day-one; later doors grow this list as they add witnesses (rank, commander,
// singleton, dispatch_target become required when their witnesses walk in).
export const DOOR1_REQUIRED_ATTESTATIONS = ['identity', 'persona', 'tint'] as const;

type Now = () => string;

export class Daemon {
  private mutex: Promise<unknown> = Promise.resolve();

  constructor(
    private store: EventStore,
    private tmux: TmuxControlPlane,
    private now: Now = () => new Date().toISOString(),
  ) {}

  /** Serialize a mutating op — the single-writer discipline. */
  private locked<T>(fn: () => Promise<T>): Promise<T> {
    const run = this.mutex.then(fn, fn);
    this.mutex = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
  }

  private prov(source: ProvenanceSource, transportReceipt: string | null): Provenance {
    return { source, transport_receipt: transportReceipt, emitter_version: SCHEMA_VERSION };
  }

  private projections(): Projections {
    return buildProjections(this.store.readAll());
  }

  // ── /launch — reg-audit SCAFFOLD (spec §4) ─────────────────────────────────
  // Creates a seat, then refuses handover unless every attestation-defined-so-far
  // is present. Binding is ATOMIC: identity + persona + tint commit as ONE
  // `reg.bound` event carrying the full tuple — half-bound is unspellable.
  launch(req: LaunchRequest, transportReceipt: string | null = null): Promise<LaunchResponse> {
    return this.locked(async () => {
      const occurred_at = this.now();
      const prov = this.prov('wrapper', transportReceipt);

      // SCHEMA-level invariant (the instances.tmux_pane lesson): pin exact version.
      if (req.schema_version !== SCHEMA_VERSION) {
        return {
          ok: false,
          seat_id: req.seat_id,
          handover: false,
          missing_attestations: [],
          reason: `schema_version_mismatch: daemon pins ${SCHEMA_VERSION}, request sent ${req.schema_version}`,
        };
      }

      // Create the seat (real pane below the membrane) + record the fact.
      await this.tmux.createSeat(req.seat_id);
      this.store.append({
        entity_type: 'seat',
        entity_id: req.seat_id,
        event_type: 'reg.pane_created',
        payload: { pane_state: 'live' },
        provenance: prov,
        occurred_at,
      });

      // Reg-audit: every attestation-defined-so-far must be present.
      const missing = DOOR1_REQUIRED_ATTESTATIONS.filter((a) => !req[a]);
      if (missing.length > 0) {
        // Stop-the-line: seat exists (freelist), but handover is refused. No
        // `bound` event — a half-launch never leaves a half-bound seat.
        return {
          ok: false,
          seat_id: req.seat_id,
          handover: false,
          missing_attestations: [...missing],
          reason: `reg-audit refused handover: missing ${missing.join(', ')}`,
        };
      }

      // Atomic bind: the full tuple in ONE event.
      this.store.append({
        entity_type: 'seat',
        entity_id: req.seat_id,
        event_type: 'reg.bound',
        payload: {
          wrapper_id: null,
          instance_id: req.identity,
          persona: req.persona,
          tint: req.tint,
          rank: req.rank ?? null,
          commander: req.commander ?? null,
        },
        provenance: prov,
        occurred_at,
      });

      return { ok: true, seat_id: req.seat_id, handover: true, missing_attestations: [], reason: null };
    });
  }

  // ── /send — the ONE chokepoint (spec §5) ───────────────────────────────────
  // enqueue-by-default; unresolved targets REFUSED at admission (never gated —
  // the #699 class is unrepresentable); typed gate true-cause; the receipt
  // carries the SAME resolution the send used (never re-derived).
  send(req: SendRequest, transportReceipt: string | null = null): Promise<SendReceipt | SendRefusal> {
    return this.locked(async () => {
      if (req.schema_version !== SCHEMA_VERSION) {
        return this.refuse('schema_version_mismatch', req.target);
      }

      // Resolve target -> canonical seat + the seq it resolved against. Prefer a
      // current binding; fall back to a bare live seat.
      const proj = this.projections();
      const resolution = this.resolveTarget(req.target, proj);
      if (!resolution) return this.refuse('pane_unresolved', req.target);
      // Pane must be live at admission (unresolved/dead never admitted).
      const board = proj.activityBoard.find((r) => r.seat_id === resolution.seat_id);
      if (board && board.pane === 'dead') return this.refuse('pane_dead', req.target);

      const occurred_at = this.now();
      const sendId = crypto.randomUUID();

      // Admit: enqueue with the resolution frozen into the queue item.
      this.store.append({
        entity_type: 'send',
        entity_id: sendId,
        event_type: 'act.send_enqueued',
        payload: { target: resolution.seat_id, resolved_seq: resolution.bound_seq, text_len: req.text.length },
        provenance: this.prov('wrapper', transportReceipt),
        occurred_at,
      });

      // Typed-cause gate. Presence is a point-in-time READ of server-maintained
      // client_activity — no shadow state, no keystroke hook. Emitting the gate
      // records the DECISION (carrying its window evidence); raw presence never
      // enters the stream. A gated send STAYS enqueued for a later drain.
      const gate = (): SendReceipt => {
        this.store.append({
          entity_type: 'send',
          entity_id: sendId,
          event_type: 'act.send_gated',
          payload: {
            target: resolution.seat_id,
            reason: 'typing_guard',
            activity_window_ms: SEND_PRESENCE_ACTIVITY_WINDOW_MS,
            resolved_seq: resolution.bound_seq,
          },
          provenance: this.prov('observer', transportReceipt),
          occurred_at: this.now(),
        });
        return this.receipt('enqueued_gated', resolution, sendId, 'typing_guard', SEND_PRESENCE_ACTIVITY_WINDOW_MS, null);
      };

      // Presence read at ADMISSION (the enqueue-time snapshot, spec §5 rung 4):
      // operator active ⇒ defer this pass (gate now, deliver on a later drain).
      const presentAtAdmission = await this.tmux.presentSeats(SEND_PRESENCE_ACTIVITY_WINDOW_MS);
      if (presentAtAdmission.has(resolution.seat_id)) return gate();

      // Presence read at DRAIN (the delivery instant): re-read fresh — the
      // operator may have become active between admission and drain.
      const presentAtDrain = await this.tmux.presentSeats(SEND_PRESENCE_ACTIVITY_WINDOW_MS);
      if (presentAtDrain.has(resolution.seat_id)) return gate();

      // Operator idle at BOTH decision points → deliver (canonical in, %id internal).
      const result = await this.tmux.sendToSeat(resolution.seat_id, req.text);
      const verdict: DeliveryVerdict = result.delivered ? 'delivered' : 'failed_none_delivered';
      if (result.delivered) {
        this.store.append({
          entity_type: 'send',
          entity_id: sendId,
          event_type: 'act.send_delivered',
          payload: { target: resolution.seat_id, bytes: result.bytes, resolved_seq: resolution.bound_seq },
          provenance: this.prov('observer', transportReceipt),
          occurred_at: this.now(),
        });
      }
      return this.receipt(verdict, resolution, sendId, null, null, result.delivered ? result.bytes : 0);
    });
  }

  private resolveTarget(target: string, proj: Projections): SendResolution | null {
    // A bound seat, matched by seat id or by the instance it carries.
    const binding = proj.currentBindings.find((b) => b.seat_id === target || b.instance_id === target);
    if (binding) return { target, seat_id: binding.seat_id, bound_seq: binding.bound_seq };
    // A bare live seat (no binding) — resolves against the seat's board row.
    // The predicate matched seat_id === target, so the seat id IS target here.
    const bare = proj.activityBoard.find((r) => r.seat_id === target && r.binding === 'unbound' && r.pane !== 'dead');
    if (bare) return { target, seat_id: target, bound_seq: 0 };
    return null;
  }

  private refuse(reason: SendRefusalReason, target: string): SendRefusal {
    console.error(JSON.stringify({ level: 'error', event: 'send_refused', reason, target }));
    return { ok: false, refused: true, reason, target };
  }

  private receipt(
    verdict: DeliveryVerdict,
    resolution: SendResolution,
    sendId: string,
    gate: 'typing_guard' | null,
    window: number | null,
    bytes: number | null,
  ): SendReceipt {
    return {
      verdict,
      resolution,
      gate_reason: gate,
      activity_window_ms: window,
      bytes_delivered: bytes,
      send_seq: this.store.readByEntity(sendId).at(-1)?.seq ?? -1,
    };
  }

  // ── /reconcile — replay + contradiction observation (spec §6) ───────────────
  // Pure replay rebuild; observes tmux and emits contradiction_flagged for
  // discrepancies (NEVER a synthesized lifecycle event). Bring-up mode: every
  // open contradiction is p0 — fail loud, ok=false.
  reconcile(transportReceipt: string | null = null): Promise<ReconcileResponse> {
    return this.locked(async () => {
      const events = this.store.readAll();
      const t0 = performance.now();
      const proj = buildProjections(events);
      const replay_ms = performance.now() - t0;

      const observed = await this.tmux.listSeats();
      const observedPane = new Map(observed.map((o) => [o.seat_id, o.pane]));

      const alreadyOpen = new Set(proj.openContradictions.map((c) => `${c.entity_id}:${c.kind}`));
      const newContradictions: OpenContradiction[] = [];

      const flag = (
        entity_id: string,
        kind: string,
        missing: string | null,
        detail: string,
      ): void => {
        if (alreadyOpen.has(`${entity_id}:${kind}`)) return; // already flagged & still open
        const occurred_at = this.now();
        const rec = this.store.append({
          entity_type: 'seat',
          entity_id,
          event_type: 'reg.contradiction_flagged',
          payload: { kind, missing_attestation: missing, detail },
          provenance: this.prov('observer', transportReceipt),
          occurred_at,
        });
        console.error(
          JSON.stringify({ level: 'error', event: 'contradiction_flagged', p0: true, entity_id, kind, missing_attestation: missing, detail }),
        );
        newContradictions.push({
          seq: rec.seq,
          entity_type: 'seat',
          entity_id,
          kind,
          missing_attestation: missing,
          detail,
          occurred_at,
        });
      };

      // Bound seat whose pane died out-of-band (the retire chain never ran).
      for (const b of proj.currentBindings) {
        const pane = observedPane.get(b.seat_id);
        if (pane === 'dead' || pane === undefined) {
          flag(
            b.seat_id,
            'bound_pane_dead',
            'seat_cleared',
            `seat is bound (bound_seq=${b.bound_seq}) but tmux pane is ${pane ?? 'absent'} — no teardown/reap/clear attested`,
          );
        }
      }
      // Retired instance whose pane is still live (retire-with-live-process).
      for (const row of proj.activityBoard) {
        if (row.seat_id === null) continue; // board row without a seat can't be a seat-liveness contradiction
        if (row.activity === 'retired' && observedPane.get(row.seat_id) === 'live') {
          flag(row.seat_id, 'retired_pane_live', 'process_reaped', `activity=retired but tmux pane is live`);
        }
      }

      // Recompute open set over the freshly-appended stream.
      const openContradictions = buildProjections(this.store.readAll()).openContradictions;
      const p0 = openContradictions.length > 0;

      return {
        ok: !p0,
        replayed_events: events.length,
        replay_ms,
        bindings: proj.currentBindings.length,
        freelist: proj.freelist.length,
        instances: proj.activityBoard.length,
        new_contradictions: newContradictions,
        open_contradictions: openContradictions,
        p0,
      };
    });
  }

  // ── Read models (spec §7 rung 6) ───────────────────────────────────────────
  entities() {
    return this.projections().activityBoard;
  }

  entityEvents(entityId: string) {
    return this.store.readByEntity(entityId);
  }

  async health(machine: string, build: { version: string; git_sha: string; bun: string }) {
    const proj = this.projections();
    const tmux_reachable = (await this.tmux.version()) !== null;
    const open = proj.openContradictions.length;
    return {
      ok: open === 0, // bring-up mode: any open contradiction ⇒ not ok
      service: 'k12_daemon' as const,
      schema_version: SCHEMA_VERSION,
      version: build.version,
      git_sha: build.git_sha,
      bun: build.bun,
      machine,
      events: this.store.count(),
      open_contradictions: open,
      tmux_reachable,
    };
  }
}

export type { EventInput };
