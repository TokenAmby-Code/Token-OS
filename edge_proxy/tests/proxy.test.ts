import { afterEach, expect, test } from "bun:test";
import { allowed } from "../src/config";
import { makeServer } from "../src/server";

const servers: ReturnType<typeof Bun.serve>[] = [];
afterEach(() => { while (servers.length) servers.pop()!.stop(true); });

function port() { return 20000 + Math.floor(Math.random() * 20000); }

test("allowlist matches exact and prefix", () => {
  expect(allowed("GET", "/openapi.json", [{ method: "GET", path: "/openapi.json" }])).toBe(true);
  expect(allowed("POST", "/api/hooks/Stop", [{ method: "POST", pathPrefix: "/api/hooks/" }])).toBe(true);
  expect(allowed("DELETE", "/api/hooks/Stop", [{ method: "POST", pathPrefix: "/api/hooks/" }])).toBe(false);
});

test("forward, refuse, and fail loud", async () => {
  const upPort = port();
  const proxyPort = port();
  servers.push(Bun.serve({ hostname: "127.0.0.1", port: upPort, fetch(req) {
    const u = new URL(req.url);
    if (u.pathname === "/health") return Response.json({ ok: true });
    if (u.pathname === "/api/echo") return Response.json({ method: req.method, via: req.headers.get("x-edge-proxy") });
    return new Response("missing", { status: 404 });
  }}));
  servers.push(makeServer({ bind: "127.0.0.1", port: proxyPort, upstream: `http://127.0.0.1:${upPort}`, machine: "test", allowlist: [{ method: "GET", pathPrefix: "/api/" }] }));
  let r = await fetch(`http://127.0.0.1:${proxyPort}/api/echo`);
  expect(r.status).toBe(200);
  expect(await r.json()).toEqual({ method: "GET", via: "edge_proxy" });
  r = await fetch(`http://127.0.0.1:${proxyPort}/nope`);
  expect(r.status).toBe(403);
  servers.shift()!.stop(true);
  r = await fetch(`http://127.0.0.1:${proxyPort}/api/echo`);
  expect(r.status).toBe(502);
});
