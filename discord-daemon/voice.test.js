// voice.test.js — pins tmux field parsing used by launchd-hosted voice routing.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { parseTmuxFields, TMUX_FIELD_SEP } from './voice.js';

test('tmux field parsing does not depend on literal tab output', () => {
  const line = ['1780603214', '/dev/ttys024', 'main'].join(TMUX_FIELD_SEP);

  assert.deepEqual(parseTmuxFields(line), ['1780603214', '/dev/ttys024', 'main']);
});

test('tmux field parsing preserves literal tabs inside field values', () => {
  const sessionName = 'main\twith-tab';
  const line = ['1780603214', '/dev/ttys024', sessionName].join(TMUX_FIELD_SEP);

  assert.deepEqual(parseTmuxFields(line), ['1780603214', '/dev/ttys024', sessionName]);
});

test('tmux field parsing handles the four-field paneInfo format', () => {
  const fields = ['%42', 'main', 'nvim', '/Users/tokenclaw/project'];
  const line = fields.join(TMUX_FIELD_SEP);

  assert.deepEqual(parseTmuxFields(line), fields);
});

test('tmux field separator is explicit for launchd C-locale tmux calls', () => {
  assert.equal(TMUX_FIELD_SEP.includes('\t'), false);
  // Keep the separator visually distinct from path fragments such as pane_current_path values.
  assert.equal(TMUX_FIELD_SEP.includes('_/'), false);
});
