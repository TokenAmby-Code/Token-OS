import { EventEmitter } from 'node:events';
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createRealtimeTranscriber } from './realtime-transcriber.ts';

class FakeWebSocket extends EventEmitter {
  static instances = [];

  constructor(url, options) {
    super();
    this.url = url;
    this.options = options;
    this.readyState = 1;
    this.sent = [];
    this.closed = false;
    FakeWebSocket.instances.push(this);
  }

  send(payload) {
    this.sent.push(JSON.parse(payload));
  }

  close() {
    this.closed = true;
    this.readyState = 3;
  }
}

function logger() {
  return {
    debug() {},
    info() {},
    warn() {},
    error() {},
  };
}

test('Realtime 60-minute session expiry tears down session so next audio reconnects', () => {
  FakeWebSocket.instances = [];
  const transcriber = createRealtimeTranscriber(
    {
      openai_api_key: 'test-key',
      _websocket_class: FakeWebSocket,
    },
    logger(),
    () => {}
  );

  transcriber.appendPCM('user-1', Buffer.alloc(19200), 'imperial_guard');
  const first = FakeWebSocket.instances[0];
  assert.ok(first);
  first.emit('open');
  first.emit('message', Buffer.from(JSON.stringify({ type: 'session.updated' })));
  assert.deepEqual(Object.keys(transcriber.getStatus()), ['imperial_guard:user-1']);

  first.emit(
    'message',
    Buffer.from(
      JSON.stringify({
        type: 'error',
        error: {
          code: 'session_expired',
          message: 'Your session hit the maximum duration of 60 minutes',
        },
      })
    )
  );

  assert.equal(first.closed, true);
  assert.deepEqual(transcriber.getStatus(), {});

  transcriber.appendPCM('user-1', Buffer.alloc(19200), 'imperial_guard');
  assert.equal(FakeWebSocket.instances.length, 2);
  assert.notEqual(FakeWebSocket.instances[1], first);
});

test('dropBot closes sessions and ignores late transcripts after VC leave', () => {
  FakeWebSocket.instances = [];
  const transcripts = [];
  const transcriber = createRealtimeTranscriber(
    {
      openai_api_key: 'test-key',
      _websocket_class: FakeWebSocket,
    },
    logger(),
    transcript => transcripts.push(transcript)
  );

  transcriber.appendPCM('user-1', Buffer.alloc(19200), 'imperial_guard');
  const ws = FakeWebSocket.instances[0];
  ws.emit('open');
  ws.emit('message', Buffer.from(JSON.stringify({ type: 'session.updated' })));
  transcriber.commitUser('user-1', 'imperial_guard', { reason: 'leave', voice_session_id: 'vs-1' });

  assert.equal(transcriber.dropBot('imperial_guard'), 1);
  assert.equal(ws.closed, true);
  assert.deepEqual(transcriber.getStatus(), {});

  ws.emit(
    'message',
    Buffer.from(
      JSON.stringify({
        type: 'conversation.item.input_audio_transcription.completed',
        transcript: 'stale Cadia transcript',
      })
    )
  );

  assert.deepEqual(transcripts, []);
});


test('Realtime commit skips buffers under 100ms and emits no transcript', () => {
  FakeWebSocket.instances = [];
  const transcripts = [];
  const logs = [];
  const transcriber = createRealtimeTranscriber(
    {
      openai_api_key: 'test-key',
      _websocket_class: FakeWebSocket,
      realtime_min_commit_audio_ms: 100,
    },
    { debug(msg) { logs.push(['debug', msg]); }, info() {}, warn() {}, error() {} },
    transcript => transcripts.push(transcript)
  );

  transcriber.appendPCM('user-1', Buffer.alloc(3840), 'imperial_guard', { routeEpoch: 1, channelId: 'cadia' });
  const ws = FakeWebSocket.instances[0];
  ws.emit('open');
  ws.emit('message', Buffer.from(JSON.stringify({ type: 'session.updated' })));

  assert.equal(transcriber.commitUser('user-1', 'imperial_guard', { reason: 'silence', routeEpoch: 1, channelId: 'cadia' }), false);
  assert.equal(ws.closed, true);
  assert.deepEqual(transcriber.getStatus(), {});
  assert.deepEqual(transcripts, []);
  assert.ok(logs.some(([, msg]) => String(msg).includes('skipping tiny commit')));
});

test('Realtime transcripts carry route epoch and channel metadata', () => {
  FakeWebSocket.instances = [];
  const transcripts = [];
  const transcriber = createRealtimeTranscriber(
    {
      openai_api_key: 'test-key',
      _websocket_class: FakeWebSocket,
    },
    logger(),
    transcript => transcripts.push(transcript)
  );

  transcriber.appendPCM('user-1', Buffer.alloc(19200), 'custodes', { routeEpoch: 7, channelId: 'terra' });
  const ws = FakeWebSocket.instances[0];
  ws.emit('open');
  ws.emit('message', Buffer.from(JSON.stringify({ type: 'session.updated' })));
  assert.equal(transcriber.commitUser('user-1', 'custodes', { reason: 'silence', routeEpoch: 7, channelId: 'terra' }), true);
  ws.emit('message', Buffer.from(JSON.stringify({
    type: 'conversation.item.input_audio_transcription.completed',
    item_id: 'item-1',
    transcript: 'For Terra',
  })));

  assert.equal(transcripts.length, 1);
  assert.equal(transcripts[0].routeEpoch, 7);
  assert.equal(transcripts[0].channelId, 'terra');
  assert.equal(transcripts[0].commitMeta.routeEpoch, 7);
});
