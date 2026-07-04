// Presentation selectors for the ops cockpit. Keep this file pure: it accepts
// already-fetched OpsState from api.ts and never performs network access.

import type {
  OpsHealthStatus,
  OpsRecommendedAction,
  OpsSourceFreshness,
  OpsSourceFreshnessStatus,
  OpsSourceHealth,
  OpsState,
  StateAssertion,
} from './types';

export type CockpitTone = 'good' | 'warn' | 'bad' | 'neutral';
export type SourceHealthBucket = 'bad' | 'warn' | 'stale' | 'missing' | 'unknown' | 'fresh';
export type NoteworthyDialSource = 'health' | 'sources' | 'corrections' | 'assertions';
export type DrawerSummaryKind = 'health' | 'sources' | 'corrections' | 'assertions';

export type NoteworthyDial = {
  id: string;
  label: string;
  value: string;
  detail: string;
  tone: CockpitTone;
  source: NoteworthyDialSource;
  drawerId: string;
  title: string;
  sort: number;
};

export type DrawerRailSummary = {
  id: string;
  kind: DrawerSummaryKind;
  label: string;
  count: number;
  tone: CockpitTone;
  headline: string;
  detail: string;
  itemIds: string[];
};

export type CorrectionQueueItem = {
  id: string;
  sourceAssertionId: string;
  severity: OpsRecommendedAction['severity'];
  tone: Extract<CockpitTone, 'warn' | 'bad'>;
  label: string;
  action: string;
  evidence: string[];
};

export type SourceHealthItem = {
  id: string;
  label: string;
  bucket: SourceHealthBucket;
  tone: CockpitTone;
  healthStatus: OpsHealthStatus | null;
  freshnessStatus: OpsSourceFreshnessStatus | null;
  available: boolean | null;
  ageSeconds: number | null;
  lastSeen: string | null;
  staleAfterSeconds: number | null;
  message: string;
  evidence: string[];
};

export type SourceHealthSummary = {
  status: OpsHealthStatus;
  total: number;
  degraded: number;
  buckets: Record<SourceHealthBucket, SourceHealthItem[]>;
  worstBucket: SourceHealthBucket;
};

export type AssertionCard = {
  id: string;
  label: string;
  value: string;
  status: StateAssertion['status'];
  tone: CockpitTone;
  confidence: StateAssertion['confidence'];
  freshnessSeconds: number | null;
  evidence: string[];
  hasCorrectionHint: boolean;
};

export type CockpitLayoutModel = {
  generatedAt: string;
  contractVersion: string;
  overallHealth: {
    status: OpsHealthStatus;
    tone: CockpitTone;
    summary: string;
    degradedSources: string[];
    badAssertionCount: number;
    warnAssertionCount: number;
  };
  noteworthyDials: NoteworthyDial[];
  drawerSummaries: DrawerRailSummary[];
  railSummaries: DrawerRailSummary[];
  correctionQueue: CorrectionQueueItem[];
  sourceHealthSummary: SourceHealthSummary;
  assertionCards: AssertionCard[];
};

const SOURCE_LABELS: Record<string, string> = {
  agents_db: 'Agents DB',
  cron: 'Cron',
  desktop_attention: 'Desktop attention',
  enforcement: 'Enforcement',
  phone_activity: 'Phone activity',
  phone_heartbeat: 'Phone heartbeat',
  timer_engine: 'Timer engine',
  tmuxctld: 'tmuxctld',
  token_api: 'Token API',
  tts: 'TTS',
  work_state: 'Work state',
};

const SOURCE_ORDER = [
  'token_api',
  'tmuxctld',
  'agents_db',
  'timer_engine',
  'work_state',
  'desktop_attention',
  'phone_activity',
  'phone_heartbeat',
  'cron',
  'enforcement',
  'tts',
];

const EMPTY_BUCKETS = (): Record<SourceHealthBucket, SourceHealthItem[]> => ({
  bad: [],
  warn: [],
  stale: [],
  missing: [],
  unknown: [],
  fresh: [],
});

