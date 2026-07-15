// Daemon core — the domain logic behind the API (spec §4, §5, §6).
//
// Single writer: every mutating path runs under one async mutex so seq order
// and read-modify-write sequences never interleave. Truth is the event stream;
// this class only APPENDS facts and READS projections — it never mutates a
// projection directly.

import {
  SCHEMA_VERSION,
  SEND_PRESENCE_ACTIVITY_WINDOW_MS,
  type ActivityBoardRow,
  type CloseRequest,
  type CloseResponse,
  type CurrentBinding,
  type DeliveryVerdict,
  type EventInput,
  type EventRecord,
  type Health,
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
  type StopAutoCloseOutcome,
  type StopReceipt,
  type StopRefusal,
  type StopRefusalReason,
  type StopRequest,
  type SubscribeRequest,
  type SubscribeResponse,
} from '@token-os/contracts';
import { EventStore } from './store.ts';
import { findTmuxId } from './ids.ts';
import { buildProjections, type Projections } from './projections.ts';
import { K12_ESTATE } from './estate.ts';
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

  // ── constructEstate — boot-time idempotent ensure (k12 estate, rung 2) ──────
  // Stands the canonical persistent estate (src/estate.ts) declaratively. NOT an
  // endpoint or CLI — the seed vocab/endpoint set is closed; this is a boot
  // ensure. Runs under the single-writer mutex so it can't interleave with a
  // concurrent launch/send. Idempotent: a re-run over a fully-present-and-attested
  // estate creates nothing and appends zero events. Each fresh seat records ONE
  // bare `reg.pane_created` (unbound) — it lands in freelist + activity_board and
  // triggers NO contradiction (reconcile only flags bound-dead / retired-live).
  //
  // Buckets: `created` = pane made + event written this run; `backfilled` = pane
  // already there but its event was missing (repaired, no new pane); `existing` =
  // present AND attested (skipped); `failed` = a seat that threw (logged, others
  // continue). Truth is the stream, so a pane with no `reg.pane_created` is
  // invisible to every projection — backfill closes that gap.
  constructEstate(): Promise<{ created: string[]; existing: string[]; backfilled: string[]; failed: string[] }> {
    return this.locked(async () => {
      // Live OR dead counts as present — a dead seat's session still exists, so
      // createSeat would throw on a duplicate session name. Skip creation either
      // way; attestation is tracked separately below.
      const present = new Set((await this.tmux.listSeats()).map((o) => o.seat_id));
      // Seats that already carry a `reg.pane_created` fact. A prior boot could
      // have torn (createSeat committed, its append did not) — the pane persists
      // but the fact was lost. Presence WITHOUT attestation is that torn state.
      const attested = new Set(
        this.store.readAll().filter((e) => e.event_type === 'reg.pane_created').map((e) => e.entity_id),
      );
      const created: string[] = [];
      const existing: string[] = [];
      const backfilled: string[] = [];
      const failed: string[] = [];

      const recordCreated = (seat: string): void => {
        this.store.append({
          entity_type: 'seat',
          entity_id: seat,
          event_type: 'reg.pane_created',
          payload: { pane_state: 'live' },
          provenance: this.prov('observer', null),
          occurred_at: this.now(),
        });
      };

      for (const seat of K12_ESTATE) {
        try {
          if (present.has(seat)) {
            if (attested.has(seat)) {
              existing.push(seat); // present AND attested — nothing to do
            } else {
              // Repair the torn state: backfill the lost fact. No second tmux
              // session is spawned, so idempotency and the pane both hold.
              recordCreated(seat);
              backfilled.push(seat);
            }
          } else {
            await this.tmux.createSeat(seat);
            recordCreated(seat);
            created.push(seat);
          }
        } catch (err) {
          // A single seat failing must not sink the estate or crash boot — health
          // stays up. Fail loud, then carry on to the next seat.
          failed.push(seat);
          console.error(
            JSON.stringify({ level: 'error', event: 'estate_seat_failed', seat, detail: String(err) }),
          );
        }
      }

      return { created, existing, backfilled, failed };
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
      const verdict: DeliveryVerdict = result.verdict;
      if (verdict === 'delivered') {
        this.store.append({
          entity_type: 'send',
          entity_id: sendId,
          event_type: 'act.send_delivered',
          payload: { target: resolution.seat_id, bytes: result.bytes, resolved_seq: resolution.bound_seq },
          provenance: this.prov('observer', transportReceipt),
          occurred_at: this.now(),
        });
      }
      // partial_delivered = text inserted but not submitted → stays enqueued (like a
      // gate); the receipt still carries the partial verdict + its byte evidence
      // (contract requires non-null bytes for partial). Only a full delivery dequeues.
      return this.receipt(verdict, resolution, sendId, null, null, verdict === 'failed_none_delivered' ? 0 : result.bytes);
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
    // The membrane also covers logs: a client may hand us a raw `%5`; redact it
    // in the log line while returning the caller's original target unchanged.
    const loggedTarget = findTmuxId(target) ? '<redacted-tmux-id>' : target;
    console.error(JSON.stringify({ level: 'error', event: 'send_refused', reason, target: loggedTarget }));
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

  // ── /close — the generic "close this instance" system (rung 3) ──────────────
  // Reaps the agent process and returns the estate seat to the freelist. Terminal
  // chain (retired + process_reaped + seat_cleared) is atomic and only written
  // AFTER the process is confirmed reaped — a retire-with-live-process is
  // unspellable (spec §4). No silent no-op: an unbound target or a failed reap
  // refuses loud and changes nothing (the mac mark-for-close-noop class, killed).
  close(req: CloseRequest, transportReceipt: string | null = null): Promise<CloseResponse> {
    return this.locked(async () => {
      if (req.schema_version !== SCHEMA_VERSION) {
        return {
          ok: false,
          target: req.target,
          seat_id: null,
          instance_id: null,
          closed: false,
          reason: `schema_version_mismatch: daemon pins ${SCHEMA_VERSION}, request sent ${req.schema_version}`,
        };
      }

      const proj = this.projections();
      const binding = proj.currentBindings.find((b) => b.seat_id === req.target || b.instance_id === req.target);
      if (!binding) {
        // Refuse loud — closing a non-bound target is a no-op the caller must see,
        // never a silent success (the mac /mark-for-close returned ok on nothing).
        return {
          ok: false,
          target: req.target,
          seat_id: null,
          instance_id: null,
          closed: false,
          reason: 'no_binding: target resolves to no current binding (already free or never bound)',
        };
      }

      // Reap FIRST; attest only on a confirmed kill (executeClose is the SAME path
      // the reflexive auto-close fires — one close mechanism, no bespoke variant).
      const closed = await this.executeClose(binding, transportReceipt);
      if (!closed) {
        return {
          ok: false,
          target: req.target,
          seat_id: binding.seat_id,
          instance_id: binding.instance_id,
          closed: false,
          reason: 'reap_failed: agent process could not be reaped; seat left bound (fail-loud, no half-close)',
        };
      }
      return { ok: true, target: req.target, seat_id: binding.seat_id, instance_id: binding.instance_id, closed: true, reason: null };
    });
  }

  // The generic close mechanism, shared by /close and the reflexive auto-close.
  // Reap-first, attest-after: respawn-pane -k keeps the estate pane (bare shell)
  // so the seat survives and returns to the freelist. On a confirmed reap, ONE
  // transaction writes retired + process_reaped + seat_cleared (seat_cleared frees
  // the binding — the ledger PROJECTION follows, no separate ledger to leak).
  // Returns false (nothing written) if the process could not be reaped, so a
  // retire-with-live-process is unspellable. Caller holds the single-writer mutex.
  private async executeClose(binding: CurrentBinding, transportReceipt: string | null): Promise<boolean> {
    const reaped = await this.tmux.reapSeat(binding.seat_id);
    if (!reaped) return false;
    const occurred_at = this.now();
    const prov = this.prov('observer', transportReceipt);
    const inputs: EventInput[] = [];
    if (binding.instance_id) {
      inputs.push({ entity_type: 'instance', entity_id: binding.instance_id, event_type: 'reg.retired', payload: {}, provenance: prov, occurred_at });
    }
    inputs.push({ entity_type: 'seat', entity_id: binding.seat_id, event_type: 'reg.process_reaped', payload: { instance_id: binding.instance_id }, provenance: prov, occurred_at });
    inputs.push({ entity_type: 'seat', entity_id: binding.seat_id, event_type: 'reg.seat_cleared', payload: {}, provenance: prov, occurred_at });
    this.store.appendAll(inputs);
    return true;
  }

  // ── /subscribe — the generic stop-hook subscription system (rung 3) ─────────
  // Records a close-on-next-stop subscription. BOUND-KEYED: refuses unless the
  // instance is currently bound, so an orphan/never-bound id can never hold a
  // subscription (the 77f7cfb4 re-firing class is structurally dead). Composing
  // this with /stop yields `final message → auto-close on next stop-hook`.
  subscribe(req: SubscribeRequest, transportReceipt: string | null = null): Promise<SubscribeResponse> {
    return this.locked(async () => {
      if (req.schema_version !== SCHEMA_VERSION) {
        return {
          ok: false,
          instance_id: req.instance_id,
          action: null,
          subscribed: false,
          reason: `schema_version_mismatch: daemon pins ${SCHEMA_VERSION}, request sent ${req.schema_version}`,
        };
      }
      const proj = this.projections();
      if (!proj.currentBindings.some((b) => b.instance_id === req.instance_id)) {
        return {
          ok: false,
          instance_id: req.instance_id,
          action: null,
          subscribed: false,
          reason: 'not_bound: subscriptions are bound-keyed — an unbound/never-bound instance cannot subscribe',
        };
      }
      this.store.append({
        entity_type: 'instance',
        entity_id: req.instance_id,
        event_type: 'reg.stop_subscribed',
        payload: { action: req.action },
        provenance: this.prov('wrapper', transportReceipt),
        occurred_at: this.now(),
      });
      return { ok: true, instance_id: req.instance_id, action: req.action, subscribed: true, reason: null };
    });
  }

  // ── /stop — the stop-hook's door (rung 3) ───────────────────────────────────
  // Three honest outcomes, no blind swallow: record a fresh stop (bound + live),
  // dedupe a repeat/late stop (act.receipt_deduped), or REFUSE a ghost — a stop for
  // an id that never walked through /launch. The ghost is refused at admission, so
  // nothing is recorded: no phantom row, no re-firing subscription (the 77f7cfb4
  // class is structurally dead). The stop-hook is a REAL but UNTRUSTED witness.
  stop(req: StopRequest, transportReceipt: string | null = null): Promise<StopReceipt | StopRefusal> {
    return this.locked(async () => {
      if (req.schema_version !== SCHEMA_VERSION) {
        return this.refuseStop('schema_version_mismatch', req.instance_id);
      }

      const proj = this.projections();
      // Ghost preclusion: never bound ⇒ never existed ⇒ refuse loud.
      if (!proj.everBoundInstances.has(req.instance_id)) {
        return this.refuseStop('no_such_instance', req.instance_id);
      }

      const activity = proj.activityByInstance.get(req.instance_id) ?? null;
      const stillBound = proj.currentBindings.some((b) => b.instance_id === req.instance_id);
      // Dedupe: already stopped/retired, or already closed (no longer bound) →
      // idempotent, but RECORDED as receipt_deduped (never a blind swallow).
      if (activity === 'stopped' || activity === 'retired' || !stillBound) {
        this.store.append({
          entity_type: 'instance',
          entity_id: req.instance_id,
          event_type: 'act.receipt_deduped',
          payload: { of: 'stop_reported', reason: activity ?? 'unbound' },
          provenance: this.prov('observer', transportReceipt),
          occurred_at: this.now(),
        });
        return { ok: true, instance_id: req.instance_id, recorded: false, deduped: true, activity, auto_close: 'none' };
      }

      // Fresh stop for a live, bound instance → record it (activity → stopped).
      this.store.append({
        entity_type: 'instance',
        entity_id: req.instance_id,
        event_type: 'act.stop_reported',
        payload: {},
        provenance: this.prov('hook', transportReceipt),
        occurred_at: this.now(),
      });

      // Reflexive auto-close: an OPEN close-on-stop subscription fires now (the stop
      // we just recorded satiates it). `proj` is the pre-stop read, so the binding
      // is still present; executeClose is the SAME mechanism as /close.
      let auto_close: StopAutoCloseOutcome = 'none';
      if (proj.openStopSubscriptions.has(req.instance_id)) {
        const binding = proj.currentBindings.find((b) => b.instance_id === req.instance_id);
        if (binding) {
          const closed = await this.executeClose(binding, transportReceipt);
          auto_close = closed ? 'fired' : 'reap_failed';
          if (!closed) {
            // Loud, not silent: the instance stays stopped+bound (visible), never a
            // quiet leak. Reconcile catches any lingering retire-with-live-process.
            console.error(
              JSON.stringify({ level: 'error', event: 'auto_close_reap_failed', instance_id: req.instance_id, seat_id: binding.seat_id }),
            );
          }
        }
      }
      return { ok: true, instance_id: req.instance_id, recorded: true, deduped: false, activity: 'stopped', auto_close };
    });
  }

  private refuseStop(reason: StopRefusalReason, instanceId: string): StopRefusal {
    const logged = findTmuxId(instanceId) ? '<redacted-tmux-id>' : instanceId;
    console.error(JSON.stringify({ level: 'error', event: 'stop_refused', reason, instance_id: logged }));
    return { ok: false, refused: true, reason, instance_id: instanceId };
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
  entities(): ActivityBoardRow[] {
    return this.projections().activityBoard;
  }

  entityEvents(entityId: string): EventRecord[] {
    return this.store.readByEntity(entityId);
  }

  async health(machine: string, build: { version: string; git_sha: string; bun: string }): Promise<Health> {
    const proj = this.projections();
    // Probe the daemon's OWN tmux socket (start-server + list-panes), not just
    // `tmux -V` — a responding binary over a dead socket must not read healthy.
    const tmux_reachable = await this.tmux.reachable();
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
