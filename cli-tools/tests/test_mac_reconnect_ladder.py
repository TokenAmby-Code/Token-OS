"""Unit tests for the sticky `mac()` reconnect ladder in the Termux bashrc.

The reconnect ladder lives inside `mobile/termux-bashrc-template` (shipped
verbatim to the phone as ~/.bashrc). Its per-attempt decision — how long to
wait, whether to print, whether to escalate to a deeper restart — is factored
into a pure shell helper `_mac_reconnect_plan` so it can be exercised without
opening a real SSH connection. These tests extract that one function from the
template and drive it under bash.

Ladder contract (see the template comment for rationale):
  attempt 1              -> 0s, silent            (instant re-tap)
  attempt 2              -> 1s, print once        (aggressive burst starts)
  attempts 3..BURST_MAX  -> 1s, silent            (burst continues)
  attempt BURST_MAX+1    -> 0s, deeper restart    (unless already done)
  thereafter             -> exponential backoff 2,4,8 capped at 8s
"""

import subprocess
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "mobile" / "termux-bashrc-template"
BURST_MAX = 6  # attempts 1..6 = the 0s + five 1s aggressive burst


def _extract_fn(name: str) -> str:
    """Slice a top-level shell function definition out of the template."""
    lines = TEMPLATE.read_text().splitlines()
    out, capturing = [], False
    for ln in lines:
        if ln.startswith(f"{name}() {{"):
            capturing = True
        if capturing:
            out.append(ln)
        if capturing and ln == "}":
            return "\n".join(out)
    raise AssertionError(f"function {name} not found in {TEMPLATE}")


def _plan(attempt: int, burst_max: int = BURST_MAX, deep_done: int = 0):
    fn = _extract_fn("_mac_reconnect_plan")
    script = f"{fn}\n_mac_reconnect_plan {attempt} {burst_max} {deep_done}\n"
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.split()


def test_attempt1_is_instant_and_silent():
    # First drop: re-tap immediately, print nothing.
    assert _plan(1) == ["0", "0", "0"]


def test_attempt2_waits_one_second_and_prints():
    # Only the second attempt surfaces the reconnect message.
    assert _plan(2) == ["1", "1", "0"]


def test_burst_is_five_one_second_cycles_and_prints_once():
    # attempts 2..6 are all 1s (five cycles); only attempt 2 prints.
    assert _plan(2)[0] == "1"
    for a in (3, 4, 5, 6):
        delay, do_print, do_deep = _plan(a)
        assert delay == "1", f"attempt {a} should stay in the 1s burst"
        assert do_print == "0", f"attempt {a} must not re-print"
        assert do_deep == "0", f"attempt {a} must not deep-restart yet"


def test_deeper_restart_fires_once_after_burst():
    # Right after the burst: zero-delay deeper restart (the fresh-`mac` reset).
    assert _plan(BURST_MAX + 1) == ["0", "0", "1"]


def test_backoff_is_exponential_and_capped_after_deep_restart():
    # Once the deeper restart has fired, fall into exponential backoff, cap 8s.
    assert _plan(8, deep_done=1) == ["2", "0", "0"]
    assert _plan(9, deep_done=1) == ["4", "0", "0"]
    assert _plan(10, deep_done=1) == ["8", "0", "0"]
    assert _plan(11, deep_done=1) == ["8", "0", "0"]  # capped, not 16
