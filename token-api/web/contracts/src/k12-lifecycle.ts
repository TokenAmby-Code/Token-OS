// K12 daemon lifecycle vocabulary (`@token-os/contracts`).
//
// The single shared lifecycle vocabulary for the tmuxctld-successor daemon,
// ruled in [[k12-daemon-spec]] §2–§7. The daemon speaks this natively;
// token-api conforms at its edges only; the cockpit wins by convergence.
//
// Design invariants baked into these types (spec §3):
//   - ONE stream, typed domains within it (`reg.*` / `act.*`) — the domain is a
//     prefix on `event_type`, NOT a second store. reg.* = registration/binding
//     lifecycle + daemon observations; act.* = agent behavior + send activity.
//   - The seed 16 event types are CLOSED day-one — no additions without a
//     `schema_version` bump landed daemon+cockpit together.
//   - The single `status` field is DEAD. Orthogonal axes only.
//   - `schema_version` is a single integer; the daemon pins it exactly.
//
// This is a TS-source package: consumers compile it directly, no build step.

import { z } from 'zod';

// The daemon pins this exact integer. Additive vocabulary = minor bump (cockpit
// conforms lazily); breaking changes land daemon+cockpit in ONE PR. Old events
// replay under the vocabulary that wrote them via `provenance.emitter_version`.
export const SCHEMA_VERSION = 1;

// ── Entities ────────────────────────────────────────────────────────────────
// The four entity kinds the daemon tracks. `send` is a first-class entity: a
// send has its own lifecycle (enqueued → gated? → delivered) and trust surface.
export const ENTITY_TYPES = ['seat', 'wrapper', 'instance', 'send'] as const;
export type EntityType = (typeof ENTITY_TYPES)[number];
export const EntityTypeSchema = z.enum(ENTITY_TYPES);

// ── Event vocabulary — the seed 16, domain-partitioned (spec §3) ─────────────
// Domain is encoded as a prefix on the qualified event_type. There is ONE
// stream; the prefix enables per-domain projections/retention later without a
// parallel behavior stream (rejected explicitly as a split-brain factory).
export const EVENT_DOMAINS = ['reg', 'act'] as const;
export type EventDomain = (typeof EVENT_DOMAINS)[number];

// reg.* — registration & binding lifecycle, plus daemon observations about it.
export const REG_EVENT_NAMES = [
  'dispatch_requested',
  'pane_created',
  'wrapper_started',
  'session_started',
  'bound',
  'contradiction_flagged',
  'teardown_started',
  'process_reaped',
  'retired',
  'seat_cleared',
] as const;

// act.* — agent behavior (feeds the `activity` axis) + send activity.
// NOTE (flagged for Custodes review): the spec names only `prompt_submitted`
// and `stop_reported` as act.* explicitly. The send-lifecycle events and
// `receipt_deduped` are placed in act.* here as a defensible reading (send =
// operational activity directed at a pane; receipt_deduped rides stop_reported).
// This is a contracts-only prefix decision, trivially re-partitioned if ruled
// otherwise — it never changes the storage (one stream) or the seed-16 set.
export const ACT_EVENT_NAMES = [
  'prompt_submitted',
  'stop_reported',
  'send_enqueued',
  'send_gated',
  'send_delivered',
  'receipt_deduped',
] as const;

// The qualified event_type union (`<domain>.<name>`), enumerated literally so
// the type stays a narrow literal union and stays greppable. 10 reg + 6 act = 16.
export const EVENT_TYPES = [
  'reg.dispatch_requested',
  'reg.pane_created',
  'reg.wrapper_started',
  'reg.session_started',
  'reg.bound',
  'reg.contradiction_flagged',
  'reg.teardown_started',
  'reg.process_reaped',
  'reg.retired',
  'reg.seat_cleared',
  'act.prompt_submitted',
  'act.stop_reported',
  'act.send_enqueued',
  'act.send_gated',
  'act.send_delivered',
  'act.receipt_deduped',
] as const;
export type EventType = (typeof EVENT_TYPES)[number];
export const EventTypeSchema = z.enum(EVENT_TYPES);

export function eventDomain(eventType: EventType): EventDomain {
  return eventType.slice(0, eventType.indexOf('.')) as EventDomain;
}

// ── Orthogonal axes (spec §3) — the single status field is dead ──────────────
export const PANE_STATES = ['live', 'dead', 'empty'] as const;
export type PaneState = (typeof PANE_STATES)[number];
export const PaneStateSchema = z.enum(PANE_STATES);

