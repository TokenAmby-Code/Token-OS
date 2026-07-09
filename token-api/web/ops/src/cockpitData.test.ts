import { describe, expect, it } from 'vitest';

import { enforcementDial, goldenThroneDial, toTtsQueue, toWorkerQueue, ttsDial, type WorkerItem } from './cockpitData';
import type { OpsState } from './contracts';

type TestInstance = {
  id: string;
  display_name: string;
  created_at: string | null;
  is_subagent?: boolean;
  commander_type?: string | null;
  persona?: { slug: string | null; chip_color: string | null } | null;
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
      })),
    },
  } as unknown as OpsState;
}

describe('toWorkerQueue', () => {
  it('maps top-level active instances newest-first across SQLite and ISO timestamps', () => {
    const workers: WorkerItem[] = toWorkerQueue(
      stateWith([
        {
          id: 'older-space',
          display_name: 'Older Space',
          created_at: '2026-07-09 09:00:00',
          persona: { slug: 'ultramarines', chip_color: '#243cff' },
        },
        {
          id: 'newer-iso',
          display_name: 'Newer ISO',
          created_at: '2026-07-09T10:00:00',
          commander_type: 'chapter',
          persona: { slug: 'blood-angels', chip_color: '#b00020' },
        },
        {
          id: 'subagent-filtered',
          display_name: 'Filtered Subagent',
          created_at: '2026-07-09T11:00:00',
          is_subagent: true,
          persona: { slug: 'raven-guard', chip_color: '#111111' },
        },
        {
          id: 'middle-space',
          display_name: 'Middle Space',
          created_at: '2026-07-09 09:30:00',
          persona: null,
        },
      ]),
    );

    expect(workers).toEqual([
      {
        id: 'newer-iso',
        persona: 'blood-angels',
        name: 'Newer ISO',
        tint: '#b00020',
        chapterChild: true,
      },
      {
        id: 'middle-space',
        persona: 'astartes',
        name: 'Middle Space',
        tint: null,
        chapterChild: false,
      },
      {
        id: 'older-space',
        persona: 'ultramarines',
        name: 'Older Space',
        tint: '#243cff',
        chapterChild: false,
      },
    ]);
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

    expect(queue[0]).toMatchObject({ commanderType: 'chapter', playbackTarget: 'phone', senderName: 'Ultramarines' });
    expect(queue[1]).toMatchObject({ commanderType: 'persona', playbackTarget: 'wsl', route: 'pause/wsl · Sender' });
  });
});

describe('dial builders', () => {
  it('ttsDial reports speaking and queued metadata', () => {
    const dial = ttsDial({
      sources: { tts: { status: 'ok', message: null } },
      tts: { current: { message: 'x' }, hot_queue_length: 1, pause_queue_length: 2, hot_queue: [], pause_queue: [], backend: 'wsl', satellite_available: true },
    } as unknown as OpsState);

    expect(dial).toMatchObject({ id: 'tts', value: 'speaking', tone: 'warn', noteworthy: true });
  });

  it('enforcementDial reports pending actions', () => {
    const dial = enforcementDial({
      sources: { enforcement: { status: 'ok', message: null } },
      enforcement: { pending_count: 2, pavlok: { enabled: true } },
    } as unknown as OpsState);

    expect(dial).toMatchObject({ id: 'enforce', value: 'pending 2', tone: 'bad', action: { kind: 'ack-enforce' } });
  });

  it('goldenThroneDial reports due rubrics', () => {
    const dial = goldenThroneDial({
      instances: { active: [{ gt: { next_fire: '2000-01-01T00:00:00Z', resume_count: 0, victory_at: null } }] },
    } as unknown as OpsState);

    expect(dial).toMatchObject({ id: 'gt', value: 'due 1', tone: 'bad', noteworthy: true });
  });
});
