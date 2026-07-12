// Authoritative tmux control plane (spec §7 rung 2) + canonical-id membrane.
//
// The daemon owns ONE tmux server (`tmux -L <socket>`). Canonical ids (seat
// names, colons and all) live ONLY in the `@canonical_id` pane option — never
// as a tmux target (a `somnium:NE` session name would collide with tmux's `:`
// target syntax). Everything ABOVE this membrane speaks canonical ids; raw
// `%id`/`@id`/`$id` never crosses upward. Below the membrane we resolve a
// canonical id to its `%id` internally to operate, and discard it.
//
// The interface is injectable so tests run against an in-memory fake with zero
// tmux dependency; on-box acceptance exercises the real plane.

export type SeatObservation = { seat_id: string; pane: 'live' | 'dead' };

// Below-membrane delivery outcome. `partial_delivered` = the literal text reached
// the pane but the submit (Enter) did not — first-class, never collapsed to failure.
export type SendOutcome = { bytes: number; verdict: 'delivered' | 'partial_delivered' | 'failed_none_delivered' };

export interface TmuxControlPlane {
  reachable(): Promise<boolean>;
  version(): Promise<string | null>;
  /** Live seats as canonical ids + pane liveness. Never exposes %id. */
  listSeats(): Promise<SeatObservation[]>;
  /** Create a bare seat: a single-pane session tagged with the canonical id. */
  createSeat(seatId: string): Promise<void>;
  /** Kill the seat's pane (teardown). Idempotent. */
  killSeat(seatId: string): Promise<void>;
  /**
   * Canonical ids of seats an attached client is actively on within windowMs —
   * a point-in-time READ of the server-maintained client_activity + active
   * pane. No shadow state, no keystroke hook.
   */
  presentSeats(windowMs: number, nowMs?: number): Promise<Set<string>>;
  /** Type text into the seat's pane. Reports full/partial/none delivery. Resolves %id below the membrane. */
  sendToSeat(seatId: string, text: string): Promise<SendOutcome>;
}

const CANON_OPT = '@canonical_id';

async function run(socket: string, args: string[]): Promise<{ code: number; stdout: string; stderr: string }> {
  const proc = Bun.spawn(['tmux', '-L', socket, ...args], { stdout: 'pipe', stderr: 'pipe' });
  const [stdout, stderr] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text()]);
  const code = await proc.exited;
  return { code, stdout, stderr };
}

export class RealTmux implements TmuxControlPlane {
  constructor(private socket: string) {}

  async reachable(): Promise<boolean> {
    await run(this.socket, ['start-server']);
    const r = await run(this.socket, ['list-panes', '-a', '-F', '#{pane_id}']);
    // Exit 0, or an empty server with "no current session" — both mean the
    // server answered. A missing binary / dead socket is unreachable.
    return r.code === 0 || /no (server|current|sessions?)/i.test(r.stderr);
  }

  async version(): Promise<string | null> {
    const r = await run(this.socket, ['-V']);
    return r.code === 0 ? r.stdout.trim() : null;
  }

  /** Resolve canonical id -> internal %id (membrane; return value stays inside). */
  private async resolvePane(seatId: string): Promise<string | null> {
    const r = await run(this.socket, ['list-panes', '-a', '-F', `#{pane_id}\t#{${CANON_OPT}}`]);
    if (r.code !== 0) return null;
    for (const line of r.stdout.split('\n')) {
      const [paneId, canon] = line.split('\t');
      if (canon === seatId && paneId) return paneId;
    }
    return null;
  }

  async listSeats(): Promise<SeatObservation[]> {
    const r = await run(this.socket, ['list-panes', '-a', '-F', `#{${CANON_OPT}}\t#{pane_dead}`]);
    if (r.code !== 0) return [];
    const out: SeatObservation[] = [];
    for (const line of r.stdout.split('\n')) {
      if (!line.trim()) continue;
      const [canon, dead] = line.split('\t');
      if (!canon) continue; // untagged panes are not seats
      out.push({ seat_id: canon, pane: dead === '1' ? 'dead' : 'live' });
    }
    return out;
  }

  async createSeat(seatId: string): Promise<void> {
    // Sanitized tmux session name (canonical id may contain `:`); the true id
    // lives in the pane option only.
    const safe = `seat_${seatId.replace(/[^A-Za-z0-9_]/g, '_')}`;
    const created = await run(this.socket, ['new-session', '-d', '-s', safe, '-x', '200', '-y', '50']);
    // Fail loud: if the session didn't come up, do NOT go on to list/retag some
    // other pane and record a seat that was never really created.
    if (created.code !== 0) {
      throw new Error(`k12_daemon tmux createSeat failed for ${seatId}: ${created.stderr.trim() || `exit ${created.code}`}`);
    }
    const paneR = await run(this.socket, ['list-panes', '-t', safe, '-F', '#{pane_id}']);
    const paneId = paneR.stdout.trim().split('\n')[0];
    if (paneId) await run(this.socket, ['set-option', '-p', '-t', paneId, CANON_OPT, seatId]);
  }

