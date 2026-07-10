import { describe, expect, it } from 'vitest';

import { OPS_COCKPIT_POLLS } from './api';
import { buildDials, dialIsUnusual, enforcementDial, goldenThroneDial, laneForStatus, LEMON_RESIDENT_PERSONAS, occupancyCompassStars, toFleetQueues, toLemonActivity, toMusterBoard, toTtsQueue, ttsDial, type FleetQueues, type WorkerItem } from './cockpitData';
import type { OpsState } from './contracts';

type TestInstance = {
  id: string;
  display_name: string;
  created_at: string | null;
  is_subagent?: boolean;
  commander_type?: string | null;
  persona?: { slug: string | null; chip_color: string | null } | null;
  domain?: string;
  status?: string;
};

function stateWith(instances: TestInstance[]): OpsState {
  return {
    instances: {
      active: instances.map((i) => ({
        id: i.id,
        display_name: i.display_name,
        created_at: i.created_at,
        is_subagent: i.is_subagent ?? false,
        commander_type: i.commander_type ?? null,
        persona: i.persona
          ? {
              slug: i.persona.slug,
              chip_color: i.persona.chip_color,
            }
          : null,
        // domain/status deliberately absent unless the test sets them — the
        // old-payload fallback (token-os / idle) is part of the contract.
        ...(i.domain !== undefined ? { domain: i.domain } : {}),
        ...(i.status !== undefined ? { status: i.status } : {}),
      })),
    },
  } as unknown as OpsState;
}

const allBuckets = (q: FleetQueues): WorkerItem[][] => [
  q.tokenOs.working,
  q.tokenOs.idle,
  q.askCivic.working,
  q.askCivic.idle,
];

