import { allowed, loadConfig, type EdgeProxyConfig } from "./config";

const build = { service: "edge_proxy", version: "0.1.0", git_sha: process.env.GIT_SHA || "unknown", bun: Bun.version };

export function makeServer(cfg: EdgeProxyConfig) {
  const upstreamBase = new URL(cfg.upstream);
  return Bun.serve({
    hostname: cfg.bind,
    port: cfg.port,
    async fetch(req) {
      const url = new URL(req.url);
      if (url.pathname === "/health" && req.method === "GET") {
        let upstream: Record<string, unknown> = { reachable: false };
        try {
          const r = await fetch(new URL("/health", upstreamBase), { method: "GET" });
          upstream = { reachable: r.ok, status: r.status, body: await safeJson(r) };
        } catch (err) {
          upstream = { reachable: false, error: String(err) };
        }
        const ok = Boolean(upstream.reachable);
        return json({ ok, build, machine: cfg.machine, upstream }, ok ? 200 : 503);
      }
      if (!allowed(req.method, url.pathname, cfg.allowlist)) {
        console.error(JSON.stringify({ level: "error", event: "refused", method: req.method, path: url.pathname }));
        return json({ ok: false, error: "path_not_allowlisted", method: req.method, path: url.pathname }, 403);
      }
      const upstreamUrl = new URL(url.pathname + url.search, upstreamBase);
      try {
        const headers = new Headers(req.headers);
        headers.set("x-edge-proxy", "edge_proxy");
        headers.set("x-edge-proxy-machine", cfg.machine);
        headers.delete("host");
        const proxied = new Request(upstreamUrl, { method: req.method, headers, body: req.body, redirect: "manual" });
        const resp = await fetch(proxied);
        const outHeaders = new Headers(resp.headers);
        outHeaders.set("x-edge-proxy", "edge_proxy");
        return new Response(resp.body, { status: resp.status, statusText: resp.statusText, headers: outHeaders });
      } catch (err) {
        console.error(JSON.stringify({ level: "error", event: "upstream_unreachable", method: req.method, path: url.pathname, upstream: cfg.upstream, error: String(err) }));
        return json({ ok: false, error: "upstream_unreachable", detail: String(err) }, 502);
      }
    }
  });
}

async function safeJson(resp: Response) { try { return await resp.clone().json(); } catch { return undefined; } }
function json(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }); }

if (import.meta.main) {
  const cfg = await loadConfig();
  const server = makeServer(cfg);
  console.log(JSON.stringify({ level: "info", event: "listening", url: `http://${cfg.bind}:${cfg.port}`, upstream: cfg.upstream, build }));
  process.on("SIGTERM", () => server.stop(true));
  process.on("SIGINT", () => server.stop(true));
}
