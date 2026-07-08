# Bounty board — discord-daemon

JS mirror of the pytest bounty lane (`token-api/tests/bounty_board/`,
`cli-tools/tests/bounty_board/`). Speculative tests for **unbuilt** daemon
features, recording intent as executable contracts.

## Contract (xfail strict=False semantics)

- Every test wraps its body in `bounty(name, fn)` from `./bounty.js`.
- A failing body is an **open bounty**: logged as `OPEN BOUNTY: <name>`, the
  test still passes, the lane stays green.
- A passing body logs `XPASS — graduate: <name>`: the feature shipped — move
  the test into the root suite (`discord-daemon/*.test.js`), drop the
  `bounty()` wrapper, and assert for real.
- Future modules are loaded with dynamic `import()` **inside** the bounty body,
  never top-level, so a not-yet-existing module reads as an open bounty
  instead of crashing the runner.

## Running

```
npm run test:bounty     # advisory lane — wired into no gate
npm test                # root regression suites only (scoped, excludes this dir)
```

## Open bounties (Terminus Stage 2, PR C/D)

- `contracts-package.test.js` — daemon consumes `@token-os/contracts`
  (Zod `OpsStateSchema`, `ops-state.v1`). Ships with the daemon TS conversion
  (PR C adds the dep; the package itself lands in PR B).
- `fleet-render.test.js` — pure `renderFleetStatus(opsState) → {content}`
  read-model renderer for the `#fleet-status` edit-in-place message (PR D).
- `notify-endpoint.test.js` — `POST /notify {message, level}` on the daemon
  HTTP server, the Discord leg of the notification fabric (PR D).