describe('toFleetQueues', () => {
  it('partitions each instance into exactly one of the four buckets (queue singleton)', () => {
    const queues = toFleetQueues(
      stateWith([
        { id: 'tw', display_name: 'Token Working', created_at: '2026-07-09T10:00:00', domain: 'token-os', status: 'working' },
        { id: 'ti', display_name: 'Token Idle', created_at: '2026-07-09T10:01:00', domain: 'token-os', status: 'idle' },
        { id: 'cw', display_name: 'Civic Working', created_at: '2026-07-09T10:02:00', domain: 'askcivic', status: 'working' },
        { id: 'ci', display_name: 'Civic Idle', created_at: '2026-07-09T10:03:00', domain: 'askcivic', status: 'reviewing' },
      ]),
    );

    expect(queues.tokenOs.working.map((w) => w.id)).toEqual(['tw']);
    expect(queues.tokenOs.idle.map((w) => w.id)).toEqual(['ti']);
    expect(queues.askCivic.working.map((w) => w.id)).toEqual(['cw']);
    expect(queues.askCivic.idle.map((w) => w.id)).toEqual(['ci']);
    // the invariant, stated flat: no id appears in two buckets
    const ids = allBuckets(queues).flat().map((w) => w.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids).toHaveLength(4);
  });

  it('never mixes domains across sides (the two systems never touch)', () => {
    const queues = toFleetQueues(
      stateWith([
        { id: 'c1', display_name: 'Civic 1', created_at: '2026-07-09T10:00:00', domain: 'askcivic', status: 'working' },
        { id: 'c2', display_name: 'Civic 2', created_at: '2026-07-09T10:01:00', domain: 'askcivic', status: 'idle' },
        { id: 't1', display_name: 'Token 1', created_at: '2026-07-09T10:02:00', domain: 'token-os', status: 'working' },
      ]),
    );

    const tokenIds = [...queues.tokenOs.working, ...queues.tokenOs.idle].map((w) => w.id);
    const civicIds = [...queues.askCivic.working, ...queues.askCivic.idle].map((w) => w.id);
    expect(tokenIds).toEqual(['t1']);
    expect(civicIds.sort()).toEqual(['c1', 'c2']);
  });

  it("maps status 'working' to the top queue and every other status to idle", () => {
    const queues = toFleetQueues(
      stateWith([
        { id: 'w', display_name: 'W', created_at: '2026-07-09T10:00:00', domain: 'token-os', status: 'working' },
        { id: 'r', display_name: 'R', created_at: '2026-07-09T10:01:00', domain: 'token-os', status: 'reviewing' },
        { id: 'v', display_name: 'V', created_at: '2026-07-09T10:02:00', domain: 'token-os', status: 'victorious' },
        { id: 'u', display_name: 'U', created_at: '2026-07-09T10:03:00', domain: 'token-os', status: 'somenewstatus' },
      ]),
    );

    expect(queues.tokenOs.working.map((w) => w.id)).toEqual(['w']);
    expect(queues.tokenOs.idle.map((w) => w.id)).toEqual(['u', 'v', 'r']); // newest-first
  });

  it('defaults a missing domain to token-os and a missing status to idle (old payloads)', () => {
    const queues = toFleetQueues(
      stateWith([{ id: 'legacy', display_name: 'Legacy Row', created_at: '2026-07-09T10:00:00' }]),
    );

    expect(queues.tokenOs.idle.map((w) => w.id)).toEqual(['legacy']);
    expect(queues.tokenOs.working).toEqual([]);
    expect(queues.askCivic.working).toEqual([]);
    expect(queues.askCivic.idle).toEqual([]);
  });

  it('keeps newest-first regKey ordering inside each bucket across SQLite and ISO timestamps', () => {
    const queues = toFleetQueues(
      stateWith([
        { id: 'older-space', display_name: 'Older Space', created_at: '2026-07-09 09:00:00', domain: 'token-os', status: 'working', persona: { slug: 'ultramarines', chip_color: '#243cff' } },
        { id: 'newer-iso', display_name: 'Newer ISO', created_at: '2026-07-09T10:00:00', domain: 'token-os', status: 'working', commander_type: 'chapter', persona: { slug: 'blood-angels', chip_color: '#b00020' } },
        { id: 'middle-space', display_name: 'Middle Space', created_at: '2026-07-09 09:30:00', domain: 'token-os', status: 'working', persona: null },
      ]),
    );

    expect(queues.tokenOs.working).toEqual([
      { id: 'newer-iso', persona: 'blood-angels', name: 'Newer ISO', tint: '#b00020', chapterChild: true },
      { id: 'middle-space', persona: 'astartes', name: 'Middle Space', tint: null, chapterChild: false },
      { id: 'older-space', persona: 'ultramarines', name: 'Older Space', tint: '#243cff', chapterChild: false },
    ]);
  });

  it('excludes subagents from every bucket', () => {
    const queues = toFleetQueues(
      stateWith([
        { id: 'sub', display_name: 'Subagent', created_at: '2026-07-09T11:00:00', is_subagent: true, domain: 'token-os', status: 'working' },
        { id: 'top', display_name: 'Top Level', created_at: '2026-07-09T10:00:00', domain: 'token-os', status: 'working' },
      ]),
    );

    expect(allBuckets(queues).flat().map((w) => w.id)).toEqual(['top']);
  });

  it('carries identical WorkerItem identity fields whichever bucket an instance lands in', () => {
    const mk = (status: string) =>
      stateWith([
        { id: 'x', display_name: 'Chip X', created_at: '2026-07-09T10:00:00', domain: 'askcivic', status, commander_type: 'chapter', persona: { slug: 'salamanders', chip_color: '#1b7a3d' } },
      ]);
    const working = toFleetQueues(mk('working')).askCivic.working[0];
    const idle = toFleetQueues(mk('idle')).askCivic.idle[0];

    expect(working).toEqual(idle); // the chip is the same dial wherever it sits
    expect(working).toEqual({ id: 'x', persona: 'salamanders', name: 'Chip X', tint: '#1b7a3d', chapterChild: true });
  });

  it('excludes lemon-resident personas from every bucket, working and idle alike', () => {
    // The full roster, alternating working/idle across both domains — none of
    // them may consume a queue slot; the lone mechanicus worker still lands.
    const residents = [...LEMON_RESIDENT_PERSONAS].map((slug, k) => ({
      id: `res-${slug}`,
      display_name: slug,
      created_at: `2026-07-09T10:0${k}:00`,
      domain: k % 2 ? 'askcivic' : 'token-os',
      status: k % 2 ? 'working' : 'idle',
      persona: { slug, chip_color: '#c0a040' },
    }));
    const queues = toFleetQueues(
      stateWith([
        ...residents,
        { id: 'wkr', display_name: 'Worker', created_at: '2026-07-09T11:00:00', domain: 'token-os', status: 'working', persona: { slug: 'mechanicus-worker', chip_color: '#884422' } },
      ]),
    );

    expect(allBuckets(queues).flat().map((w) => w.id)).toEqual(['wkr']);
  });
});