export const BINDING_STATES = ['unbound', 'bound'] as const;
export type BindingState = (typeof BINDING_STATES)[number];
export const BindingStateSchema = z.enum(BINDING_STATES);

export const ACTIVITY_STATES = ['working', 'idle', 'stopped', 'retired'] as const;
export type ActivityState = (typeof ACTIVITY_STATES)[number];
export const ActivityStateSchema = z.enum(ACTIVITY_STATES);

// ── Send chokepoint (spec §5) ────────────────────────────────────────────────
// Gate reasons enqueue-and-HOLD; each carries its TRUE typed cause. The #699
// class (typing_guard masking pane_unresolved) is unrepresentable because
// unresolved targets are REFUSED at admission (below), never gated.
export const SEND_GATE_REASONS = ['typing_guard'] as const;
export type SendGateReason = (typeof SEND_GATE_REASONS)[number];
export const SendGateReasonSchema = z.enum(SEND_GATE_REASONS);

// Admission refusals fail LOUD and never admit to the queue (spec §5 drain
// discipline). Distinct from gate reasons by construction.
export const SEND_REFUSAL_REASONS = [
  'pane_unresolved',
  'pane_dead',
  'schema_version_mismatch',
] as const;
export type SendRefusalReason = (typeof SEND_REFUSAL_REASONS)[number];
export const SendRefusalReasonSchema = z.enum(SEND_REFUSAL_REASONS);

// Partial-delivery verdicts (spec §5). `partial_delivered` MUST carry
// what-got-through evidence; started-then-died is first-class, never collapsed
// to pure failure and never silently truncated.
export const DELIVERY_VERDICTS = [
  'delivered',
  'enqueued_gated',
  'failed_none_delivered',
  'partial_delivered',
] as const;
export type DeliveryVerdict = (typeof DELIVERY_VERDICTS)[number];
export const DeliveryVerdictSchema = z.enum(DELIVERY_VERDICTS);

// Operator-presence activity window (spec §5). NAMED constant, echoed in every
// send_gated payload — no buried magic numbers. `client_activity` is bumped by
// ANY input from an attached client; a client counts as "present" on the target
// pane only if its last activity is within this window at the decision point.
// Tunable; the accepted tradeoff (spec §6 rider) is that scrolling also bumps
// client_activity and can gate a send.
export const SEND_PRESENCE_ACTIVITY_WINDOW_MS = 10_000;

// ── Provenance (spec §2) — three real emitters, hooks REAL but UNTRUSTED ──────
export const PROVENANCE_SOURCES = ['hook', 'wrapper', 'observer'] as const;
export type ProvenanceSource = (typeof PROVENANCE_SOURCES)[number];
export const ProvenanceSchema = z.object({
  source: z.enum(PROVENANCE_SOURCES),
  // The localhost edge_proxy transport receipt — separates hook-never-fired
  // from swallowed-after-arrival. Null when the emitter is the daemon itself.
  transport_receipt: z.string().nullable().optional(),
  emitter_version: z.number().int().nullable().optional(),
});
export type Provenance = z.infer<typeof ProvenanceSchema>;

// ── Event record (spec §2) — the 8 append-only columns, nothing derived ──────
// Payload holds DUMB FACTS only, never derived state. The store assigns `seq`
// (global monotonic, single writer) and `recorded_at` (daemon clock; skew vs
// `occurred_at` is visible data).
export const EventInputSchema = z.object({
  entity_type: EntityTypeSchema,
  entity_id: z.string().min(1),
  event_type: EventTypeSchema,
  payload: z.record(z.string(), z.unknown()),
  provenance: ProvenanceSchema,
  occurred_at: z.string().min(1),
});
export type EventInput = z.infer<typeof EventInputSchema>;

export const EventRecordSchema = EventInputSchema.extend({
  seq: z.number().int(),
  recorded_at: z.string(),
});
export type EventRecord = z.infer<typeof EventRecordSchema>;

// ── Projections (spec §10) — all three rebuilt by replay, nobody writes them ─
export const CurrentBindingSchema = z.object({
  seat_id: z.string(),
  wrapper_id: z.string().nullable(),
  instance_id: z.string().nullable(),
  persona: z.string().nullable(),
  tint: z.string().nullable(),
  // The bound-event seq the binding resolved against — receipts and drains
  // resolve against this exact seq (stale-target-at-drain unrepresentable).
  bound_seq: z.number().int(),
});
export type CurrentBinding = z.infer<typeof CurrentBindingSchema>;

