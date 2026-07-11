// Shared typed contract for the Discord voice self-test probe
// (`voice-selftest.v1`). Mirrors the report returned by the discord daemon's
// `POST /voice/selftest` and `GET /voice/selftest/last`, logged into the
// token-api events table, and posted to the alerts channel on fail/degraded.
//
// Same conventions as ops-state.v1: loose objects pass unknown keys through
// and non-spine fields are optional, so the contract never rejects fields the
// daemon later grows. A schema miss is an advisory log at the consumer.

import { z } from 'zod';

export const VOICE_SELFTEST_CONTRACT_VERSION = 'voice-selftest.v1';

export type VoiceSelftestVariant = 'seams' | 'full';
export type VoiceSelftestOverall = 'pass' | 'degraded' | 'fail' | 'aborted';

export type VoiceSelftestStage = {
  stage: string;
  ok: boolean;
  ms: number;
  errorCode?: string | null;
  detail?: string | null;
};

export type VoiceSelftestTranscriptMatch = {
  matched: boolean;
  matched_tokens: number;
  total_tokens: number;
  attempts: number;
  passed_on_retry: boolean;
  transcript?: string | null;
};

export type VoiceSelftestReport = {
  contract_version: typeof VOICE_SELFTEST_CONTRACT_VERSION;
  probe_id: string;
  variant: VoiceSelftestVariant;
  trigger: string;
  started_at: string;
  finished_at: string;
  duration_ms: number;
  overall: VoiceSelftestOverall;
  abort_reason?: string | null;
  first_failed_stage?: string | null;
  stages: VoiceSelftestStage[];
  transcript_match?: VoiceSelftestTranscriptMatch | null;
  daemon?: {
    version?: string | null;
    pid?: number | null;
    node?: string | null;
  } | null;
};

export const SelftestStageSchema = z.looseObject({
  stage: z.string(),
  ok: z.boolean(),
  ms: z.number(),
  errorCode: z.string().nullish(),
  detail: z.string().nullish(),
});

export const SelftestTranscriptMatchSchema = z.looseObject({
  matched: z.boolean(),
  matched_tokens: z.number(),
  total_tokens: z.number(),
  attempts: z.number(),
  passed_on_retry: z.boolean(),
  transcript: z.string().nullish(),
});

export const SelftestReportSchema = z.looseObject({
  contract_version: z.literal(VOICE_SELFTEST_CONTRACT_VERSION),
  probe_id: z.string(),
  variant: z.enum(['seams', 'full']),
  trigger: z.string(),
  started_at: z.string(),
  finished_at: z.string(),
  duration_ms: z.number(),
  overall: z.enum(['pass', 'degraded', 'fail', 'aborted']),
  abort_reason: z.string().nullish(),
  first_failed_stage: z.string().nullish(),
  stages: z.array(SelftestStageSchema),
  transcript_match: SelftestTranscriptMatchSchema.nullish(),
  daemon: z
    .looseObject({
      version: z.string().nullish(),
      pid: z.number().nullish(),
      node: z.string().nullish(),
    })
    .nullish(),
});