describe('toLemonActivity', () => {
  it('lights exactly the residents with a working instance', () => {
    const active = toLemonActivity(
      stateWith([
        { id: 'c', display_name: 'Custodes', created_at: '2026-07-09T10:00:00', domain: 'token-os', status: 'working', persona: { slug: 'custodes', chip_color: '#c0a040' } },
        { id: 'm', display_name: 'Malcador', created_at: '2026-07-09T10:01:00', domain: 'token-os', status: 'idle', persona: { slug: 'malcador', chip_color: '#8a7a4a' } },
        { id: 'p', display_name: 'Pax', created_at: '2026-07-09T10:02:00', domain: 'askcivic', status: 'working', persona: { slug: 'pax', chip_color: '#aa9955' } },
      ]),
    );

    expect(active).toEqual(new Set(['custodes', 'pax']));
  });

  it('ignores working non-residents — a busy chapter worker never lights the lemon', () => {
    const active = toLemonActivity(
      stateWith([
        { id: 'w', display_name: 'Worker', created_at: '2026-07-09T10:00:00', domain: 'token-os', status: 'working', persona: { slug: 'salamanders', chip_color: '#1b7a3d' } },
      ]),
    );

    expect(active.size).toBe(0);
  });

  it("ignores non-'working' residents and subagents wearing a resident persona", () => {
    const active = toLemonActivity(
      stateWith([
        { id: 'r', display_name: 'FG', created_at: '2026-07-09T10:00:00', domain: 'token-os', status: 'reviewing', persona: { slug: 'fabricator-general', chip_color: '#c05030' } },
        { id: 's', display_name: 'Custodes Sub', created_at: '2026-07-09T10:01:00', is_subagent: true, domain: 'token-os', status: 'working', persona: { slug: 'custodes', chip_color: '#c0a040' } },
      ]),
    );

    expect(active.size).toBe(0);
  });
});


describe('toTtsQueue', () => {
  it('preserves commanderType and playbackTarget metadata', () => {
    const queue = toTtsQueue({
      ...stateWith([
        {
          id: 'sender-instance-1234',
          display_name: 'Sender',
          created_at: '2026-07-09T10:00:00',
          commander_type: 'chapter',
          persona: { slug: 'ultramarines', chip_color: '#243cff' },
        },
      ]),
      tts: {
        current: {
          instance_id: 'sender-instance-1234',
          name: null,
          message: 'current line',
          voice: 'Microsoft David',
          backend: 'wsl',
          persona_slug: 'ultramarines',
          persona_display_name: 'Ultramarines',
          commander_type: 'chapter',
          playback_target: 'phone',
          started_at: '2026-07-09T10:00:01',
        },
        hot_queue: [],
        pause_queue: [
          {
            item_key: 'pause-key-1',
            instance_id: 'sender-instance-1234',
            name: null,
            message: 'queued line',
            voice: 'Microsoft David',
            queue: 'pause',
            queued_at: '2026-07-09T10:00:02',
            commander_type: 'persona',
            playback_target: 'wsl',
          },
        ],
      },
    } as unknown as OpsState);

    expect(queue[0]).toMatchObject({
      id: 'cur:sender-instance-1234:2026-07-09T10:00:01',
      itemKey: undefined,
      queueState: 'current',
      promotable: false,
      commanderType: 'chapter',
      playbackTarget: 'phone',
      senderName: 'Ultramarines',
    });
    expect(queue[1]).toMatchObject({
      itemKey: 'pause-key-1',
      queueState: 'pause',
      promotable: true,
      commanderType: 'persona',
      playbackTarget: 'wsl',
      route: 'pause/wsl · Sender',
    });
  });

  it('uses item_key in the current TTS item id when present', () => {
    const queue = toTtsQueue({
      ...stateWith([]),
      tts: {
        current: {
          item_key: 'current-key-1',
          instance_id: 'sender-instance-1234',
          name: 'Current Sender',
          message: 'current keyed line',
          voice: 'Microsoft David',
          backend: 'wsl',
          playback_target: 'phone',
          started_at: '2026-07-09T10:00:03',
        },
        hot_queue: [],
        pause_queue: [],
      },
    } as unknown as OpsState);

    expect(queue[0]).toMatchObject({
      id: 'cur:current-key-1:2026-07-09T10:00:03',
      itemKey: 'current-key-1',
      queueState: 'current',
      promotable: false,
    });
  });
});

