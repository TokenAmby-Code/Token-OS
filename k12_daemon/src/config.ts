// Daemon configuration (B1 config pattern — no hardcoded machine values).
//
// Every machine-specific value (machine identity, socket, data paths, port,
// bind) is env/config-driven. A JSON file pointed at by K12_DAEMON_CONFIG wins;
// otherwise env vars, otherwise the localhost-safe defaults below. `machine`
// has NO default — it must come from config or IMPERIUM_MACHINE (fail loud if
// the box identity is unknown; a daemon that guesses its own machine is a bug).

export type DaemonConfig = {
  bind: string;
  port: number;
  machine: string;
  /** Absolute path to the single append-only SQLite event-store file. */
  dbPath: string;
  /** The tmux socket name (`tmux -L <name>`) this daemon owns authoritatively. */
  tmuxSocket: string;
};

function envDefaults(): Partial<DaemonConfig> {
  return {
    bind: process.env.K12_DAEMON_BIND,
    port: process.env.K12_DAEMON_PORT ? Number(process.env.K12_DAEMON_PORT) : undefined,
    machine: process.env.IMPERIUM_MACHINE,
    dbPath: process.env.K12_DAEMON_DB,
    tmuxSocket: process.env.K12_DAEMON_TMUX_SOCKET,
  };
}

const HARD_DEFAULTS = {
  bind: '127.0.0.1',
  port: 7781,
  dbPath: `${process.env.HOME ?? ''}/runtimes/database/k12_daemon.events.sqlite`,
  tmuxSocket: 'k12',
} as const;

export function assertConfig(raw: Partial<DaemonConfig>): DaemonConfig {
  const env = envDefaults();
  const cfg: Partial<DaemonConfig> = {
    bind: raw.bind ?? env.bind ?? HARD_DEFAULTS.bind,
    port: raw.port ?? env.port ?? HARD_DEFAULTS.port,
    machine: raw.machine ?? env.machine, // NO hard default — must be known
    dbPath: raw.dbPath ?? env.dbPath ?? HARD_DEFAULTS.dbPath,
    tmuxSocket: raw.tmuxSocket ?? env.tmuxSocket ?? HARD_DEFAULTS.tmuxSocket,
  };

  if (!cfg.bind) throw new Error('k12_daemon config error: bind is required');
  if (!Number.isInteger(cfg.port) || cfg.port! < 1 || cfg.port! > 65535)
    throw new Error(`k12_daemon config error: invalid port ${cfg.port}`);
  if (!cfg.machine)
    throw new Error('k12_daemon config error: machine is required (set IMPERIUM_MACHINE or config.machine — the daemon must never guess its box identity)');
  if (!cfg.dbPath || !cfg.dbPath.startsWith('/'))
    throw new Error(`k12_daemon config error: dbPath must be an absolute path (got ${cfg.dbPath})`);
  if (!cfg.tmuxSocket) throw new Error('k12_daemon config error: tmuxSocket is required');

  return cfg as DaemonConfig;
}

export async function loadConfig(path = process.env.K12_DAEMON_CONFIG): Promise<DaemonConfig> {
  if (!path) return assertConfig({});
  const file = Bun.file(path);
  if (!(await file.exists())) throw new Error(`k12_daemon config error: missing config file ${path}`);
  return assertConfig(await file.json());
}
