import { allowed, forwardPath, loadConfig, resolveRoute, type EdgeProxyConfig, type RouteConfig } from "./config";

const build = { service: "edge_proxy", version: "0.1.0", git_sha: process.env.GIT_SHA || "unknown", bun: Bun.version };

// A hung (not merely down) upstream must not wedge the proxy forever.
const UPSTREAM_TIMEOUT_MS = 30_000;
const HEALTH_PROBE_TIMEOUT_MS = 2_000;

function authorized(req: Request, route: RouteConfig): boolean {
  if (!route.token) return true;
  return req.headers.get("authorization") === `Bearer ${route.token}`;
}

export function makeServer(cfg: EdgeProxyConfig): ReturnType<typeof Bun.serve> {
  return Bun.serve({
    hostname: cfg.bind,
    port: cfg.port,
    async fetch(req) {
      const url = new URL(req.url);
      // Proxy's OWN liveness — must resolve before routing, so it lives here (admission test ✓).
      if (url.pathname === "/health" && req.method === "GET") {
        return healthResponse(cfg);
      }
      const route = resolveRoute(url.pathname, cfg.routes);
      if (!route) {
        console.error(JSON.stringify({ level: "error", event: "no_route", method: req.method, path: url.pathname }));
        return json({ ok: false, error: "no_route", method: req.method, path: url.pathname }, 404);
      }
      // Route-scoped auth (§12): a route cred gates only its own route.
      if (!authorized(req, route)) {
        console.error(JSON.stringify({ level: "error", event: "unauthorized", method: req.method, path: url.pathname }));
        return json({ ok: false, error: "unauthorized", method: req.method, path: url.pathname }, 401);
      }
      const fwdPath = forwardPath(url.pathname, route);
      if (!allowed(req.method, fwdPath, route.allowlist)) {
        console.error(JSON.stringify({ level: "error", event: "refused", method: req.method, path: url.pathname }));
        return json({ ok: false, error: "path_not_allowlisted", method: req.method, path: url.pathname }, 403);
      }
      const upstreamUrl = new URL(fwdPath + url.search, route.upstream);
      try {
        const headers = new Headers(req.headers);
        headers.set("x-edge-proxy", "edge_proxy");
        headers.set("x-edge-proxy-machine", cfg.machine);
        headers.delete("host");
        // A ROUTE cred is proxy-terminated, never forwarded upstream. A tokenless
        // route passes the caller's Authorization through untouched — upstream
        // endpoints that do their own bearer auth (e.g. token-api /api/cd/restart,
        // fail-closed on CD_RESTART_SECRET) stay the auth authority.
        if (route.token) headers.delete("authorization");
        // duplex:"half" is required by Bun/WHATWG fetch when streaming a request body.
        const init: RequestInit = { method: req.method, headers, body: req.body, redirect: "manual" };
        if (req.body) (init as RequestInit & { duplex: "half" }).duplex = "half";
        const proxied = new Request(upstreamUrl, init);
        const resp = await fetch(proxied, { signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS) });
        const outHeaders = new Headers(resp.headers);
        outHeaders.set("x-edge-proxy", "edge_proxy");
        return new Response(resp.body, { status: resp.status, statusText: resp.statusText, headers: outHeaders });
      } catch (err) {
        console.error(JSON.stringify({ level: "error", event: "upstream_unreachable", method: req.method, path: url.pathname, upstream: route.upstream, error: String(err) }));
        return json({ ok: false, error: "upstream_unreachable", detail: String(err) }, 502);
      }
    },
  });
}

async function healthResponse(cfg: EdgeProxyConfig): Promise<Response> {
  // Probe all upstreams in parallel with a bounded timeout — a hung upstream
  // must not stall the liveness endpoint the way a sequential un-timed loop would.
  const upstreams = await Promise.all(cfg.routes.map(async (route) => {
    try {
      const r = await fetch(new URL("/health", route.upstream), { method: "GET", signal: AbortSignal.timeout(HEALTH_PROBE_TIMEOUT_MS) });
      return { prefix: route.prefix, upstream: route.upstream, reachable: r.ok, status: r.status };
    } catch (err) {
      return { prefix: route.prefix, upstream: route.upstream, reachable: false, error: String(err) };
    }
  }));
  const ok = upstreams.every((u) => u.reachable);
  return json({ ok, build, machine: cfg.machine, upstreams }, ok ? 200 : 503);
}

function json(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }); }

if (import.meta.main) {
  const cfg = await loadConfig();
  const server = makeServer(cfg);
  console.log(JSON.stringify({ level: "info", event: "listening", url: `http://${cfg.bind}:${cfg.port}`, routes: cfg.routes.map((r) => ({ prefix: r.prefix, upstream: r.upstream })), build }));
  process.on("SIGTERM", () => server.stop(true));
  process.on("SIGINT", () => server.stop(true));
}
