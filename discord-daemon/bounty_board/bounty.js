// Bounty-board lane for the discord daemon (xfail strict=False semantics).
//
// A bounty records intent for an UNBUILT feature. The wrapped body is
// expected to fail today; that failure is an OPEN bounty and the lane stays
// green. When the feature ships and the body starts passing, the run logs
// "XPASS — graduate": move the test out of bounty_board/ into the root suite
// and drop the bounty() wrapper.
//
// Future modules must be loaded via dynamic import() INSIDE the bounty body —
// never top-level — so resolve/syntax errors on not-yet-existing modules are
// caught as open bounties instead of crashing the runner.

import { test } from 'node:test';

export function bounty(name, fn) {
  test(name, async (t) => {
    try {
      await fn(t);
    } catch (err) {
      const first = String(err && err.message ? err.message : err).split('\n')[0];
      console.log(`OPEN BOUNTY: ${name} (${first})`);
      return; // expected failure — lane stays green
    }
    console.log(`XPASS — graduate: ${name}`);
  });
}
