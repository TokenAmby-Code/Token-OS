import { describe, expect, it } from 'vitest';

import { toWorkerQueue, type WorkerItem } from './cockpitData';
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