const BUCKET_ORDER: SourceHealthBucket[] = ['bad', 'warn', 'stale', 'missing', 'unknown', 'fresh'];

function labelForSource(id: string): string {
  return SOURCE_LABELS[id] ?? id.replace(/_/g, ' ');
}

function healthTone(status: OpsHealthStatus): CockpitTone {
  if (status === 'bad') return 'bad';
  if (status === 'warn') return 'warn';
  if (status === 'ok') return 'good';
  return 'warn';
}

function assertionTone(status: StateAssertion['status']): CockpitTone {
  if (status === 'bad') return 'bad';
  if (status === 'warn') return 'warn';
  if (status === 'good') return 'good';
  return 'neutral';
}

function bucketTone(bucket: SourceHealthBucket): CockpitTone {
  if (bucket === 'bad' || bucket === 'missing') return 'bad';
  if (bucket === 'warn' || bucket === 'stale' || bucket === 'unknown') return 'warn';
  return 'good';
}

function uniqueSourceIds(state: OpsState): string[] {
  const ids = new Set<string>();
  SOURCE_ORDER.forEach((id) => ids.add(id));
  Object.keys(state.sources ?? {}).forEach((id) => ids.add(id));
  Object.keys(state.source_freshness ?? {}).forEach((id) => ids.add(id));
  return [...ids].filter((id) => id in state.sources || id in state.source_freshness);
}

function sourceBucket(
  health: OpsSourceHealth | undefined,
  freshness: OpsSourceFreshness | undefined,
): SourceHealthBucket {
  if (health?.status === 'bad') return 'bad';
  if (freshness?.status === 'missing') return 'missing';
  if (health?.available === false && !freshness) return 'missing';
  if (health?.status === 'warn') return 'warn';
  if (freshness?.status === 'stale') return 'stale';
  if (health?.status === 'unknown' || freshness?.status === 'unknown') return 'unknown';
  if (health?.status === 'ok' || freshness?.status === 'fresh') return 'fresh';
  return 'unknown';
}

function sourceMessage(
  bucket: SourceHealthBucket,
  health: OpsSourceHealth | undefined,
  freshness: OpsSourceFreshness | undefined,
): string {
  return health?.message ?? freshness?.message ?? (bucket === 'fresh' ? 'fresh' : bucket);
}

function sourceEvidence(
  health: OpsSourceHealth | undefined,
  freshness: OpsSourceFreshness | undefined,
): string[] {
  return [health?.message, freshness?.message, ...(freshness?.evidence ?? [])]
    .filter((line): line is string => Boolean(line));
}

export function buildSourceHealthSummary(state: OpsState): SourceHealthSummary {
  const buckets = EMPTY_BUCKETS();
  for (const id of uniqueSourceIds(state)) {
    const health = state.sources[id as keyof OpsState['sources']] as OpsSourceHealth | undefined;
    const freshness = state.source_freshness[id as keyof OpsState['source_freshness']] as OpsSourceFreshness | undefined;
    const bucket = sourceBucket(health, freshness);
    buckets[bucket].push({
      id,
      label: labelForSource(id),
      bucket,
      tone: bucketTone(bucket),
      healthStatus: health?.status ?? null,
      freshnessStatus: freshness?.status ?? null,
      available: health?.available ?? null,
      ageSeconds: freshness?.age_seconds ?? null,
      lastSeen: freshness?.last_seen ?? null,
      staleAfterSeconds: freshness?.stale_after_seconds ?? null,
      message: sourceMessage(bucket, health, freshness),
      evidence: sourceEvidence(health, freshness),
    });
  }

  const total = BUCKET_ORDER.reduce((sum, bucket) => sum + buckets[bucket].length, 0);
  const degraded = total - buckets.fresh.length;
  const worstBucket = BUCKET_ORDER.find((bucket) => buckets[bucket].length > 0) ?? 'fresh';
  return { status: state.health.status, total, degraded, buckets, worstBucket };
}