export const FreelistEntrySchema = z.object({
  seat_id: z.string(),
  pane_state: PaneStateSchema, // live | empty (never dead — dead is a contradiction if bound)
});
export type FreelistEntry = z.infer<typeof FreelistEntrySchema>;

export const ActivityBoardRowSchema = z.object({
  entity_id: z.string(),
  entity_type: EntityTypeSchema,
  seat_id: z.string().nullable(),
  pane: PaneStateSchema,
  binding: BindingStateSchema,
  activity: ActivityStateSchema,
  queue_depth: z.number().int(), // projection column, NOT an axis
  persona: z.string().nullable(),
  tint: z.string().nullable(),
});
export type ActivityBoardRow = z.infer<typeof ActivityBoardRowSchema>;

// "Currently contradicted" is a STREAM FILTER, never a projection table.
export const OpenContradictionSchema = z.object({
  seq: z.number().int(),
  entity_type: EntityTypeSchema,
  entity_id: z.string(),
  kind: z.string(),
  missing_attestation: z.string().nullable(),
  detail: z.string().nullable(),
  occurred_at: z.string(),
});
export type OpenContradiction = z.infer<typeof OpenContradictionSchema>;

// ── API surface (spec §7) ────────────────────────────────────────────────────
export const HealthSchema = z.object({
  ok: z.boolean(),
  service: z.literal('k12_daemon'),
  schema_version: z.number().int(),
  version: z.string(),
  git_sha: z.string(),
  bun: z.string(),
  machine: z.string(),
  events: z.number().int(),
  // Honest-only: bring-up mode reports ok=false while any contradiction is open.
  open_contradictions: z.number().int(),
  tmux_reachable: z.boolean(),
});
export type Health = z.infer<typeof HealthSchema>;

export const LaunchRequestSchema = z.object({
  seat_id: z.string().min(1),
  schema_version: z.number().int(),
  // The attestation tuple the reg-audit scaffold checks. At door step 1 the
  // set is small; later doors grow it. A missing field the audit demands =
  // refused handover (stop-the-line), never a silent partial launch.
  identity: z.string().min(1).optional(),
  persona: z.string().min(1).optional(),
  rank: z.string().min(1).optional(),
  tint: z.string().min(1).optional(),
  commander: z.string().min(1).optional(),
  singleton_ok: z.boolean().optional(),
  dispatch_target: z.string().min(1).optional(),
});
export type LaunchRequest = z.infer<typeof LaunchRequestSchema>;

export const LaunchResponseSchema = z.object({
  ok: z.boolean(),
  seat_id: z.string(),
  handover: z.boolean(), // false when the reg-audit refused
  missing_attestations: z.array(z.string()),
  reason: z.string().nullable(),
});
export type LaunchResponse = z.infer<typeof LaunchResponseSchema>;

// The resolution a send is bound to — carried verbatim from admission through
// drain into the receipt. NEVER re-derived (kills the target=unresolved
// split-brain, spec §5 fresh datum 07-12).
export const SendResolutionSchema = z.object({
  target: z.string(),
  seat_id: z.string(),
  bound_seq: z.number().int(),
});
export type SendResolution = z.infer<typeof SendResolutionSchema>;

export const SendRequestSchema = z.object({
  target: z.string().min(1), // canonical id ONLY — never a tmux %id
  text: z.string(),
  schema_version: z.number().int(),
});
export type SendRequest = z.infer<typeof SendRequestSchema>;

// Base shape kept separate so consumers can still .extend()/.pick()/.omit();
// the refined schema below enforces the partial-delivery evidence invariant.
export const SendReceiptBaseSchema = z.object({
  verdict: DeliveryVerdictSchema,
  resolution: SendResolutionSchema, // the SAME resolution the send used
  gate_reason: SendGateReasonSchema.nullable(),
  activity_window_ms: z.number().int().nonnegative().nullable(), // echoed when gated
  bytes_delivered: z.number().int().nonnegative().nullable(), // required non-null for partial_delivered
  send_seq: z.number().int().nonnegative(), // seq is 1-based and monotonic
});
export const SendReceiptSchema = SendReceiptBaseSchema.refine(
  (r) => r.verdict !== 'partial_delivered' || r.bytes_delivered !== null,
  { message: 'partial_delivered must carry non-null bytes_delivered', path: ['bytes_delivered'] },
);
export type SendReceipt = z.infer<typeof SendReceiptSchema>;