// A fully-nominal OpsState — every subsystem in its expected state. Tests mutate
// one facet at a time to prove exactly that facet makes its dial unusual.
function nominalState(): OpsState {
  return {
    timer: { mode: 'working', break_balance_ms: 60000 },
    attention: { phone: { app: null, is_distracted: false }, desktop: { mode: 'silence' } },
    sources: { cron: { status: 'ok' }, tts: { status: 'ok' }, enforcement: { status: 'ok' }, token_api: { status: 'ok' }, agents_db: { status: 'ok' }, timer_engine: { status: 'ok' }, tmuxctld: { status: 'ok' } },
    tts: { hot_queue_length: 0, pause_queue_length: 0, hot_queue: [], pause_queue: [], current: null, backend: 'wsl', satellite_available: true },
    enforcement: { pending_count: 0, pavlok: { enabled: true } },
    instances: { active: [], counts: { active: 2, stale: 0, by_engine: { codex: 2 }, by_status: {}, by_persona: {} } },
    work_state: { productivity_active: true, reason: 'recent activity', typing_active: false },
    tmux: { reachable: true, occupancy: { status: 'ok', total: 4, occupied: 2, free: 2, dead: 0, protected: 0, drift: 0, unknown: 0, errors: [], cells: [], generated_at: 'x' } },
  } as unknown as OpsState;
}

describe('dial builders', () => {
  it('ttsDial reports speaking and queued metadata', () => {
    const dial = ttsDial({
      sources: { tts: { status: 'ok', message: null } },
      tts: { current: { message: 'x' }, hot_queue_length: 1, pause_queue_length: 2, hot_queue: [], pause_queue: [], backend: 'wsl', satellite_available: true },
    } as unknown as OpsState);

    expect(dial).toMatchObject({ id: 'tts', value: 'speaking', tone: 'warn', defaultValue: 'idle' });
    expect(dialIsUnusual(dial)).toBe(true);
  });

  it('enforcementDial reports pending actions', () => {
    const dial = enforcementDial({
      sources: { enforcement: { status: 'ok', message: null } },
      enforcement: { pending_count: 2, pavlok: { enabled: true } },
    } as unknown as OpsState);

    expect(dial).toMatchObject({ id: 'enforce', value: 'pending 2', tone: 'bad', action: { kind: 'ack-enforce' } });
  });

  it('enforcementDial surfaces a warn-health source even with an empty pending queue', () => {
    const dial = enforcementDial({
      sources: { enforcement: { status: 'warn', message: 'stale heartbeat' } },
      enforcement: { pending_count: 0, pavlok: { enabled: true } },
    } as unknown as OpsState);

    expect(dial).toMatchObject({ id: 'enforce', value: 'degraded', tone: 'warn', defaultValue: 'clear' });
    expect(dialIsUnusual(dial)).toBe(true);
  });

  // The optimal-cockpit invariant: with every subsystem in its expected state,
  // every dial's value collapses onto its defaultValue — the fan renders NOTHING.
  it('buildDials yields zero unusual dials when every subsystem is nominal', () => {
    const dials = buildDials(nominalState());
    expect(dials.filter(dialIsUnusual)).toEqual([]);
  });

  it('timer dial surfaces morning_session as unusual with the end-morning click contract', () => {
    const s = nominalState();
    (s.timer as { mode: string }).mode = 'morning_session';
    const timer = buildDials(s).find((d) => d.id === 'timer');
    expect(timer).toMatchObject({ value: 'MORNING', defaultValue: 'WORKING', action: { kind: 'end-morning' } });
    expect(dialIsUnusual(timer!)).toBe(true);
  });

  it('balance dial hides credit and surfaces debt', () => {
    const credit = buildDials(nominalState()).find((d) => d.id === 'balance');
    expect(dialIsUnusual(credit!)).toBe(false);
    const s = nominalState();
    (s.timer as { break_balance_ms: number }).break_balance_ms = -120000;
    const debt = buildDials(s).find((d) => d.id === 'balance');
    expect(debt).toMatchObject({ value: '−2m', tone: 'bad' });
    expect(dialIsUnusual(debt!)).toBe(true);
  });

  it('goldenThroneDial reports due rubrics', () => {
    const dial = goldenThroneDial({
      instances: { active: [{ gt: { next_fire: '2000-01-01T00:00:00Z', resume_count: 0, victory_at: null } }] },
    } as unknown as OpsState);

    expect(dial).toMatchObject({ id: 'gt', value: 'due 1', tone: 'bad', defaultValue: 'clear' });
    expect(dialIsUnusual(dial)).toBe(true);
  });
});

