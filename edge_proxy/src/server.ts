import { allowed, forwardPath, loadConfig, resolveRoute, type EdgeProxyConfig, type RouteConfig } from "./config";

const build = { service: "edge_proxy", version: "0.1.0", git_sha: process.env.GIT_SHA || "unknown", bun: Bun.version };

function authorized(req: Request, route: RouteConfig): boolean {
  if (!route.token) return true;
  return req.headers.get("authorization") === `Bearer ${route.token}`;
}

export function makeServer(cfg: EdgeProxyConfig) {
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
        headers.delete("authorization"); // route cred is proxy-terminated, never forwarded upstream
        const proxied = new Request(upstreamUrl, { method: req.method, headers, body: req.body, redirect: "manual" });
        const resp = await fetch(proxied);
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
  const upstreams: Record<string, unknown>[] = [];
  let ok = true;
  for (const route of cfg.routes) {
    try {
      const r = await fetch(new URL("/health", route.upstream), { method: "GET" });
      upstreams.push({ prefix: route.prefix, upstream: route.upstream, reachable: r.ok, status: r.status });
      if (!r.ok) ok = false;
    } catch (err) {
      upstreams.push({ prefix: route.prefix, upstream: route.upstream, reachable: false, error: String(err) });
      ok = false;
    }
  }
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
