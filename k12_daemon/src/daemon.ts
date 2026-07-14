// Entrypoint. Wires config → event store → tmux control plane → core → server.
// Source-run under Bun, no build step. systemd user unit owns the process.

import { loadConfig } from './config.ts';
import { EventStore } from './store.ts';
import { RealTmux } from './tmux.ts';
import { Daemon } from './core.ts';
import { makeServer, type BuildInfo } from './server.ts';
import { resolveGitSha } from './build.ts';

const build: BuildInfo = {
  version: '0.1.0',
  // Resolved from the checkout this file was loaded from (src/ → package dir);
  // rev-parse walks up to the repo root, so the daemon subdir is sufficient.
  git_sha: resolveGitSha(new URL('..', import.meta.url).pathname),
  bun: Bun.version,
};

const cfg = await loadConfig();
const store = new EventStore(cfg.dbPath);
const tmux = new RealTmux(cfg.tmuxSocket);
const daemon = new Daemon(store, tmux);
const server = makeServer({ bind: cfg.bind, port: cfg.port, daemon, build, machine: cfg.machine });

console.log(
  JSON.stringify({
    level: 'info',
    event: 'listening',
    url: `http://${cfg.bind}:${cfg.port}`,
    machine: cfg.machine,
    db: cfg.dbPath,
    tmux_socket: cfg.tmuxSocket,
    build,
  }),
);

// Stand the canonical persistent estate declaratively (rung 2). Idempotent and
// best-effort: constructEstate swallows per-seat errors internally, so this can
// never crash boot — a partial estate is logged, not fatal.
const est = await daemon.constructEstate();
// Structured logs go to stderr here as elsewhere in the daemon (core.ts).
console.error(
  JSON.stringify({
    level: 'info',
    event: 'estate_constructed',
    created: est.created.length,
    existing: est.existing.length,
    backfilled: est.backfilled.length,
    failed: est.failed.length,
    created_seats: est.created,
    backfilled_seats: est.backfilled,
  }),
);

async function shutdown() {
  // Graceful, but bounded: let in-flight requests finish, yet never let a stuck
  // request block termination — close the store and exit after 5s regardless.
  await Promise.race([server.stop(), Bun.sleep(5_000)]);
  store.close();
  process.exit(0);
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
