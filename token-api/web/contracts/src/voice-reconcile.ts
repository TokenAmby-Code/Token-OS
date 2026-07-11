// Shared typed contract for the Discord voice draft-truth reconcile
// (`voice-drafts-reconcile.v1`). Mirrors the report returned by the discord
// daemon's `POST /voice/drafts/reconcile`, comparing the three copies of
// draft truth (daemon map, tmuxctld VOICE_SESSIONS, token-api dict).
//
// Same conventions as ops-state.v1: loose objects, non-spine fields optional.

import { z } from 'zod';

export const VOICE_RECONCILE_CONTRACT_VERSION = 'voice-drafts-reconcile.v1';

export type VoiceDraftOrphanSource = 'tmuxctld_session' | 'daemon_draft' | 'token_api_draft';

export type VoiceDraftOrphan = {
  source: VoiceDraftOrphanSource;
  bot_name: string;
  author_id: string;
  voice_session_id?: string | null;
  cleared: boolean;
};

export type VoiceReconcileReport = {
  contract_version: typeof VOICE_RECONCILE_CONTRACT_VERSION;
  auto_clear: boolean;
  counts: {
    daemon_drafts: number;
    tmuxctld_sessions: number;
    token_api_drafts: number;
  };
  sources: {
    tmuxctld: { ok: boolean; error?: string };
    token_api: { ok: boolean; error?: string };
  };
  orphans: VoiceDraftOrphan[];
  in_sync: boolean;
};

export const VoiceDraftOrphanSchema = z.looseObject({
  source: z.enum(['tmuxctld_session', 'daemon_draft', 'token_api_draft']),
  bot_name: z.string(),
  author_id: z.string(),
  voice_session_id: z.string().nullish(),
  cleared: z.boolean(),
});

export const ReconcileReportSchema = z.looseObject({
  contract_version: z.literal(VOICE_RECONCILE_CONTRACT_VERSION),
  auto_clear: z.boolean(),
  counts: z.looseObject({
    daemon_drafts: z.number(),
    tmuxctld_sessions: z.number(),
    token_api_drafts: z.number(),
  }),
  sources: z.looseObject({
    tmuxctld: z.looseObject({ ok: z.boolean(), error: z.string().nullish() }),
    token_api: z.looseObject({ ok: z.boolean(), error: z.string().nullish() }),
  }),
  orphans: z.array(VoiceDraftOrphanSchema),
  in_sync: z.boolean(),
});