export function buildCorrectionQueue(state: OpsState): CorrectionQueueItem[] {
  return (state.recommended_actions ?? []).map((action) => ({
    id: action.id,
    sourceAssertionId: action.source_assertion_id,
    severity: action.severity,
    tone: action.severity === 'bad' ? 'bad' : 'warn',
    label: action.label,
    action: action.action,
    evidence: action.evidence ?? [],
  }));
}

const ASSERTION_SORT: Record<CockpitTone, number> = { bad: 0, warn: 1, neutral: 2, good: 3 };

export function buildAssertionCards(state: OpsState): AssertionCard[] {
  return (state.assertions ?? [])
    .map((assertion) => ({
      id: assertion.id,
      label: assertion.label,
      value: assertion.value,
      status: assertion.status,
      tone: assertionTone(assertion.status),
      confidence: assertion.confidence,
      freshnessSeconds: assertion.freshness_seconds,
      evidence: assertion.evidence.slice(0, 2),
      hasCorrectionHint: Boolean(assertion.correction_hint),
    }))
    .sort((a, b) => ASSERTION_SORT[a.tone] - ASSERTION_SORT[b.tone] || a.label.localeCompare(b.label));
}

function plural(count: number, singular: string, pluralLabel = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralLabel}`;
}

function sourceBucketDial(bucket: SourceHealthBucket, count: number): NoteworthyDial | null {
  if (bucket === 'fresh' || count === 0) return null;
  const tone = bucketTone(bucket);
  const labels: Record<Exclude<SourceHealthBucket, 'fresh'>, string> = {
    bad: 'Bad source',
    warn: 'Warn source',
    stale: 'Stale source',
    missing: 'Missing source',
    unknown: 'Unknown source',
  };
  return {
    id: `sources.${bucket}`,
    label: labels[bucket],
    value: String(count),
    detail: bucket === 'missing' ? 'telemetry absent' : `${bucket} telemetry`,
    tone,
    source: 'sources',
    drawerId: `sources.${bucket}`,
    title: `${plural(count, 'source')} in ${bucket} bucket`,
    sort: { bad: 20, missing: 21, warn: 22, stale: 23, unknown: 24 }[bucket],
  };
}

export function buildNoteworthyDials(
  state: OpsState,
  sourceHealthSummary = buildSourceHealthSummary(state),
  correctionQueue = buildCorrectionQueue(state),
): NoteworthyDial[] {
  const dials: NoteworthyDial[] = [];

  if (state.health.status !== 'ok') {
    dials.push({
      id: 'health.overall',
      label: 'Health',
      value: state.health.status.toUpperCase(),
      detail: state.health.summary,
      tone: healthTone(state.health.status),
      source: 'health',
      drawerId: 'health.overall',
      title: `Ops health: ${state.health.summary}`,
      sort: 0,
    });
  }

  if (correctionQueue.length) {
    const bad = correctionQueue.filter((item) => item.severity === 'bad').length;
    dials.push({
      id: 'corrections.queue',
      label: 'Corrections',
      value: String(correctionQueue.length),
      detail: bad ? `${bad} bad` : 'warn',
      tone: bad ? 'bad' : 'warn',
      source: 'corrections',
      drawerId: 'corrections.queue',
      title: `${plural(correctionQueue.length, 'recommended correction')}`,
      sort: 10,
    });
  }

  for (const bucket of BUCKET_ORDER) {
    const dial = sourceBucketDial(bucket, sourceHealthSummary.buckets[bucket].length);
    if (dial) dials.push(dial);
  }

  if (state.health.bad_assertion_count > 0) {
    dials.push({
      id: 'assertions.bad',
      label: 'Bad assertions',
      value: String(state.health.bad_assertion_count),
      detail: 'backend assertions',
      tone: 'bad',
      source: 'assertions',
      drawerId: 'assertions.bad',
      title: `${plural(state.health.bad_assertion_count, 'bad assertion')}`,
      sort: 30,
    });
  }

  if (state.health.warn_assertion_count > 0) {
    dials.push({
      id: 'assertions.warn',
      label: 'Warn assertions',
      value: String(state.health.warn_assertion_count),
      detail: 'backend assertions',
      tone: 'warn',
      source: 'assertions',
      drawerId: 'assertions.warn',
      title: `${plural(state.health.warn_assertion_count, 'warn assertion')}`,
      sort: 31,
    });
  }

  return dials.sort((a, b) => a.sort - b.sort || a.id.localeCompare(b.id));
}

function sourceDrawerSummaries(sourceHealthSummary: SourceHealthSummary): DrawerRailSummary[] {
  return BUCKET_ORDER.map((bucket) => {
    const items = sourceHealthSummary.buckets[bucket];
    return {
      id: `sources.${bucket}`,
      kind: 'sources' as const,
      label: `${bucket[0].toUpperCase()}${bucket.slice(1)} sources`,
      count: items.length,
      tone: bucketTone(bucket),
      headline: items.length ? items.map((item) => item.label).join(', ') : `No ${bucket} sources`,
      detail: bucket === 'fresh' ? 'Telemetry currently fresh' : 'Telemetry remains visible until the backend reports recovery',
      itemIds: items.map((item) => item.id),
    };
  });
}

export function buildDrawerSummaries(
  state: OpsState,
  sourceHealthSummary = buildSourceHealthSummary(state),
  correctionQueue = buildCorrectionQueue(state),
  assertionCards = buildAssertionCards(state),
): DrawerRailSummary[] {
  const badAssertions = assertionCards.filter((item) => item.tone === 'bad');
  const warnAssertions = assertionCards.filter((item) => item.tone === 'warn');
  return [
    {
      id: 'health.overall',
      kind: 'health',
      label: 'Overall health',
      count: sourceHealthSummary.degraded,
      tone: healthTone(state.health.status),
      headline: state.health.summary,
      detail: state.health.degraded_sources.length
        ? `Degraded sources: ${state.health.degraded_sources.join(', ')}`
        : 'No degraded sources reported by backend health summary',
      itemIds: state.health.degraded_sources,
    },
    {
      id: 'corrections.queue',
      kind: 'corrections',
      label: 'Correction queue',
      count: correctionQueue.length,
      tone: correctionQueue.some((item) => item.severity === 'bad') ? 'bad' : correctionQueue.length ? 'warn' : 'good',
      headline: correctionQueue.length ? `${plural(correctionQueue.length, 'backend recommended action')}` : 'No backend recommended actions',
      detail: 'Queue is sourced only from OpsState.recommended_actions',
      itemIds: correctionQueue.map((item) => item.id),
    },
    ...sourceDrawerSummaries(sourceHealthSummary),
    {
      id: 'assertions.compact',
      kind: 'assertions',
      label: 'Assertion cards',
      count: assertionCards.length,
      tone: badAssertions.length ? 'bad' : warnAssertions.length ? 'warn' : assertionCards.length ? 'good' : 'neutral',
      headline: `${plural(badAssertions.length, 'bad assertion')} · ${plural(warnAssertions.length, 'warn assertion')}`,
      detail: 'Compact backend assertion facts for supporting evidence',
      itemIds: assertionCards.map((item) => item.id),
    },
  ];
}

export function buildCockpitLayoutModel(state: OpsState): CockpitLayoutModel {
  const sourceHealthSummary = buildSourceHealthSummary(state);
  const correctionQueue = buildCorrectionQueue(state);
  const assertionCards = buildAssertionCards(state);
  const noteworthyDials = buildNoteworthyDials(state, sourceHealthSummary, correctionQueue);
  const drawerSummaries = buildDrawerSummaries(state, sourceHealthSummary, correctionQueue, assertionCards);

  return {
    generatedAt: state.generated_at,
    contractVersion: state.contract_version,
    overallHealth: {
      status: state.health.status,
      tone: healthTone(state.health.status),
      summary: state.health.summary,
      degradedSources: state.health.degraded_sources,
      badAssertionCount: state.health.bad_assertion_count,
      warnAssertionCount: state.health.warn_assertion_count,
    },
    noteworthyDials,
    drawerSummaries,
    railSummaries: drawerSummaries,
    correctionQueue,
    sourceHealthSummary,
    assertionCards,
  };
}

export const createCockpitLayoutModel = buildCockpitLayoutModel;
