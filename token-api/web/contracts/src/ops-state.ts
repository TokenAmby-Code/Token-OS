// Shared typed contracts for the Token-OS ops read-models (`@token-os/contracts`).
//
// `OpsState` mirrors `GET /api/ui/ops/state` (the cockpit boundary).
// `TimerHistory` and `OpsGraph` mirror the live read-models consumed by the
// chart components, with mock graph data retained only as a degraded fallback.
//
// The hand-written types are the compile-time contract (moved verbatim from
// the cockpit's `web/ops/src/types.ts`). The Zod schemas at the bottom are
// runtime validators for consumer boundaries (cockpit polls; discord daemon
// from PR C). They are deliberately permissive — loose objects pass unknown
// keys through and non-spine fields are optional — so `ops-state.v1` never
// rejects fields Python already emits or later grows. A schema miss is an
// advisory log at the consumer, never a render blocker.

import { z } from 'zod';

export const CONTRACT_VERSION = 'ops-state.v1';

export type Counts = Record<string, number>;

export type TimerMode =
  | 'working'
  | 'multitasking'
  | 'distracted'
  | 'break'
  | 'idle'
  | 'sleeping'
  | 'quiet'
  | 'morning_session'
  | string;

export type SessionDoc = {
  id: number | null;
  title: string | null;
  path: string | null;
  status: string | null;
  project: string | null;
  policy: string | null;
  binding_source: string | null;
  cron_job_id: string | null;
};

export type OpsInstance = {
  id: string;
  display_name: string;
  name: string | null;
  status: string;
  engine: string;
  device_id: string | null;
  working_dir: string | null;
  // Fleet-queue domain — server-side cwd classification (the browser never
  // decides from a raw path). 'token-os' = the LEFT worker system,
  // 'askcivic' = the RIGHT. Optional in the wire shape: an old payload
  // missing it must not blank the cockpit (consumers default to 'token-os').
  domain?: 'token-os' | 'askcivic' | string;
  runtime: { live: boolean; pane_id: string | null; role: string | null; source: string };
  last_activity: string | null;
  created_at: string | null;
  age_seconds: number | null;
  age_minutes: number | null;
  is_subagent: boolean;
  // 'emperor' | 'persona' | 'chapter' — chapter children legitimately share a
  // persona (the DB singleton trigger exempts them; UI breach-marking must too).
  commander_type: string | null;
  persona: { slug: string | null; display_name: string | null; pane_tint: string | null; chip_color: string | null; tts_voice: string | null; tts_rate?: number | null; notification_sound: string | null } | null;
  golden_throne: string | null;
  pr_url: string | null;
  pr_state: string | null; // "open" | "merged" | null
  workflow_state: string | null;
  next_required_action: string | null;
  stop_allowed: boolean | null;
  session_doc: SessionDoc;
  stale: { is_stale: boolean; threshold_seconds: number | null; reason: string | null };
  attention_rank: number;
  attention_reasons: string[];
  zealotry: number;
  gt: {
    next_fire: string | null;
    resume_count: number;
    resume_window_started_at: string | null;
    last_resume_at: string | null;
    victory_at: string | null;
    victory_reason: string | null;
  };
};

export type TtsQueueItem = {
  item_key?: string;
  instance_id: string;
  name: string | null;
  message: string; // full text — UI clamps with CSS, expands on click
  voice: string | null;
  persona_slug?: string | null;
  persona_display_name?: string | null;
  commander_type?: string | null;
  playback_target?: string | null;
  queue: string; // "hot" | "pause"
  status?: string; // queued | playing | completed
  queued_at: string; // ISO timestamp
};

export type TtsCurrent = {
  item_key?: string;
  instance_id: string;
  name: string | null;
  message: string;
  voice: string | null;
  backend?: string | null;
  persona_slug?: string | null;
  persona_display_name?: string | null;
  commander_type?: string | null;
  playback_target?: string | null;
  started_at?: string | null; // ISO; present when cheaply available
};

export type TtsRouting = {
  device: string; // wsl | phone | mac | discord
  reason: string; // why this device
  context?: Record<string, unknown>;
};

export type TtsGlobalMode = 'verbose' | 'muted' | 'silent';

export type VoiceDraft = {
  bot_name: string;
  author_id: string;
  pane: string | null;
  created_at: string | null;
  utterances: number;
  pane_alive: boolean | null;
};

