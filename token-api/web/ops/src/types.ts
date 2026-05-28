// Typed contracts for the ops cockpit.
//
// `OpsState` mirrors `GET /api/ui/ops/state` (the cockpit boundary).
// `TimerHistory` and `OpsGraph` mirror the proposed read-models from
// docs/ops-cockpit-frontend-design-brief.md. They are consumed by the chart
// components today via mocked data, and will swap to live endpoints unchanged.

export type Counts = Record<string, number>;

export type TimerMode =
  | 'working'
  | 'multitasking'
  | 'distracted'
  | 'break'
  | 'idle'
  | 'sleeping'
  | 'quiet'
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
  session_id: string | null;
  display_name: string;
  tab_name: string | null;
  status: string;
  engine: string;
  device_id: string | null;
  working_dir: string | null;
  tmux_pane: string | null;
  pane_label: string | null;
  last_activity: string | null;
  age_seconds: number | null;
  age_minutes: number | null;
  is_subagent: boolean;
  legion: string | null;
  instance_type: string | null;
  workflow_state: string | null;
  next_required_action: string | null;
  stop_allowed: boolean | null;
  session_doc: SessionDoc;
  stale: { is_stale: boolean; threshold_seconds: number | null; reason: string | null };
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
  tab_name: string | null;
  message: string;
  voice: string | null;
  queue: string; // "hot" | "pause"
  queued_at: string; // ISO timestamp
};

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

export type OpsState = {
  surface: 'ops';
  ui_build_id: string | null;
  generated_at: string;
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
  assertions: StateAssertion[];
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
    counts: {
      active: number;
      stale: number;
      by_status: Counts;
      by_engine: Counts;
      by_legion: Counts;
    };
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
    current: Record<string, unknown> | null;
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
  annotations?: TimerHistoryAnnotation[];
  gaps?: Array<{ start: string; end: string; reason: string; anomaly_reason?: string }>;
  anomalies?: Array<Record<string, unknown>>;
  anomaly_summary?: {
    count: number;
    gap_count: number;
    gap_count_by_reason?: Record<string, number>;
    latest?: Record<string, unknown> | null;
  };
  source?: string;
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