  async killSeat(seatId: string): Promise<void> {
    const paneId = await this.resolvePane(seatId);
    if (paneId) await run(this.socket, ['kill-pane', '-t', paneId]);
  }

  async presentSeats(windowMs: number, nowMs = Date.now()): Promise<Set<string>> {
    // Active pane (canonical) per session.
    const panes = await run(this.socket, [
      'list-panes',
      '-a',
      '-F',
      `#{session_name}\t#{window_active}\t#{pane_active}\t#{${CANON_OPT}}`,
    ]);
    const activeCanonBySession = new Map<string, string>();
    for (const line of panes.stdout.split('\n')) {
      const [session, winActive, paneActive, canon] = line.split('\t');
      if (winActive === '1' && paneActive === '1' && session && canon) activeCanonBySession.set(session, canon);
    }
    // Attached clients + last activity (epoch seconds).
    const clients = await run(this.socket, ['list-clients', '-F', '#{client_session}\t#{client_activity}']);
    const present = new Set<string>();
    const nowSec = Math.floor(nowMs / 1000);
    for (const line of clients.stdout.split('\n')) {
      const [session, activity] = line.split('\t');
      if (!session) continue;
      const canon = activeCanonBySession.get(session);
      const activitySec = Number(activity);
      if (canon && Number.isFinite(activitySec) && (nowSec - activitySec) * 1000 <= windowMs) present.add(canon);
    }
    return present;
  }

  async sendToSeat(seatId: string, text: string): Promise<SendOutcome> {
    const paneId = await this.resolvePane(seatId);
    if (!paneId) return { bytes: 0, verdict: 'failed_none_delivered' };
    const literal = await run(this.socket, ['send-keys', '-t', paneId, '-l', text]);
    if (literal.code !== 0) return { bytes: 0, verdict: 'failed_none_delivered' };
    // Literal insert succeeded → the text is in the pane. If the separate Enter
    // fails, it's inserted-but-not-submitted = partial, not pure failure.
    const bytes = Buffer.byteLength(text, 'utf8');
    const enter = await run(this.socket, ['send-keys', '-t', paneId, 'Enter']);
    return enter.code === 0 ? { bytes, verdict: 'delivered' } : { bytes, verdict: 'partial_delivered' };
  }
}

// In-memory fake for tests — same membrane contract, no tmux dependency.
export class FakeTmux implements TmuxControlPlane {
  private seats = new Map<string, { pane: 'live' | 'dead' }>();
  private present = new Map<string, number>(); // seat -> last activity epoch ms
  reachableFlag = true;

  async reachable(): Promise<boolean> {
    return this.reachableFlag;
  }
  async version(): Promise<string | null> {
    return 'tmux 3.5a (fake)';
  }
  async listSeats(): Promise<SeatObservation[]> {
    return [...this.seats].map(([seat_id, s]) => ({ seat_id, pane: s.pane }));
  }
  async createSeat(seatId: string): Promise<void> {
    this.seats.set(seatId, { pane: 'live' });
  }
  async killSeat(seatId: string): Promise<void> {
    const s = this.seats.get(seatId);
    if (s) s.pane = 'dead';
  }
  /** Test control: kill a pane out-of-band (simulates a raw tmux kill). */
  killOutOfBand(seatId: string): void {
    const s = this.seats.get(seatId);
    if (s) s.pane = 'dead';
  }
  /** Test control: mark an operator active on a seat as of nowMs. */
  setPresence(seatId: string, atMs: number): void {
    this.present.set(seatId, atMs);
  }
  async presentSeats(windowMs: number, nowMs = Date.now()): Promise<Set<string>> {
    const out = new Set<string>();
    for (const [seat, at] of this.present) if (nowMs - at <= windowMs) out.add(seat);
    return out;
  }
  async sendToSeat(seatId: string, text: string): Promise<SendOutcome> {
    const s = this.seats.get(seatId);
    if (!s || s.pane === 'dead') return { bytes: 0, verdict: 'failed_none_delivered' };
    return { bytes: Buffer.byteLength(text, 'utf8'), verdict: 'delivered' };
  }
}