export type OpsEvent = {
  event_type: string;
  instance_id: string | null;
  device_id: string | null;
  details: unknown;
  created_at: string;
};

export type StateAssertion = {
  id: string;
  label: string;
  value: string;
  status: 'good' | 'warn' | 'bad' | 'neutral' | string;
  confidence: 'high' | 'medium' | 'low' | string;
  evidence: string[];
  freshness_seconds: number | null;
  correction_hint: string | null;
  details: Record<string, unknown>;
};

export type OpsHealthStatus = 'ok' | 'warn' | 'bad' | 'unknown';

export type OpsSourceHealth = {
  status: OpsHealthStatus;
  available: boolean | null;
  message: string | null;
  details: Record<string, unknown>;
};

export type OpsSourceFreshnessStatus = 'fresh' | 'stale' | 'missing' | 'unknown';

export type OpsSourceFreshness = {
  status: OpsSourceFreshnessStatus;
  age_seconds: number | null;
  last_seen: string | null;
  stale_after_seconds: number | null;
  message: string;
  evidence: string[];
};

export type OpsSourceFreshnessMap = {
  desktop_attention: OpsSourceFreshness;
  phone_activity: OpsSourceFreshness;
  phone_heartbeat: OpsSourceFreshness;
  work_state: OpsSourceFreshness;
  timer_engine: OpsSourceFreshness;
  agents_db: OpsSourceFreshness;
  tmuxctld: OpsSourceFreshness;
  cron: OpsSourceFreshness;
  enforcement: OpsSourceFreshness;
  tts: OpsSourceFreshness;
};

export type OpsRecommendedAction = {
  id: string;
  source_assertion_id: string;
  severity: 'warn' | 'bad';
  label: string;
  action: string;
  evidence: string[];
};

export type OpsSourceMap = {
  token_api: OpsSourceHealth;
  agents_db: OpsSourceHealth;
  timer_engine: OpsSourceHealth;
  tmuxctld: OpsSourceHealth;
  cron: OpsSourceHealth;
  enforcement: OpsSourceHealth;
  tts: OpsSourceHealth;
};

export type OpsHealthSummary = {
  status: OpsHealthStatus;
  summary: string;
  degraded_sources: string[];
  bad_assertion_count: number;
  warn_assertion_count: number;
  recommended_actions: OpsRecommendedAction[];
};

export type OpsBillableSummary = {
  on_the_clock: boolean;
  active_counts: { billable: number; personal: number; unknown: number };
  work_seconds: Partial<Record<'billable' | 'personal' | 'unknown', number>> & Counts;
  break_seconds: Partial<Record<'billable' | 'personal' | 'unknown', number>> & Counts;
  x_work_instances: number;
  y_distraction: number;
  accrual_weight: number;
  trickle_numerator: number;
};

export type InstanceCounts = {
  active: number;
  stale: number;
  by_status: Counts;
  by_engine: Counts;
  by_persona: Counts;
  by_work_class?: Counts;
};

export type TmuxOccupancyCellState = 'occupied' | 'free' | 'dead' | 'protected' | 'drift' | 'unknown';

export type TmuxOccupancyCell = {
  pane_positional_id: string | null;
  instance_id: string | null;
  persona: string | null;
  engine: string | null;
  working_dir: string | null;
  wrapper_id: string | null;
  state: TmuxOccupancyCellState;
  source: string;
};

export type TmuxOccupancy = {
  status: OpsHealthStatus;
  generated_at: string;
  total: number;
  occupied: number;
  free: number;
  dead: number;
  protected: number;
  drift: number;
  unknown: number;
  errors: string[];
  cells: TmuxOccupancyCell[];
};

