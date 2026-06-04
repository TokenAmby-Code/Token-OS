// voice.test.js — pins tmux field parsing used by launchd-hosted voice routing.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { parseTmuxFields, TMUX_FIELD_SEP } from './voice.js';

test('tmux field parsing does not depend on literal tab output', () => {
  const line = ['1780603214', '/dev/ttys024', 'main'].join(TMUX_FIELD_SEP);

  assert.deepEqual(parseTmuxFields(line), ['1780603214', '/dev/ttys024', 'main']);
});

test('tmux field separator is explicit for launchd C-locale tmux calls', () => {
  assert.equal(TMUX_FIELD_SEP.includes('\t'), false);
  assert.equal(TMUX_FIELD_SEP.includes('_/'), false);
});