// Admission refusal (fail-loud; nothing admitted to the queue).
export const SendRefusalSchema = z.object({
  ok: z.literal(false),
  refused: z.literal(true),
  reason: SendRefusalReasonSchema,
  target: z.string(),
});
export type SendRefusal = z.infer<typeof SendRefusalSchema>;

// ── Close operation (rung 3) — the generic "close this instance" system ──────
// Executes the terminal-retirement chain for a bound estate seat: reg.retired +
// reg.process_reaped (the agent process is reaped) + reg.seat_cleared (binding
// cleared → seat returns to the freelist). The persistent estate PANE is kept
// and respawned bare, so the estate stays standing. Reap-first, attest-after: the
// three events are recorded only once the process is confirmed reaped, so a
// retire-with-live-process is unspellable — a failed reap refuses loud, changing
// nothing (spec §4: retired is not terminal until process_reaped + seat_cleared).
export const CloseRequestSchema = z.object({
  target: z.string().min(1), // canonical seat id OR instance id — never a tmux %id
  schema_version: z.number().int(),
});
export type CloseRequest = z.infer<typeof CloseRequestSchema>;

export const CloseResponseSchema = z.object({
  ok: z.boolean(),
  target: z.string(),
  seat_id: z.string().nullable(),
  instance_id: z.string().nullable(),
  closed: z.boolean(), // true = full retire chain attested + seat freed
  reason: z.string().nullable(),
});
export type CloseResponse = z.infer<typeof CloseResponseSchema>;

// ── Stop ingestion (rung 3) — the stop-hook's door into the daemon ───────────
// A stop-hook reports that an instance's turn ended. The door has three honest
// outcomes, none of them a blind swallow:
//   - recorded: a fresh act.stop_reported for a currently-bound, not-yet-stopped
//     instance (activity → stopped).
//   - deduped: a repeat/late stop for an instance already stopped or already
//     closed — writes act.receipt_deduped (idempotent, NO blind swallow).
//   - refused: a stop for an instance that NEVER walked through /launch (never
//     bound) — a ghost. Refused loud at admission; nothing recorded, so no
//     phantom row and no re-firing subscription can exist (the 77f7cfb4 class).
export const StopRequestSchema = z.object({
  instance_id: z.string().min(1), // canonical instance id ONLY — never a tmux %id
  schema_version: z.number().int(),
});
export type StopRequest = z.infer<typeof StopRequestSchema>;

export const STOP_REFUSAL_REASONS = ['no_such_instance', 'schema_version_mismatch'] as const;
export type StopRefusalReason = (typeof STOP_REFUSAL_REASONS)[number];
export const StopRefusalReasonSchema = z.enum(STOP_REFUSAL_REASONS);

export const StopReceiptSchema = z.object({
  ok: z.literal(true),
  instance_id: z.string(),
  recorded: z.boolean(), // true = stop_reported appended; false = deduped
  deduped: z.boolean(),
  activity: ActivityStateSchema.nullable(), // resulting activity for the instance
});
export type StopReceipt = z.infer<typeof StopReceiptSchema>;

export const StopRefusalSchema = z.object({
  ok: z.literal(false),
  refused: z.literal(true),
  reason: StopRefusalReasonSchema,
  instance_id: z.string(),
});
export type StopRefusal = z.infer<typeof StopRefusalSchema>;

export const ReconcileResponseSchema = z.object({
  ok: z.boolean(),
  replayed_events: z.number().int(),
  replay_ms: z.number(),
  bindings: z.number().int(),
  freelist: z.number().int(),
  instances: z.number().int(),
  // New contradictions flagged this reconcile pass, and all currently-open ones.
  new_contradictions: z.array(OpenContradictionSchema),
  open_contradictions: z.array(OpenContradictionSchema),
  // Bring-up mode: any open contradiction is p0. ok=false, fail loud.
  p0: z.boolean(),
});
export type ReconcileResponse = z.infer<typeof ReconcileResponseSchema>;

export const EntitiesResponseSchema = z.object({
  schema_version: z.number().int(),
  rows: z.array(ActivityBoardRowSchema),
});
export type EntitiesResponse = z.infer<typeof EntitiesResponseSchema>;

export const EntityEventsResponseSchema = z.object({
  entity_id: z.string(),
  events: z.array(EventRecordSchema),
});
export type EntityEventsResponse = z.infer<typeof EntityEventsResponseSchema>;
