# Bounty board (token-api)

Speculative, **non-blocking** tests for features the vault describes but the code
does **not yet implement**. The blocking regression suite lives in the rest of
`tests/`; this lane never gates a merge.

## How it works

- Drop a normal pytest module here. `conftest.py` auto-marks every test in this
  directory with `bounty` + `xfail(strict=False)` — no decorators needed.
- The required regression run excludes this lane (`pytest -m "not bounty"`).
- The advisory **Bounty Board** workflow runs *only* this lane and never blocks.

## Reading the board

| State | Meaning |
|-------|---------|
| **xfail** | Bounty is **open** — the feature isn't built. Lane stays green. |
| **xpass** | The feature shipped! **Graduate** the test: move it out of `bounty_board/` into the regression suite and delete the `bounty` framing. |

## The pin/bounty pair (drift capture)

When code behavior **contradicts** vault intent, the Coherence Wave files two
mutually-exclusive tests:

- a **pin** test in the regression suite asserting what the code *actually does
  now* (keeps regression honest and green), and
- a **bounty** test here asserting the vault-*intended* behavior (xfails until
  the code is fixed).

When the fix lands the pin goes **red** (delete it — reality changed) and the
bounty **xpasses** (graduate it). Full contract: `Sessions/coherence-wave.md`.
