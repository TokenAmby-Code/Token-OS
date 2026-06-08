import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  defaultVoiceTargetForBot,
  normalizeVoiceCommand,
  parseVoiceCommand,
} from './voice-transcript-router.js';

test('persona bots use stable tmuxctl public targets, not physical pane ids', () => {
  assert.equal(defaultVoiceTargetForBot('custodes'), '3:0');
  assert.equal(defaultVoiceTargetForBot('mechanicus'), '4:0');
});

test('normalizes voice command text', () => {
  assert.equal(normalizeVoiceCommand('Command: Ship it!'), 'command ship it');
});

test('parses standalone commands', () => {
  assert.deepEqual(parseVoiceCommand('ship it'), { command: 'ship', draftText: '' });
  assert.deepEqual(parseVoiceCommand('scratch that'), { command: 'scratch', draftText: '' });
  assert.deepEqual(parseVoiceCommand('clear target'), { command: 'clear', draftText: '' });
});

test('parses suffix command while preserving draft text', () => {
  assert.deepEqual(parseVoiceCommand('do the thing, then verify ship'), {
    command: 'ship',
    draftText: 'do the thing, then verify',
  });
});

test('leading command filler is ignored for commands', () => {
  assert.deepEqual(parseVoiceCommand('command scratch'), { command: 'scratch', draftText: '' });
});

test('non-command transcript remains draft text', () => {
  assert.deepEqual(parseVoiceCommand('hello Custodes'), {
    command: null,
    draftText: 'hello Custodes',
  });
});
