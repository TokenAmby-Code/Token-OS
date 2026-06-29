import asyncio
from pathlib import Path

import pytest

from custodes_state_policy import CustodesIntervention, StateEvent


def _event(event_type: str = "idle_timeout") -> StateEvent:
    return StateEvent(
        event_type=event_type,
        source="pytest_state_hook",
        instance_id="inst-admin-test",
        severity=2,
        payload={"mode": "break"},
    )


def _intervention(
    event_type: str = "idle_timeout", dedupe_key: str = "admin-test-key"
) -> CustodesIntervention:
    return CustodesIntervention(
        event_type=event_type,
        dedupe_key=dedupe_key,
        severity=2,
        prompt="",
        reason="pytest",
        payload={"mode": "break"},
        observed="mode=break",
    )


def test_administratum_log_uses_doctrine_path_and_single_frontmatter(app_env):
    main = app_env.main

    asyncio.run(
        main._append_administratum_log(
            None,
            classification="state",
            event=_event("idle_timeout"),
            intervention=_intervention("idle_timeout", "state-key"),
        )
    )
    # Retry/double-open of the same hook must be idempotent: one hook, one entry.
    asyncio.run(
        main._append_administratum_log(
            None,
            classification="state",
            event=_event("idle_timeout"),
            intervention=_intervention("idle_timeout", "state-key"),
        )
    )
    asyncio.run(
        main._append_administratum_log(
            None,
            classification="enforcement",
            event=_event("phone_distraction_enforce"),
            intervention=_intervention("phone_distraction_enforce", "enforce-key"),
        )
    )

    log_dir = Path(main._administratum_vault_root()) / "Ultramar" / "Logs" / "Mars"
    logs = list(log_dir.glob("administratum-*.md"))
    assert len(logs) == 1
    text = logs[0].read_text(encoding="utf-8")
    assert text.count("---\n") == 2
    assert text.count("# Administratum") == 1
    assert text.count("**state** `idle_timeout`") == 1
    assert text.count("**enforcement** `phone_distraction_enforce`") == 1
    assert not (Path(main._administratum_vault_root()) / "Mars" / "Logs").exists()


def test_administratum_log_append_failure_is_loud(app_env, monkeypatch, tmp_path):
    main = app_env.main
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("blocks mkdir", encoding="utf-8")
    bad_path = blocker / "administratum-2026-06-29.md"
    monkeypatch.setattr(main, "_administratum_log_path", lambda _instance: bad_path)

    with pytest.raises(RuntimeError, match="Administratum record: log append failed"):
        asyncio.run(
            main._append_administratum_log(
                None,
                classification="state",
                event=_event(),
                intervention=_intervention(),
            )
        )
