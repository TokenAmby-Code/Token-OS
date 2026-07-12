// HTTP surface (spec §7). Six honest endpoints; collection routes registered
// BEFORE parameterized ones (the `/api/instances/all` lesson) — the ordering is
// data (the exported route table) so the committed route-shadow test can assert it.
//
// Ingress is via localhost edge_proxy ONLY (day-one purity). The daemon still
// binds loopback and treats the `x-edge-proxy` header as the transport receipt
// woven into event provenance (hook-never-fired vs swallowed-after-arrival).

import {
  LaunchRequestSchema,
  SendRequestSchema,
  type EntitiesResponse,
  type EntityEventsResponse,
} from '@token-os/contracts';
import type { Daemon } from './core.ts';
import { assertNoTmuxId } from './ids.ts';

export type BuildInfo = { version: string; git_sha: string; bun: string };

export type Route = {
  method: string;
  /** Exact match, or a matcher returning captured params (null = no match). */
  match: (pathname: string) => Record<string, string> | null;
  label: string;
  handler: (req: Request, params: Record<string, string>) => Promise<Response>;
};

function json(body: unknown, status = 200): Response {
  // Canonical-id membrane enforcement: nothing crosses upward carrying a raw
  // tmux id. A breach fails loud rather than leaking.
  assertNoTmuxId(body, 'http_response');
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function exact(path: string) {
  return (pathname: string) => (pathname === path ? {} : null);
}

async function readJson(req: Request): Promise<unknown> {
  try {
    return await req.json();
  } catch {
    return undefined;
  }
}

function receipt(req: Request): string | null {
  return req.headers.get('x-edge-proxy');
}

// Ordered route table. Collection routes come first; the parameterized
// `/entities/:id/events` route is LAST among the /entities family so it can
// never shadow the collection.
export function buildRoutes(daemon: Daemon, build: BuildInfo, machine: string): Route[] {
  return [
    {
      method: 'GET',
      match: exact('/health'),
      label: 'GET /health',
      handler: async () => {
        const h = await daemon.health(machine, build);
        return json(h, h.ok ? 200 : 503);
      },
    },
    {
      method: 'POST',
      match: exact('/launch'),
      label: 'POST /launch',
      handler: async (req) => {
        const parsed = LaunchRequestSchema.safeParse(await readJson(req));
        if (!parsed.success) return json({ ok: false, error: 'invalid_launch_request', detail: parsed.error.issues }, 422);
        const res = await daemon.launch(parsed.data, receipt(req));
        return json(res, res.handover ? 200 : 409);
      },
    },
    {
      method: 'POST',
      match: exact('/send'),
      label: 'POST /send',
      handler: async (req) => {
        const parsed = SendRequestSchema.safeParse(await readJson(req));
        if (!parsed.success) return json({ ok: false, error: 'invalid_send_request', detail: parsed.error.issues }, 422);
        const res = await daemon.send(parsed.data, receipt(req));
        // Admission refusal fails loud (not admitted); gated/delivered are 200.
        if ('refused' in res) return json(res, 422);
        return json(res, 200);
      },
    },
    {
      method: 'POST',
      match: exact('/reconcile'),
      label: 'POST /reconcile',
      handler: async (req) => {
        const res = await daemon.reconcile(receipt(req));
        // Bring-up mode: p0 contradiction ⇒ fail loud with a non-2xx.
        return json(res, res.p0 ? 409 : 200);
      },
    },
    // COLLECTION before PARAMETERIZED — order is load-bearing.
    {
      method: 'GET',
      match: exact('/entities'),
      label: 'GET /entities',
      handler: async () => {
        const body: EntitiesResponse = { schema_version: 1, rows: daemon.entities() };
        return json(body);
      },
    },
    {
      method: 'GET',
      match: (pathname) => {
        const m = /^\/entities\/([^/]+)\/events$/.exec(pathname);
        return m ? { id: decodeURIComponent(m[1]!) } : null;
      },
      label: 'GET /entities/:id/events',
      handler: async (_req, params) => {
        const body: EntityEventsResponse = { entity_id: params.id!, events: daemon.entityEvents(params.id!) };
        return json(body);
      },
    },
  ];
}

export function makeServer(opts: { bind: string; port: number; daemon: Daemon; build: BuildInfo; machine: string }): ReturnType<typeof Bun.serve> {
  const routes = buildRoutes(opts.daemon, opts.build, opts.machine);
  return Bun.serve({
    hostname: opts.bind,
    port: opts.port,
    async fetch(req) {
      const url = new URL(req.url);
      for (const route of routes) {
        if (route.method !== req.method) continue;
        const params = route.match(url.pathname);
        if (!params) continue;
        try {
          return await route.handler(req, params);
        } catch (err) {
          console.error(JSON.stringify({ level: 'error', event: 'handler_error', route: route.label, error: String(err) }));
          // Generic body: the full error stays in the server log only. Serializing
          // String(err) could echo a raw %id back through the membrane (assertNoTmuxId).
          return json({ ok: false, error: 'internal_error' }, 500);
        }
      }
      return json({ ok: false, error: 'not_found', method: req.method, path: url.pathname }, 404);
    },
  });
}