export type OpsState = {
  surface: 'ops';
  contract_version: 'ops-state.v1' | string;
  ui_build_id: string | null;
  generated_at: string;
  health: OpsHealthSummary;
  sources: OpsSourceMap;
  timer: {
    mode: TimerMode;
    activity: string;
    productivity_active: boolean;
    manual_mode: string | null;
    focus_active: boolean;
    break_balance_ms: number;
    break_available_ms: number;
    break_backlog_ms: number;
    is_in_backlog: boolean;
    total_work_time_ms: number;
    total_break_time_ms: number;
    idle_timer?: {
      visible: boolean;
      state: string;
      label: string | null;
      reason: string;
      remaining_seconds: number | null;
      timeout_seconds?: number;
    };
  };
  billable: OpsBillableSummary;
  assertions: StateAssertion[];
  recommended_actions: OpsRecommendedAction[];
  source_freshness: OpsSourceFreshnessMap;
  attention: {
    desktop: {
      mode: string;
      work_mode: string;
      last_detection: string | null;
      location_zone: string | null;
      ahk_reachable: boolean | null;
      steam_app_name: string | null;
      steam_exe: string | null;
      in_meeting: boolean;
    };
    phone: {
      app: string | null;
      is_distracted: boolean;
      last_activity: string | null;
      app_opened_at: string | null;
      heartbeat_age_seconds: number | null;
    };
  };
  work_state: {
    productivity_active: boolean;
    reason: string;
    active_instance_count: number;
    processing_recent_count: number;
    observed_agent_count: number;
    timer_mode: string;
    desktop_mode: string;
    phone_app: string | null;
    productivity_hold?: string;
    work_action_source?: string | null;
    work_action_note?: string | null;
    work_action_age_seconds?: number | null;
    work_action_buffer_remaining_seconds?: number | null;
    typing_active?: boolean;
  };
  instances: {
    active: OpsInstance[];
    counts: InstanceCounts;
  };
  events: OpsEvent[];
  cron: {
    available: boolean;
    total_jobs: number;
    enabled: number;
    running: number;
    runs_last_24h: number;
    jobs: Array<Record<string, unknown>>;
    error?: string;
  };
  tts: {
    current: TtsCurrent | null;
    routing?: TtsRouting | null;
    hot_queue: TtsQueueItem[];
    pause_queue: TtsQueueItem[];
    hot_queue_length: number;
    pause_queue_length: number;
    queue_length: number;
    backend: string | null;
    satellite_available: boolean | null;
    global_mode: string | null;
  };
  voice_drafts: VoiceDraft[];
  enforcement: {
    available: boolean;
    pending_count: number;
    pending: Array<Record<string, unknown>>;
    pavlok: Record<string, unknown>;
    error?: string;
  };
  tmux: {
    reachable: boolean | null;
    tmux_reachable: boolean | null;
    version: string | null;
    sha: string | null;
    error?: string | null;
    payload?: unknown;
    occupancy?: TmuxOccupancy;
  };
  alarm?: {
    acked: boolean;
    day_started_at: string | null;
    source: string | null;
  };
  work_actions?: WorkActionSummary;
  /**
   * Muster Ledger feed embedded per the #671 contract: the kanban board
   * consumes useOpsState — one poller, one feed (same builder as
   * GET /api/ui/ops/session-docs, capped tighter for the board).
   */
  session_docs?: SessionDocsFeed;
};

// Concise agent/script read model (GET /api/ops/status).
export type OpsStatus = {
  surface: 'ops-status';
  generated_at: string;
  status: OpsHealthStatus;
  summary: string;
  sources: OpsSourceMap;
  source_freshness: OpsSourceFreshnessMap;
  timer: {
    mode: string;
    activity: string;
    productivity_active: boolean;
    break_balance_ms: number;
    break_available_ms: number;
    break_backlog_ms: number;
    is_in_backlog: boolean;
  };
  attention: {
    desktop_mode: string;
    desktop_work_mode: string;
    phone_app: string | null;
    phone_distracted: boolean;
    phone_heartbeat_age_seconds: number | null;
  };
  fleet: {
    active: number;
    stale: number;
    by_status: Counts;
    by_engine: Counts;
    by_persona: Counts;
  };
  tmux: {
    reachable: boolean | null;
    tmux_reachable: boolean | null;
    version: string | null;
    sha: string | null;
    live_instance_panes: number | null;
    projection_drift: number | null;
    occupancy?: TmuxOccupancy;
  };
  tts: {
    current: string | null;
    queue_length: number;
    hot_queue_length: number;
    pause_queue_length: number;
    satellite_available: boolean | null;
    global_mode: string | null;
  };
  enforcement: {
    pending_count: number;
    pavlok_enabled: boolean | null;
  };
  assertions: StateAssertion[];
  recommended_actions: OpsRecommendedAction[];
};

// One explicit work-action press: timeline tick + dial input.
export type WorkActionTick = {
  at: string; // local ISO timestamp of the press
  source: string | null;
};

// Work-action visualization read model (GET /api/ui/ops/state → work_actions).
export type WorkActionSummary = {
  count: number; // load-bearing: explicit work-actions today
  ticks: WorkActionTick[];
  last_at: string | null; // drives the staleness green→red fade
  score: number; // non-load-bearing: all work_signal events today
  stale_fade_minutes: number; // fade window; backend + frontend agree
};

