// Entrypoint. Wires config → event store → tmux control plane → core → server.
// Source-run under Bun, no build step. systemd user unit owns the process.

import { loadConfig } from './config.ts';
import { EventStore } from './store.ts';
import { RealTmux } from './tmux.ts';
import { Daemon } from './core.ts';
import { makeServer, type BuildInfo } from './server.ts';

const build: BuildInfo = {
  version: '0.1.0',
  git_sha: process.env.GIT_SHA ?? 'unknown',
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

function shutdown() {
  server.stop(true);
  store.close();
  process.exit(0);
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
