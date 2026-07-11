// http-server-selftest.test.js — pins the /voice/selftest HTTP surface (PR B).

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createHttpServer } from './http-server.ts';

function logger() {
  return { debug() {}, info() {}, warn() {}, error() {} };
}

function fakeClient() {
  return {
    onMessage() {},
    onReaction() {},
    getStatus() { return { connected: true }; },
    async sendMessage() { return { message_id: 'm1' }; },
  };
}

async function withServer(voiceSelftest, fn) {
  const server = createHttpServer(
    fakeClient(),
    { persist() {}, remove() {} },
    { daemon_port: 0, channels: {} },
    logger(),
    null,
    null,
    voiceSelftest,
  );
  await server.start();
  try {
    const { port } = server.address();
    await fn(`http://127.0.0.1:${port}`);
  } finally {
    await server.stop();
  }
}

const REPORT = {
  contract_version: 'voice-selftest.v1',
  probe_id: 'probe-1',
  variant: 'seams',
  trigger: 'manual',
  overall: 'pass',
  stages: [],
};

test('POST /voice/selftest passes variant+trigger through and returns the report', async () => {
  const runs = [];
  const selftest = {
    async run(opts) { runs.push(opts); return REPORT; },
    last: () => REPORT,
    consumeTranscript: () => false,
  };
  await withServer(selftest, async (url) => {
    const resp = await fetch(`${url}/voice/selftest`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ variant: 'full', trigger: 'cron' }),
    });
    assert.equal(resp.status, 200);
    assert.deepEqual(await resp.json(), REPORT);
  });
  assert.deepEqual(runs, [{ variant: 'full', trigger: 'cron' }]);
});

test('POST /voice/selftest returns 409 while a probe is in progress', async () => {
  const selftest = {
    async run() { return { errorCode: 'probe_in_progress', probe_id: 'probe-live' }; },
    last: () => null,
    consumeTranscript: () => false,
  };
  await withServer(selftest, async (url) => {
    const resp = await fetch(`${url}/voice/selftest`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ variant: 'seams' }),
    });
    assert.equal(resp.status, 409);
    const body = await resp.json();
    assert.equal(body.errorCode, 'probe_in_progress');
  });
});

test('GET /voice/selftest/last returns the report, 404 when none yet', async () => {
  let last = null;
  const selftest = {
    async run() { return REPORT; },
    last: () => last,
    consumeTranscript: () => false,
  };
  await withServer(selftest, async (url) => {
    const missing = await fetch(`${url}/voice/selftest/last`);
    assert.equal(missing.status, 404);
    last = REPORT;
    const resp = await fetch(`${url}/voice/selftest/last`);
    assert.equal(resp.status, 200);
    assert.deepEqual(await resp.json(), REPORT);
  });
});

test('selftest endpoints 501 when the module is absent', async () => {
  await withServer(null, async (url) => {
    const post = await fetch(`${url}/voice/selftest`, { method: 'POST', body: '{}' });
    assert.equal(post.status, 501);
    const get = await fetch(`${url}/voice/selftest/last`);
    assert.equal(get.status, 501);
  });
});