// ── Timer history read-model (GET /api/ui/ops/timer/history) ──────────────

export type TimerHistoryPoint = {
  t: string; // ISO timestamp
  break_balance_ms: number; // left axis
  total_work_time_ms?: number;
  productivity_active: boolean;
  activity?: string | null;
  active_instance_count?: number;
  processing_recent_count?: number;
  observed_agent_count?: number;
  desktop_mode?: string | null;
  phone_app?: string | null;
  sample_source?: string | null;
  gap_before?: boolean;
  gap_reason?: string;
  anomaly?: boolean;
  anomaly_reason?: string;
  delta_balance_ms?: number;
  mode: TimerMode;
};

export type TimerHistorySegment = {
  start: string;
  end: string;
  mode: TimerMode;
  activity: string;
  productivity_active?: boolean;
  source?: string | null;
};

export type TimerHistoryAnnotation = {
  id: string;
  t: string;
  lane: 'timer' | 'desktop' | 'phone' | 'enforcement' | 'gt' | 'instance' | string;
  type: string;
  label: string;
  severity?: 'info' | 'warn' | 'bad' | 'good' | string;
  details?: Record<string, unknown>;
};

export type TimerHistory = {
  generated_at: string;
  window_seconds: number;
  bucket_seconds: number;
  gap_threshold_seconds?: number;
  points: TimerHistoryPoint[];
  segments: TimerHistorySegment[];
  // Explicit work-action presses within the graph window — drawn as vertical
  // ticks, distinct from the timer_shifts mode bands and session dividers.
  work_action_ticks?: WorkActionTick[];
  annotations?: TimerHistoryAnnotation[];
  gaps?: Array<{ start: string; end: string; reason: string; anomaly_reason?: string }>;
  anomalies?: Array<Record<string, unknown>>;
  anomaly_summary?: {
    count: number;
    gap_count: number;
    gap_count_by_reason?: Record<string, number>;
    latest?: Record<string, unknown> | null;
    // A wall of anomalies is a reverse signal: when the detector flags a large
    // share of the window at once the batch is treated as a systemic false
    // detection, not real timer violations. `count` is then 0 and the
    // suppressed wall size is reported here for a calmer "suspect" banner.
    bulk_suspected?: boolean;
    suppressed_count?: number;
    dominant_reason?: string | null;
  };
  source?: string;
};

// ── Session-doc pipeline read-model (GET /api/ui/ops/session-docs) ────────
// Read-only board feed. The cockpit groups these into status lanes and never
// renders more than `head` (one line); the document itself lives in Obsidian.

/**
 * Golden Throne rubric state as summarized by the session-docs feed. `present`
 * is the load-bearing flag: legacy docs with no rubric evaluate as
 * complete:true, so consumers must key every rubric treatment on `present` —
 * never `complete` alone.
 */
export type RubricSummary = {
  present: boolean;
  complete: boolean;
  met: number;
  total: number;
  skipped: number;
  first_unmet: string | null;
  notified_at: string | null;
  acknowledged_at: string | null;
};

export type PipelineDoc = {
  id: number | null;
  title: string | null;
  path: string | null;
  vault_rel: string | null;
  obsidian_uri: string | null;
  status: string;
  project: string | null;
  primarch: string | null;
  persona_slug: string | null;
  /** seeded persona profile join — chip_color null for unknown/absent slugs */
  persona: { slug: string | null; chip_color: string | null; display_name: string | null } | null;
  golden_throne: string | null;
  rubric: RubricSummary | null;
  head: string | null; // one-line excerpt only — never the full document
  created_at: string | null;
  /**
   * Preferred doc-date basis for cockpit date filters:
   * frontmatter created/start_time/date, then DB created_at.
   */
  session_date?: string | null;
  session_date_source?: string | null;
  age_seconds: number | null; // since creation — surfaces long-open docs honestly
  linked_instances: number;
};

export type SessionDocsFeed = {
  generated_at: string;
  /** true per-status counts before per-lane capping — so the board never lies */
  lane_totals: Record<string, number>;
  limit_per_lane: number;
  docs: PipelineDoc[];
};

// ── Arbitrary node/edge graph read-model (GET /api/ui/ops/graph/{name}) ───

