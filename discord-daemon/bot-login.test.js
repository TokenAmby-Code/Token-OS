// bot-login.test.js — pins bounded login retry and zombie-client hygiene.
//
// Regression class (2026-07-17..20 outage): a transient Discord 500 at boot
// permanently deleted the bot from botClients (voice selftest lost its
// speaker for days) while the never-logged-in client object lingered. The
// contract: retry the SAME client object with bounded backoff, remove the
// bot from the map only while it is down, re-add on success, and destroy
// the client once retries are exhausted.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createBotLogin, LOGIN_RETRY_DELAYS_MS } from './bot-login.ts';

function logger(logs = []) {
  return {
    logs,
    debug(msg) { logs.push(['debug', msg]); },
    info(msg) { logs.push(['info', msg]); },
    warn(msg) { logs.push(['warn', msg]); },
    error(msg) { logs.push(['error', msg]); },
  };
}

function fakeClient({ failures = 0 } = {}) {
  const client = {
    startCalls: 0,
    stopCalls: 0,
    async start() {
      client.startCalls += 1;
      if (client.startCalls <= failures) throw new Error('Internal Server Error');
    },
    async stop() { client.stopCalls += 1; },
  };
  return client;
}

// setTimeout stand-in that records scheduled callbacks for manual firing.
function manualTimers() {
  const scheduled = [];
  return {
    scheduled,
    impl(fn, delayMs) {
      scheduled.push({ fn, delayMs });
      return { unref() {} };
    },
    async fire(i) {
      await scheduled[i].fn();
    },
  };
}

test('default retry delays are bounded and start at 60s', () => {
  assert.ok(Array.isArray(LOGIN_RETRY_DELAYS_MS));
  assert.ok(LOGIN_RETRY_DELAYS_MS.length >= 2);
  assert.ok(LOGIN_RETRY_DELAYS_MS[0] >= 60_000);
});

test('successful boot login keeps the bot in the map, no retries scheduled', async () => {
  const client = fakeClient();
  const botClients = { inquisition: client };
  const timers = manualTimers();
  const log = logger();
  const botLogin = createBotLogin({ botClients, logger: log, setTimeoutImpl: timers.impl });

  await botLogin.startAll();

  assert.equal(botClients.inquisition, client);
  assert.equal(timers.scheduled.length, 0);
  assert.ok(log.logs.some(([lvl, msg]) => lvl === 'info' && msg.includes("Bot 'inquisition' connected")));
});

test('transient boot failure removes the bot, retry on the SAME client re-adds it', async () => {
  const client = fakeClient({ failures: 1 });
  const botClients = { inquisition: client };
  const timers = manualTimers();
  const log = logger();
  const botLogin = createBotLogin({
    botClients,
    logger: log,
    retryDelaysMs: [10, 20],
    setTimeoutImpl: timers.impl,
  });

  await botLogin.startAll();

  // Down while waiting for the retry: sends must fall back, not hit a dead client.
  assert.equal(botClients.inquisition, undefined);
  assert.equal(timers.scheduled.length, 1);
  assert.equal(timers.scheduled[0].delayMs, 10);

  await timers.fire(0);

  // Same object came back — creation-time handlers stay wired.
  assert.equal(botClients.inquisition, client);
  assert.equal(client.startCalls, 2);
  assert.equal(client.stopCalls, 0);
  assert.ok(log.logs.some(([, msg]) => msg.includes('(login retry 1)') && msg.includes('connected')));
});

test('exhausted retries destroy the zombie client and stay loud', async () => {
  const client = fakeClient({ failures: 99 });
  const botClients = { inquisition: client };
  const timers = manualTimers();
  const log = logger();
  const botLogin = createBotLogin({
    botClients,
    logger: log,
    retryDelaysMs: [10, 20],
    setTimeoutImpl: timers.impl,
  });

  await botLogin.startAll();
  await timers.fire(0);
  await timers.fire(1);

  assert.equal(botClients.inquisition, undefined);
  assert.equal(client.startCalls, 3); // boot + 2 retries
  assert.equal(client.stopCalls, 1); // zombie destroyed exactly once
  assert.equal(timers.scheduled.length, 2); // nothing further scheduled
  assert.ok(log.logs.some(([lvl, msg]) => lvl === 'warn' && msg.includes('login retries exhausted')));
});

test('one failing bot never blocks the others', async () => {
  const dead = fakeClient({ failures: 99 });
  const live = fakeClient();
  const botClients = { inquisition: dead, mechanicus: live };
  const timers = manualTimers();
  const botLogin = createBotLogin({
    botClients,
    logger: logger(),
    retryDelaysMs: [10],
    setTimeoutImpl: timers.impl,
  });

  await botLogin.startAll();

  assert.equal(botClients.mechanicus, live);
  assert.equal(botClients.inquisition, undefined);
  assert.equal(live.startCalls, 1);
});
