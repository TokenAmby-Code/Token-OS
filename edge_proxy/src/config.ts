export type AllowRule = { method?: string; path?: string; pathPrefix?: string };
export type EdgeProxyConfig = { bind: string; port: number; upstream: string; machine: string; allowlist: AllowRule[] };

const DEFAULTS: EdgeProxyConfig = {
  bind: process.env.EDGE_PROXY_BIND || "127.0.0.1",
  port: Number(process.env.EDGE_PROXY_PORT || "7780"),
  upstream: process.env.EDGE_PROXY_UPSTREAM || process.env.TOKEN_API_URL || "http://127.0.0.1:7777",
  machine: process.env.IMPERIUM_MACHINE || "auto",
  allowlist: [
    { method: "GET", path: "/openapi.json" },
    { method: "GET", pathPrefix: "/api/" },
    { method: "POST", pathPrefix: "/api/hooks/" }
  ]
};

function assertConfig(raw: Partial<EdgeProxyConfig>): EdgeProxyConfig {
  const cfg = { ...DEFAULTS, ...raw };
  if (!cfg.bind) throw new Error("edge_proxy config error: bind is required");
  if (!Number.isInteger(cfg.port) || cfg.port < 1 || cfg.port > 65535) throw new Error(`edge_proxy config error: invalid port ${cfg.port}`);
  try { new URL(cfg.upstream); } catch { throw new Error(`edge_proxy config error: invalid upstream ${cfg.upstream}`); }
  if (!Array.isArray(cfg.allowlist) || cfg.allowlist.length === 0) throw new Error("edge_proxy config error: allowlist must be non-empty");
  for (const [i, rule] of cfg.allowlist.entries()) {
    if (!rule.path && !rule.pathPrefix) throw new Error(`edge_proxy config error: allowlist[${i}] needs path or pathPrefix`);
    if (rule.path && !rule.path.startsWith("/")) throw new Error(`edge_proxy config error: allowlist[${i}].path must start with /`);
    if (rule.pathPrefix && !rule.pathPrefix.startsWith("/")) throw new Error(`edge_proxy config error: allowlist[${i}].pathPrefix must start with /`);
  }
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
