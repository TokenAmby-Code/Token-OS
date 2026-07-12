export type AllowRule = { method?: string; path?: string; pathPrefix?: string };

// One upstream behind one path prefix. The box's front door fans requests out
// across these by longest-prefix match (spec §12: one edge proxy per box, with
// upstream routes). The proxy stays DUMB — routing, auth, and admission
// (allowlist) only; never per-upstream business logic.
export type RouteConfig = {
  /** Path prefix that selects this upstream. Longest match wins; "/" is the catch-all. */
  prefix: string;
  /** Upstream base URL for requests matching this prefix. */
  upstream: string;
  /** Strip `prefix` from the path before forwarding (the upstream serves its own paths). */
  stripPrefix?: boolean;
  /** Route-scoped allowlist, matched against the forwarded (post-strip) path. */
  allowlist: AllowRule[];
  /** Optional route-scoped credential. When set, requests must present
   *  `Authorization: Bearer <token>`. Route-scoped by construction — one route's
   *  cred never grants another route or the box (spec §12: route-scoped auth). */
  token?: string;
};

export type EdgeProxyConfig = { bind: string; port: number; machine: string; routes: RouteConfig[] };

// Legacy single-upstream shape, kept for back-compat with existing config files.
type LegacyConfig = { upstream?: string; allowlist?: AllowRule[] };

const ENV = {
  bind: process.env.EDGE_PROXY_BIND || "127.0.0.1",
  port: Number(process.env.EDGE_PROXY_PORT || "7780"),
  machine: process.env.IMPERIUM_MACHINE || "auto",
};

function defaultUpstream(): string {
  return process.env.EDGE_PROXY_UPSTREAM || process.env.TOKEN_API_URL || "http://127.0.0.1:7777";
}

function defaultRoutes(): RouteConfig[] {
  return [
    {
      prefix: "/",
      upstream: defaultUpstream(),
      allowlist: [
        { method: "GET", path: "/openapi.json" },
        { method: "GET", pathPrefix: "/api/" },
        { method: "POST", pathPrefix: "/api/hooks/" },
      ],
    },
  ];
}

function assertRoute(r: RouteConfig, i: number): RouteConfig {
  if (!r.prefix || !r.prefix.startsWith("/")) throw new Error(`edge_proxy config error: routes[${i}].prefix must start with /`);
  try { new URL(r.upstream); } catch { throw new Error(`edge_proxy config error: routes[${i}].upstream invalid: ${r.upstream}`); }
  if (!Array.isArray(r.allowlist) || r.allowlist.length === 0) throw new Error(`edge_proxy config error: routes[${i}].allowlist must be non-empty`);
  for (const [j, rule] of r.allowlist.entries()) {
    if (!rule.path && !rule.pathPrefix) throw new Error(`edge_proxy config error: routes[${i}].allowlist[${j}] needs path or pathPrefix`);
    if (rule.path && !rule.path.startsWith("/")) throw new Error(`edge_proxy config error: routes[${i}].allowlist[${j}].path must start with /`);
    if (rule.pathPrefix && !rule.pathPrefix.startsWith("/")) throw new Error(`edge_proxy config error: routes[${i}].allowlist[${j}].pathPrefix must start with /`);
  }
  return r;
}

export function assertConfig(raw: Partial<EdgeProxyConfig> & LegacyConfig): EdgeProxyConfig {
  // Back-compat: a single-upstream config (no `routes`) folds into one "/" route.
  let routes = raw.routes;
  if (!routes && (raw.upstream || raw.allowlist)) {
    routes = [{ prefix: "/", upstream: raw.upstream ?? defaultUpstream(), allowlist: raw.allowlist ?? defaultRoutes()[0]!.allowlist }];
  }
  const cfg: EdgeProxyConfig = {
    bind: raw.bind ?? ENV.bind,
    port: raw.port ?? ENV.port,
    machine: raw.machine ?? ENV.machine,
    routes: routes ?? defaultRoutes(),
  };
  if (!cfg.bind) throw new Error("edge_proxy config error: bind is required");
  if (!Number.isInteger(cfg.port) || cfg.port < 1 || cfg.port > 65535) throw new Error(`edge_proxy config error: invalid port ${cfg.port}`);
  if (!Array.isArray(cfg.routes) || cfg.routes.length === 0) throw new Error("edge_proxy config error: routes must be non-empty");
  cfg.routes.forEach(assertRoute);
  // Longest prefix first so the catch-all "/" never shadows a specific route.
  cfg.routes.sort((a, b) => b.prefix.length - a.prefix.length);
  return cfg;
}

export async function loadConfig(path = process.env.EDGE_PROXY_CONFIG): Promise<EdgeProxyConfig> {
  if (!path) return assertConfig({});
  const file = Bun.file(path);
  if (!(await file.exists())) throw new Error(`edge_proxy config error: missing ${path}`);
  return assertConfig(await file.json());
}

export function allowed(method: string, pathname: string, rules: AllowRule[]): boolean {
  const m = method.toUpperCase();
  return rules.some((r) => (!r.method || r.method.toUpperCase() === m) && ((r.path && r.path === pathname) || (r.pathPrefix && pathname.startsWith(r.pathPrefix))));
}

/** Select the route for a path: exact prefix or prefix + "/" boundary; "/" is the catch-all.
 *  Assumes routes are ordered longest-prefix-first (assertConfig guarantees this). */
export function resolveRoute(pathname: string, routes: RouteConfig[]): RouteConfig | null {
  return routes.find((r) => r.prefix === "/" || pathname === r.prefix || pathname.startsWith(r.prefix + "/")) ?? null;
}

/** Path handed to the upstream: prefix stripped when the route asks, else unchanged. */
export function forwardPath(pathname: string, route: RouteConfig): string {
  if (!route.stripPrefix || route.prefix === "/") return pathname;
  const rest = pathname.slice(route.prefix.length);
  return rest.startsWith("/") ? rest : "/" + rest;
}
