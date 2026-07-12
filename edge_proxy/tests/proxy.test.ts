import { afterEach, expect, test } from "bun:test";
import { allowed, assertConfig, forwardPath, resolveRoute, type RouteConfig } from "../src/config";
import { makeServer } from "../src/server";

const servers: ReturnType<typeof Bun.serve>[] = [];
afterEach(() => { while (servers.length) servers.pop()!.stop(true); });

function port() { return 20000 + Math.floor(Math.random() * 20000); }

test("allowlist matches exact and prefix", () => {
  expect(allowed("GET", "/openapi.json", [{ method: "GET", path: "/openapi.json" }])).toBe(true);
  expect(allowed("POST", "/api/hooks/Stop", [{ method: "POST", pathPrefix: "/api/hooks/" }])).toBe(true);
  expect(allowed("DELETE", "/api/hooks/Stop", [{ method: "POST", pathPrefix: "/api/hooks/" }])).toBe(false);
});

test("resolveRoute picks the longest matching prefix, default catches the rest", () => {
  const routes: RouteConfig[] = [
    { prefix: "/k12", upstream: "http://127.0.0.1:7781", stripPrefix: true, allowlist: [{ pathPrefix: "/" }] },
    { prefix: "/", upstream: "http://127.0.0.1:7777", allowlist: [{ pathPrefix: "/" }] },
  ];
  expect(resolveRoute("/k12/health", routes)?.upstream).toBe("http://127.0.0.1:7781");
  expect(resolveRoute("/k12", routes)?.upstream).toBe("http://127.0.0.1:7781");
  expect(resolveRoute("/api/echo", routes)?.upstream).toBe("http://127.0.0.1:7777");
  // Boundary: /k12x must NOT match the /k12 route.
  expect(resolveRoute("/k12x/y", routes)?.upstream).toBe("http://127.0.0.1:7777");
});

test("forwardPath strips the route prefix only when asked", () => {
  const k12: RouteConfig = { prefix: "/k12", upstream: "u", stripPrefix: true, allowlist: [] };
  expect(forwardPath("/k12/health", k12)).toBe("/health");
  expect(forwardPath("/k12", k12)).toBe("/");
  const root: RouteConfig = { prefix: "/", upstream: "u", allowlist: [] };
  expect(forwardPath("/api/echo", root)).toBe("/api/echo");
});

test("legacy single-upstream config normalizes to one default route", () => {
  const cfg = assertConfig({ bind: "127.0.0.1", port: 7780, machine: "test", upstream: "http://127.0.0.1:7777", allowlist: [{ pathPrefix: "/api/" }] } as any);
  expect(cfg.routes.length).toBe(1);
  expect(cfg.routes[0]!.prefix).toBe("/");
  expect(cfg.routes[0]!.upstream).toBe("http://127.0.0.1:7777");
});

test("per-route forwarding sends /k12/* to the daemon upstream (stripped) and the rest to token-api", async () => {
  const daemonPort = port();
  const apiPort = port();
  const proxyPort = port();
  servers.push(Bun.serve({ hostname: "127.0.0.1", port: daemonPort, fetch(req) {
    const u = new URL(req.url);
    return Response.json({ who: "daemon", path: u.pathname, via: req.headers.get("x-edge-proxy") });
  }}));
  servers.push(Bun.serve({ hostname: "127.0.0.1", port: apiPort, fetch(req) {
    const u = new URL(req.url);
    return Response.json({ who: "api", path: u.pathname });
  }}));
  servers.push(makeServer({ bind: "127.0.0.1", port: proxyPort, machine: "test", routes: [
    { prefix: "/k12", upstream: `http://127.0.0.1:${daemonPort}`, stripPrefix: true, allowlist: [{ method: "GET", path: "/health" }] },
    { prefix: "/", upstream: `http://127.0.0.1:${apiPort}`, allowlist: [{ method: "GET", pathPrefix: "/api/" }] },
  ] }));
  let r = await fetch(`http://127.0.0.1:${proxyPort}/k12/health`);
  expect(await r.json()).toEqual({ who: "daemon", path: "/health", via: "edge_proxy" });
  r = await fetch(`http://127.0.0.1:${proxyPort}/api/echo`);
  expect(await r.json()).toEqual({ who: "api", path: "/api/echo" });
});

test("route-scoped auth: a route token gates only its own route", async () => {
  const daemonPort = port();
  const apiPort = port();
  const proxyPort = port();
  servers.push(Bun.serve({ hostname: "127.0.0.1", port: daemonPort, fetch() { return Response.json({ who: "daemon" }); } }));
  servers.push(Bun.serve({ hostname: "127.0.0.1", port: apiPort, fetch() { return Response.json({ who: "api" }); } }));
  servers.push(makeServer({ bind: "127.0.0.1", port: proxyPort, machine: "test", routes: [
    { prefix: "/k12", upstream: `http://127.0.0.1:${daemonPort}`, stripPrefix: true, token: "s3cret", allowlist: [{ method: "GET", path: "/health" }] },
    { prefix: "/", upstream: `http://127.0.0.1:${apiPort}`, allowlist: [{ method: "GET", pathPrefix: "/api/" }] },
  ] }));
  // Missing cred on the guarded route → 401.
  let r = await fetch(`http://127.0.0.1:${proxyPort}/k12/health`);
  expect(r.status).toBe(401);
  // Correct cred → forwarded.
  r = await fetch(`http://127.0.0.1:${proxyPort}/k12/health`, { headers: { authorization: "Bearer s3cret" } });
  expect(r.status).toBe(200);
  // The k12 token does NOT grant the default route (which requires none, still works without it).
  r = await fetch(`http://127.0.0.1:${proxyPort}/api/echo`);
  expect(r.status).toBe(200);
});

test("forward, refuse non-allowlisted, and fail loud when upstream dies", async () => {
  const upPort = port();
  const proxyPort = port();
  servers.push(Bun.serve({ hostname: "127.0.0.1", port: upPort, fetch(req) {
    const u = new URL(req.url);
    if (u.pathname === "/api/echo") return Response.json({ method: req.method, via: req.headers.get("x-edge-proxy") });
    return new Response("missing", { status: 404 });
  }}));
  servers.push(makeServer({ bind: "127.0.0.1", port: proxyPort, machine: "test", routes: [
    { prefix: "/", upstream: `http://127.0.0.1:${upPort}`, allowlist: [{ method: "GET", pathPrefix: "/api/" }] },
  ] }));
  let r = await fetch(`http://127.0.0.1:${proxyPort}/api/echo`);
  expect(r.status).toBe(200);
  expect(await r.json()).toEqual({ method: "GET", via: "edge_proxy" });
  r = await fetch(`http://127.0.0.1:${proxyPort}/nope`);
  expect(r.status).toBe(403);
  servers.shift()!.stop(true);
  r = await fetch(`http://127.0.0.1:${proxyPort}/api/echo`);
  expect(r.status).toBe(502);
});