// ── The poll ledger tripwire ────────────────────────────────────────────────
// Pins the OPS_COCKPIT_POLLS manifest to the exact known polls. A third poll
// cannot slip in unledgered: register it in api.ts AND update this test —
// deliberately, in review — or the suite goes red.

describe('OPS_COCKPIT_POLLS', () => {
  it('ledgers exactly the two tolerated polls', () => {
    expect(OPS_COCKPIT_POLLS.map(({ route, intervalMs }) => ({ route, intervalMs }))).toEqual([
      { route: '/api/ui/ops/state', intervalMs: 2000 },
      { route: '/api/ui/ops/timer/history', intervalMs: 30000 },
    ]);
  });
});

// ── Muster Ledger projection ────────────────────────────────────────────────

describe('laneForStatus', () => {
  it('implements the Session Lifecycle Decree absorption table', () => {
    // one representative per absorption group + the canonical slugs themselves
    expect(laneForStatus('stub')).toBe('aspirant');
    expect(laneForStatus('dispatched')).toBe('aspirant');
    expect(laneForStatus('aspirant')).toBe('aspirant');
    expect(laneForStatus('active')).toBe('astartes');
    expect(laneForStatus('in-progress')).toBe('astartes');
    expect(laneForStatus('in-review')).toBe('arbites');
    expect(laneForStatus('parked-ready-to-merge')).toBe('arbites');
    expect(laneForStatus('merged')).toBe('inquisitor');
    expect(laneForStatus('merged-deployed-live-verified')).toBe('inquisitor');
    expect(laneForStatus('complete')).toBe('victorious');
    expect(laneForStatus('consolidated')).toBe('victorious');
  });

  it('hides the terminal statuses (archived is not a lane)', () => {
    expect(laneForStatus('archived')).toBeNull();
    expect(laneForStatus('reference')).toBeNull();
    expect(laneForStatus('captured')).toBeNull();
  });

  it('projects unknown/prose dialects to the working default', () => {
    expect(laneForStatus('doing the thing')).toBe('astartes');
    expect(laneForStatus('  Active ')).toBe('astartes');
  });
});