export type OpsGraphNode = {
  id: string;
  type: string; // instance, session_doc, cron_job, event, device, ...
  label: string;
  subtitle?: string;
  status?: string;
  group?: string;
  weight?: number;
  href?: string;
  data?: Record<string, unknown>;
};

export type OpsGraphEdge = {
  id: string;
  source: string;
  target: string;
  type: string; // bound_to, spawned, resumed_by, caused, blocks, ...
  directed: boolean;
  label?: string;
  status?: string; // active | stale | blocked | completed | ...
  weight?: number;
  data?: Record<string, unknown>;
};

export type OpsGraph = {
  graph: string;
  generated_at: string;
  layout_hint?: 'force' | 'dagre' | 'elk' | 'radial';
  nodes: OpsGraphNode[];
  edges: OpsGraphEdge[];
};

// ── Zod runtime schemas (ops-state.v1) ─────────────────────────────────────
// Spine fields (identity + the arrays consumers iterate) are required; all
// else optional. Loose objects: unknown keys always pass through.

export const TimerModeSchema = z.string();

export const StateAssertionSchema = z.looseObject({
  id: z.string(),
  label: z.string().optional(),
  value: z.string().optional(),
  status: z.string().optional(),
  confidence: z.string().optional(),
  evidence: z.array(z.string()).optional(),
  freshness_seconds: z.number().nullable().optional(),
  correction_hint: z.string().nullable().optional(),
  details: z.record(z.string(), z.unknown()).optional(),
});

export const OpsRecommendedActionSchema = z.looseObject({
  id: z.string(),
  source_assertion_id: z.string().optional(),
  severity: z.string().optional(),
  label: z.string().optional(),
  action: z.string().optional(),
  evidence: z.array(z.string()).optional(),
});

export const OpsHealthSummarySchema = z.looseObject({
  status: z.string(),
  summary: z.string().optional(),
  degraded_sources: z.array(z.string()).optional(),
  bad_assertion_count: z.number().optional(),
  warn_assertion_count: z.number().optional(),
  recommended_actions: z.array(OpsRecommendedActionSchema).optional(),
});

export const TtsRoutingSchema = z.looseObject({
  device: z.string(),
  reason: z.string().optional(),
  context: z.record(z.string(), z.unknown()).optional(),
});

export const OpsInstanceSchema = z.looseObject({
  id: z.string(),
  display_name: z.string().optional(),
  name: z.string().nullable().optional(),
  status: z.string().optional(),
  engine: z.string().optional(),
  device_id: z.string().nullable().optional(),
  working_dir: z.string().nullable().optional(),
  domain: z.string().optional(),
  last_activity: z.string().nullable().optional(),
  age_seconds: z.number().nullable().optional(),
  is_subagent: z.boolean().optional(),
  golden_throne: z.string().nullable().optional(),
  pr_url: z.string().nullable().optional(),
  pr_state: z.string().nullable().optional(),
  workflow_state: z.string().nullable().optional(),
  attention_rank: z.number().optional(),
  attention_reasons: z.array(z.string()).optional(),
});


export const TmuxOccupancyCellStateSchema = z.enum(['occupied', 'free', 'dead', 'protected', 'drift', 'unknown']);

export const TmuxOccupancyCellSchema = z.looseObject({
  pane_positional_id: z.string().nullable(),
  instance_id: z.string().nullable().optional(),
  persona: z.string().nullable().optional(),
  engine: z.string().nullable().optional(),
  working_dir: z.string().nullable().optional(),
  wrapper_id: z.string().nullable().optional(),
  state: TmuxOccupancyCellStateSchema,
  source: z.string().optional(),
});

export const TmuxOccupancySchema = z.looseObject({
  status: z.string(),
  generated_at: z.string(),
  total: z.number(),
  occupied: z.number(),
  free: z.number(),
  dead: z.number(),
  protected: z.number(),
  drift: z.number(),
  unknown: z.number(),
  errors: z.array(z.string()).optional(),
  cells: z.array(TmuxOccupancyCellSchema),
});

export const TmuxHealthSchema = z.looseObject({
  reachable: z.boolean().nullable().optional(),
  tmux_reachable: z.boolean().nullable().optional(),
  version: z.string().nullable().optional(),
  sha: z.string().nullable().optional(),
  error: z.string().nullable().optional(),
  payload: z.unknown().optional(),
  occupancy: TmuxOccupancySchema.optional(),
});

