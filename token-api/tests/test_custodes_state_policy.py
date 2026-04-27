from custodes_state_policy import StateEvent, evaluate_state_event


def _snapshot():
    return {
        "timer": {"current_mode": "break", "break_balance_ms": -12 * 60 * 1000},
        "phone": {"current_app": "slay_the_spire"},
        "desktop": {"current_mode": "gaming"},
    }


def test_v1_triggers_emit_interventions():
    for event_type in [
        "idle_timeout",
        "distraction_timeout",
        "break_exhausted",
        "phone_distraction_blocked",
        "desktop_mode_blocked",
        "enforcement_cascade_started",
    ]:
        intervention = evaluate_state_event(
            StateEvent(event_type=event_type, source="test", payload={"app": "slay_the_spire"}),
            _snapshot(),
        )

        assert intervention is not None
        assert intervention.event_type == event_type
        assert intervention.dedupe_key == f"{event_type}:test:slay_the_spire"
        assert intervention.prompt.startswith(f"State hook: {event_type}.")
        assert "Be direct; do not over-explain." in intervention.prompt


def test_routine_events_are_noop():
    assert (
        evaluate_state_event(
            StateEvent(event_type="timer_tick", source="timer_worker"),
            _snapshot(),
        )
        is None
    )


def test_prompt_includes_relevant_snapshot_fields():
    intervention = evaluate_state_event(
        StateEvent(
            event_type="idle_timeout",
            source="timer_worker",
            payload={"phone_app": "slay_the_spire"},
        ),
        _snapshot(),
    )

    assert intervention is not None
    assert "phone_app=slay_the_spire" in intervention.prompt
    assert "timer_mode=break" in intervention.prompt
    assert "break_balance=-12m" in intervention.prompt


def test_severity_defaults_and_normalizes():
    defaulted = evaluate_state_event(
        StateEvent(event_type="break_exhausted", source="timer_worker"),
        _snapshot(),
    )
    explicit = evaluate_state_event(
        StateEvent(event_type="break_exhausted", source="timer_worker", severity=4),
        _snapshot(),
    )

    assert defaulted is not None
    assert explicit is not None
    assert defaulted.severity == 1
    assert explicit.severity == 4
