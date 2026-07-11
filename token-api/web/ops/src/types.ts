// Typed contracts for the ops cockpit.
//
// `OpsState` mirrors `GET /api/ui/ops/state` (the cockpit boundary).
// `TimerHistory` and `OpsGraph` mirror the live read-models consumed by the
// chart components, with mock graph data retained only as a degraded fallback.

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
  runtime: { live: boolean; pane_id: string | null; role: string | null; source: string };
  last_activity: string | null;
  age_seconds: number | null;
  age_minutes: number | null;
  is_subagent: boolean;
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
  instance_id: string;
  name: string | null;
  message: string; // full text — UI clamps with CSS, expands on click
  voice: string | null;
  queue: string; // "hot" | "pause"
  status?: string; // queued | playing | completed
  queued_at: string; // ISO timestamp
};

export type TtsCurrent = {
  instance_id: string;
  name: string | null;
  message: string;
  voice: string | null;
  backend?: string | null;
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
  };
  alarm?: {
    acked: boolean;
    day_started_at: string | null;
    source: string | null;
  };
  work_actions?: WorkActionSummary;
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
  golden_throne: string | null;
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
