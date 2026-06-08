import { EventEmitter } from 'node:events';
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createRealtimeTranscriber } from './realtime-transcriber.js';

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

  transcriber.appendPCM('user-1', Buffer.alloc(3840), 'imperial_guard');
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

  transcriber.appendPCM('user-1', Buffer.alloc(3840), 'imperial_guard');
  assert.equal(FakeWebSocket.instances.length, 2);
  assert.notEqual(FakeWebSocket.instances[1], first);
});