export const OpsStateSchema = z.looseObject({
  surface: z.string().optional(),
  contract_version: z.string(),
  generated_at: z.string(),
  health: OpsHealthSummarySchema.optional(),
  timer: z.looseObject({ mode: TimerModeSchema }).optional(),
  assertions: z.array(StateAssertionSchema).optional(),
  recommended_actions: z.array(OpsRecommendedActionSchema).optional(),
  instances: z.looseObject({
    active: z.array(OpsInstanceSchema),
    counts: z.looseObject({}).optional(),
  }),
  events: z.array(z.looseObject({ event_type: z.string().optional() })).optional(),
  tts: z
    .looseObject({ routing: TtsRoutingSchema.nullable().optional() })
    .optional(),
  tmux: TmuxHealthSchema.optional(),
});

export const OpsStatusSchema = z.looseObject({
  surface: z.string().optional(),
  generated_at: z.string(),
  status: z.string(),
  summary: z.string().optional(),
  assertions: z.array(StateAssertionSchema).optional(),
  recommended_actions: z.array(OpsRecommendedActionSchema).optional(),
  fleet: z.looseObject({ active: z.number().optional() }).optional(),
});

// Spine-only validators for the cockpit's other poll feeds.
export const TimerHistorySchema = z.looseObject({
  generated_at: z.string(),
  points: z.array(z.looseObject({ t: z.string() })),
  segments: z.array(z.looseObject({ start: z.string(), end: z.string() })).optional(),
});

// Golden Throne rubric state as summarized by the session-docs feed. `present`
// is the load-bearing flag: legacy docs with no rubric evaluate as
// complete:true, so consumers must key every rubric treatment on `present` —
// never `complete` alone.
export const RubricSummarySchema = z.looseObject({
  present: z.boolean().optional(),
  complete: z.boolean().optional(),
  met: z.number().optional(),
  total: z.number().optional(),
  skipped: z.number().optional(),
  first_unmet: z.string().nullable().optional(),
  notified_at: z.string().nullable().optional(),
  acknowledged_at: z.string().nullable().optional(),
});

// One session-doc card on the Muster Ledger. Spine (`id`/`status`) required;
// everything else optional/nullable — validation is advisory and must never
// block a render.
export const PipelineDocSchema = z.looseObject({
  id: z.number(),
  status: z.string(),
  title: z.string().nullable().optional(),
  path: z.string().nullable().optional(),
  vault_rel: z.string().nullable().optional(),
  obsidian_uri: z.string().nullable().optional(),
  project: z.string().nullable().optional(),
  primarch: z.string().nullable().optional(),
  persona_slug: z.string().nullable().optional(),
  persona: z
    .looseObject({
      slug: z.string().nullable().optional(),
      chip_color: z.string().nullable().optional(),
      display_name: z.string().nullable().optional(),
    })
    .nullable()
    .optional(),
  golden_throne: z.string().nullable().optional(),
  rubric: RubricSummarySchema.nullable().optional(),
  head: z.string().nullable().optional(),
  created_at: z.string().nullable().optional(),
  session_date: z.string().nullable().optional(),
  session_date_source: z.string().nullable().optional(),
  age_seconds: z.number().nullable().optional(),
  linked_instances: z.number().optional(),
});

export const SessionDocsFeedSchema = z.looseObject({
  generated_at: z.string(),
  lane_totals: z.record(z.string(), z.number()).optional(),
  limit_per_lane: z.number().optional(),
  docs: z.array(PipelineDocSchema),
});

export const OpsGraphSchema = z.looseObject({
  graph: z.string(),
  generated_at: z.string(),
  nodes: z.array(z.looseObject({ id: z.string() })),
  edges: z.array(z.looseObject({ source: z.string(), target: z.string() })),
});

// Runtime-validated shapes. The hand-written types above stay the compile-time
// contract consumed by the cockpit (zero-churn re-export); these are what a
// `.parse()` provably returns — the daemon-side (PR C/D) input types.
export type OpsStateParsed = z.infer<typeof OpsStateSchema>;
export type OpsStatusParsed = z.infer<typeof OpsStatusSchema>;
export type OpsInstanceParsed = z.infer<typeof OpsInstanceSchema>;
export type RubricSummaryParsed = z.infer<typeof RubricSummarySchema>;
export type PipelineDocParsed = z.infer<typeof PipelineDocSchema>;
export type SessionDocsFeedParsed = z.infer<typeof SessionDocsFeedSchema>;