describe('toMusterBoard', () => {
  const NOW = new Date(2026, 6, 9, 15, 0, 0); // local 2026-07-09 15:00

  type TestDoc = {
    id?: number | null;
    status?: string;
    title?: string | null;
    path?: string | null;
    head?: string | null;
    created_at?: string | null;
    session_date?: string | null;
    age_seconds?: number | null;
    linked_instances?: number;
    rubric?: Record<string, unknown> | null;
  };

  function boardState(docs: TestDoc[], laneTotals: Record<string, number> = {}, instances: unknown[] = []): OpsState {
    return {
      instances: { active: instances },
      session_docs: {
        generated_at: '2026-07-09T15:00:00',
        lane_totals: laneTotals,
        limit_per_lane: 4,
        docs: docs.map((d) => ({
          id: d.id ?? 1,
          status: d.status ?? 'active',
          title: d.title ?? null,
          path: d.path ?? null,
          head: d.head ?? null,
          created_at: d.created_at ?? '2026-07-09 10:00:00',
          session_date: d.session_date ?? null,
          age_seconds: d.age_seconds ?? null,
          linked_instances: d.linked_instances ?? 0,
          rubric: d.rubric ?? null,
        })),
      },
    } as unknown as OpsState;
  }

  it('returns no lanes when the feed is absent (older API)', () => {
    expect(toMusterBoard({ instances: { active: [] } } as unknown as OpsState, NOW)).toEqual({});
  });

  it('projects raw statuses onto canonical lanes and keeps only today', () => {
    const board = toMusterBoard(
      boardState([
        { id: 1, status: 'active', title: 'Today Doc', created_at: '2026-07-09 09:00:00' },
        { id: 2, status: 'active', title: 'Yesterday Doc', created_at: '2026-07-08T23:59:00' },
        { id: 3, status: 'merged', title: 'Deploying', created_at: '2026-07-09T12:00:00' },
      ]),
      NOW,
    );

    expect(board.astartes.cards.map((c) => c.title)).toEqual(['Today Doc']);
    expect(board.inquisitor.cards.map((c) => c.title)).toEqual(['Deploying']);
  });

  it('prefers session_date over created_at for the today gate', () => {
    const board = toMusterBoard(
      boardState([
        // frontmatter says today even though the DB row is older — kept
        { id: 1, session_date: '2026-07-09', created_at: '2026-07-01 08:00:00', title: 'FM Today' },
        // frontmatter says yesterday — dropped even though DB created today
        { id: 2, session_date: '2026-07-08', created_at: '2026-07-09 08:00:00', title: 'FM Yesterday' },
      ]),
      NOW,
    );

    expect(board.astartes.cards.map((c) => c.title)).toEqual(['FM Today']);
  });

  it('falls back to the path basename for untitled docs', () => {
    const board = toMusterBoard(
      boardState([{ id: 1, title: null, path: 'Sessions/kanban-docs-wiring.md' }]),
      NOW,
    );

    expect(board.astartes.cards[0].title).toBe('kanban-docs-wiring');
  });

  it('re-caps each canonical lane at limit_per_lane after absorbing multiple raw statuses', () => {
    // The feed caps per RAW status; active + in-progress both absorb into
    // astartes, so raw caps could stack to 2× the lane limit without a
    // board-side re-cap. Newest (feed order) survive; overflow stays honest.
    const state = boardState(
      [
        { id: 1, status: 'active', title: 'A1' },
        { id: 2, status: 'active', title: 'A2' },
        { id: 3, status: 'in-progress', title: 'P1' },
        { id: 4, status: 'in-progress', title: 'P2' },
      ],
      { active: 2, 'in-progress': 2 },
    );
    (state.session_docs as { limit_per_lane: number }).limit_per_lane = 3;

    const board = toMusterBoard(state, NOW);

    expect(board.astartes.cards.map((c) => c.title)).toEqual(['A1', 'A2', 'P1']);
    expect(board.astartes.overflow).toBe(1); // 4 truly in-lane, 3 shown
  });

  it('projects raw lane_totals onto lanes and reports honesty overflow', () => {
    const board = toMusterBoard(
      boardState(
        [{ id: 1, status: 'active', title: 'Shown' }],
        // lane_totals: raw-keyed, pre-cap, all days — active + in-progress both
        // project to astartes; in-review opens an otherwise-empty arbites lane.
        { active: 40, 'in-progress': 2, 'in-review': 3, archived: 500 },
      ),
      NOW,
    );

    expect(board.astartes.cards[0].laneKey).toBe('astartes');
    expect(board.astartes.overflow).toBe(41); // 42 truly in-lane, 1 shown
    expect(board.arbites).toEqual({ cards: [], overflow: 3 });
    expect(board.victorious).toBeUndefined(); // empty lane renders empty
    // archived is a hidden terminal — it must never open a lane or count
    expect(Object.values(board).reduce((n, l) => n + l.overflow, 0)).toBe(44);
  });
});

