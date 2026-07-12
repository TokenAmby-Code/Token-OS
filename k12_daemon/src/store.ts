// Event store (spec §2) — the single source of truth.
//
// One SQLite file, one append-only `events` table, ONE writer. Truth is the
// stream; every displayed status is a derived view (see projections.ts).
// Retention is keep-forever day-one; no snapshots (replay-from-zero every
// reconcile, honestly measured). The 8 columns are exactly the ruled shape —
// nothing derived is stored on the write side.
//
// Append-only is STRUCTURAL, not conventional: SQLite triggers raise on any
// UPDATE or DELETE, so a stray writer cannot silently rewrite history.

import { Database } from 'bun:sqlite';
import {
  EventInputSchema,
  type EventInput,
  type EventRecord,
  type EventType,
  type EntityType,
  type Provenance,
} from '@token-os/contracts';

type Row = {
  seq: number;
  entity_type: string;
  entity_id: string;
  event_type: string;
  payload: string;
  provenance: string;
  occurred_at: string;
  recorded_at: string;
};

function rowToRecord(r: Row): EventRecord {
  return {
    seq: r.seq,
    entity_type: r.entity_type as EntityType,
    entity_id: r.entity_id,
    event_type: r.event_type as EventType,
    payload: JSON.parse(r.payload),
    provenance: JSON.parse(r.provenance) as Provenance,
    occurred_at: r.occurred_at,
    recorded_at: r.recorded_at,
  };
}

export type Clock = () => string;
const systemClock: Clock = () => new Date().toISOString();

export class EventStore {
  private db: Database;
  private now: Clock;

  constructor(dbPath: string, now: Clock = systemClock) {
    this.db = new Database(dbPath, { create: true });
    this.now = now;
    // WAL survives process restart durably (systemd bounce, reboot); single
    // writer means no reader/writer contention to manage.
    this.db.exec('PRAGMA journal_mode = WAL');
    this.db.exec('PRAGMA synchronous = NORMAL');
    this.db.exec('PRAGMA foreign_keys = ON');
    this.migrate();
  }

  private migrate(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS events (
        seq          INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type  TEXT NOT NULL,
        entity_id    TEXT NOT NULL,
        event_type   TEXT NOT NULL,
        payload      TEXT NOT NULL,
        provenance   TEXT NOT NULL,
        occurred_at  TEXT NOT NULL,
        recorded_at  TEXT NOT NULL
      );
    `);
    this.db.exec('CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_id, seq)');
    // Structural append-only: history is immutable once written.
    this.db.exec(`
      CREATE TRIGGER IF NOT EXISTS events_no_update
      BEFORE UPDATE ON events
      BEGIN SELECT RAISE(ABORT, 'events is append-only: UPDATE forbidden'); END;
    `);
    this.db.exec(`
      CREATE TRIGGER IF NOT EXISTS events_no_delete
      BEFORE DELETE ON events
      BEGIN SELECT RAISE(ABORT, 'events is append-only: DELETE forbidden'); END;
    `);
  }

  /** Append one event. The store assigns seq (monotonic) and recorded_at. */
  append(input: EventInput): EventRecord {
    const parsed = EventInputSchema.parse(input);
    const recorded_at = this.now();
    const stmt = this.db.query(
      `INSERT INTO events (entity_type, entity_id, event_type, payload, provenance, occurred_at, recorded_at)
       VALUES ($entity_type, $entity_id, $event_type, $payload, $provenance, $occurred_at, $recorded_at)
       RETURNING seq`,
    );
    const res = stmt.get({
      $entity_type: parsed.entity_type,
      $entity_id: parsed.entity_id,
      $event_type: parsed.event_type,
      $payload: JSON.stringify(parsed.payload),
      $provenance: JSON.stringify(parsed.provenance),
      $occurred_at: parsed.occurred_at,
      $recorded_at: recorded_at,
    }) as { seq: number };
    return { ...parsed, seq: res.seq, recorded_at };
  }

  /** Append many events in one transaction (single-writer batch). */
  appendAll(inputs: EventInput[]): EventRecord[] {
    const tx = this.db.transaction((rows: EventInput[]) => rows.map((r) => this.append(r)));
    return tx(inputs);
  }

  /** Full stream in seq order — the replay source. */
  readAll(): EventRecord[] {
    return (this.db.query('SELECT * FROM events ORDER BY seq').all() as Row[]).map(rowToRecord);
  }

  readByEntity(entityId: string): EventRecord[] {
    return (
      this.db.query('SELECT * FROM events WHERE entity_id = $id ORDER BY seq').all({ $id: entityId }) as Row[]
    ).map(rowToRecord);
  }

  count(): number {
    return (this.db.query('SELECT COUNT(*) AS n FROM events').get() as { n: number }).n;
  }

  close(): void {
    this.db.close();
  }
}
