from custodes_state_policy import StateEvent, build_dedupe_key, evaluate_state_event


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
        assert "AFK rule" in intervention.prompt
        assert "TTS" in intervention.prompt
        assert "Do NOT reply with in-thread text only" in intervention.prompt


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


def test_prompt_includes_enriched_snapshot_fields():
    snapshot = {
        "timer": {"current_mode": "break", "break_balance_ms": -12 * 60 * 1000},
        "phone": {"current_app": "slay_the_spire"},
        "desktop": {"current_mode": "gaming"},
        "cascade_count_today": 4,
        "open_panes": 2,
        "active_threads": {"count": 1, "names": ["legion-a"]},
    }
    intervention = evaluate_state_event(
        StateEvent(
            event_type="enforcement_cascade_started",
            source="phone",
            payload={"app": "slay_the_spire", "phone_app": "slay_the_spire"},
        ),
        snapshot,
    )

    assert intervention is not None
    assert "cascades_today=4" in intervention.prompt
    assert "open_panes=2" in intervention.prompt
    assert "active_threads=1" in intervention.prompt
    assert "thread_names=legion-a" in intervention.prompt


def test_internal_enforcement_sources_are_labeled_as_ack_sources():
    for source, internal_name in (
        ("askq_ladder", "askuserquestion"),
        ("golden_throne", "golden_throne"),
    ):
        intervention = evaluate_state_event(
            StateEvent(
                event_type="enforcement_cascade_started",
                source=source,
                payload={"ack_source": internal_name, "phone_app": None},
            ),
            {"phone": {"current_app": "slay_the_spire"}},
        )

        assert intervention is not None
        assert f"ack_source={internal_name}" in intervention.prompt
        assert f"app={internal_name}" not in intervention.prompt
        assert f"phone_app={internal_name}" not in intervention.prompt
        assert "phone_app=slay_the_spire" not in intervention.prompt


def test_phone_source_app_still_labels_as_phone_app():
    intervention = evaluate_state_event(
        StateEvent(
            event_type="enforcement_cascade_started",
            source="phone",
            payload={"app": "slay_the_spire"},
        ),
        {},
    )

    assert intervention is not None
    assert "phone_app=slay_the_spire" in intervention.prompt


def test_cascade_escalate_emits_intervention_with_level_dedupe():
    event = StateEvent(
        event_type="enforcement_cascade_escalate",
        source="phone",
        severity=5,
        payload={"app": "x", "phone_app": "x", "level": 3, "elapsed_s": 42},
    )
    intervention = evaluate_state_event(event, {})

    assert intervention is not None
    assert intervention.event_type == "enforcement_cascade_escalate"
    assert "level=3" in intervention.prompt
    assert build_dedupe_key(event).endswith(":level=3")
    assert intervention.dedupe_key.endswith(":level=3")


def test_expected_ack_escalated_emits_intervention_with_ack_level_dedupe():
    event = StateEvent(
        event_type="expected_ack_escalated",
        source="desktop_gaming",
        instance_id="686060",
        severity=3,
        payload={
            "ack_id": "abc-123",
            "level": 2,
            "reason": "Mewgenics turn ended during work",
            "app": "Mewgenics",
        },
    )
    intervention = evaluate_state_event(event, _snapshot())

    assert intervention is not None
    assert intervention.event_type == "expected_ack_escalated"
    dedupe = build_dedupe_key(event)
    assert dedupe == "expected_ack_escalated:desktop_gaming:Mewgenics:ack=abc-123:level=2"
    assert intervention.dedupe_key == dedupe
    assert "level=2" in intervention.prompt
    assert "Mewgenics" in intervention.prompt or "phone_app=" in intervention.prompt


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