describe('tmux occupancy adapters', () => {
  it('buildDials exposes real tmux/fleet/work/source dials and no mac/wsl/mesh placeholders', () => {
    const dials = buildDials({
      timer: { mode: 'working', break_balance_ms: 60000 },
      attention: { phone: { app: null, is_distracted: false }, desktop: { mode: 'silence' } },
      sources: { cron: { status: 'ok' }, tts: { status: 'ok' }, enforcement: { status: 'ok' }, token_api: { status: 'ok' }, agents_db: { status: 'ok' }, timer_engine: { status: 'ok' }, tmuxctld: { status: 'ok' } },
      tts: { hot_queue_length: 0, pause_queue_length: 0, hot_queue: [], pause_queue: [], current: null, backend: 'wsl', satellite_available: true },
      enforcement: { pending_count: 0, pavlok: {} },
      instances: { active: [], counts: { active: 2, stale: 1, by_engine: { codex: 2 }, by_status: {}, by_persona: {} } },
      work_state: { productivity_active: true, reason: 'recent activity', typing_active: true },
      tmux: { reachable: true, occupancy: { status: 'warn', total: 4, occupied: 2, free: 1, dead: 0, protected: 0, drift: 1, unknown: 0, errors: [], cells: [], generated_at: 'x' } },
    } as unknown as OpsState);
    const ids = dials.map((d) => d.id);
    expect(ids).toEqual(expect.arrayContaining(['tmux', 'fleet', 'work', 'sources']));
    expect(ids).not.toEqual(expect.arrayContaining(['mac', 'wsl', 'mesh']));
  });


  it('keeps tmux dial bad when bad occupancy also reports drift or dead panes', () => {
    const dials = buildDials({
      sources: { cron: { status: 'ok' }, tts: { status: 'ok' }, enforcement: { status: 'ok' }, token_api: { status: 'ok' }, agents_db: { status: 'ok' }, timer_engine: { status: 'ok' }, tmuxctld: { status: 'warn' } },
      timer: { mode: 'working', break_balance_ms: 0 },
      attention: { phone: {}, desktop: {} },
      tts: { hot_queue_length: 0, pause_queue_length: 0, hot_queue: [], pause_queue: [], current: null, backend: 'wsl', satellite_available: true },
      enforcement: { pending_count: 0, pavlok: {} },
      instances: { active: [], counts: { active: 0, stale: 0, by_engine: {}, by_status: {}, by_persona: {} } },
      work_state: { productivity_active: false, reason: 'idle', typing_active: false },
      tmux: { reachable: true, occupancy: { status: 'bad', total: 2, occupied: 0, free: 0, dead: 1, protected: 0, drift: 1, unknown: 0, errors: ['partial failure'], cells: [], generated_at: 'x' } },
    } as unknown as OpsState);

    const tmux = dials.find((d) => d.id === 'tmux');
    expect(tmux).toMatchObject({ tone: 'bad', value: '0/2 used' });
    expect(dialIsUnusual(tmux!)).toBe(true);
  });

  it('maps occupied palace/somnium pane slots to compass star colors', () => {
    const stars = occupancyCompassStars({
      tmux: {
        reachable: true,
        occupancy: {
          status: 'ok', total: 4, occupied: 3, free: 1, dead: 0, protected: 0, drift: 0, unknown: 0, errors: [], generated_at: 'x',
          cells: [
            { pane_positional_id: 'palace:N', state: 'occupied' },
            { pane_positional_id: '2:NE', state: 'occupied' },
            { pane_positional_id: 'somnium:S', state: 'free' },
            { pane_positional_id: '1:E', state: 'drift' },
          ],
        },
      },
    } as unknown as OpsState);
    expect(stars).toEqual([
      { dir: 'N', color: 'red' },
      { dir: 'NE', color: 'blue' },
      { dir: 'E', color: 'red' },
    ]);
  });
});
