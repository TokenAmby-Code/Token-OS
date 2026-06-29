"""`_assert_and_send_custodes` must fail open when the persona correction is stuck.

Counterpart to the tmuxctld prompt-send fail-open: when `tmuxctl assert-instance`
reports the live runtime is present but the persona correction is stuck after
bounded attempts (`deliverable=True`), the launch/Discord send path must still
deliver through tmuxctld — emitting a loud diagnostic — instead of returning
`dispatched=False` and silently dropping the enforcement intervention.
A genuinely failed assertion (no `deliverable`) must still refuse delivery.
"""

import json


class _FakeProc:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def _patch_subprocess(
    monkeypatch, main, *, assert_result: dict, send_rc: int = 0
) -> list[list[str]]:
    """Route create_subprocess_exec to canned assert / send-text outputs."""
    calls: list[list[str]] = []

    async def fake_exec(*args, **kwargs):
        argv = list(args)
        calls.append(argv)
        if "assert-instance" in argv:
            return _FakeProc(stdout=json.dumps(assert_result).encode())
        if "send-text" in argv:
            return _FakeProc(stdout=b"", stderr=b"", returncode=send_rc)
        raise AssertionError(f"unexpected exec: {argv}")

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", fake_exec)
    return calls


async def test_assert_and_send_fails_open_when_correction_stuck(app_env, monkeypatch) -> None:
    main = app_env.main
    calls = _patch_subprocess(
        monkeypatch,
        main,
        assert_result={
            "ok": False,
            "action": "persona_correction_failopen",
            "deliverable": True,
            "reason": "persona_assert_failopen attempts=4",
            "pane": "%25",
        },
    )

    sent = []

    async def fake_send(pane, prompt, **kwargs):
        sent.append((pane, prompt, kwargs))
        return {"returncode": 0, "stderr": "", "gated": False}

    monkeypatch.setattr(main, "send_prompt_to_pane", fake_send)

    result = await main._assert_and_send_custodes("enforcement payload", source="test")

    # The payload reached the pane via tmuxctld despite the stuck correction.
    assert result["dispatched"] is True
    assert sent == [("%25", "enforcement payload", {})]
    assert not any("send-text" in argv for argv in calls)


async def test_assert_and_send_refuses_when_not_deliverable(app_env, monkeypatch) -> None:
    main = app_env.main
    calls = _patch_subprocess(
        monkeypatch,
        main,
        assert_result={
            "ok": False,
            "action": "launch_failed",
            "reason": "dispatch rc=1",
        },
    )

    result = await main._assert_and_send_custodes("enforcement payload", source="test")

    assert result["dispatched"] is False
    # A dead/launching pane must NOT receive the byte-bearing payload.
    assert not any("send-text" in argv for argv in calls)
