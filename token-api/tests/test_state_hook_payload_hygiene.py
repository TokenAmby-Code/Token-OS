from custodes_state_policy import StateEvent, evaluate_state_event


def test_internal_ack_ids_become_ack_source_not_app_or_phone_app(app_env):
    for source, internal_name in (
        ("askq_ladder", "askuserquestion"),
        ("golden_throne", "golden_throne"),
    ):
        payload = app_env.main._enforcement_state_payload(
            source=source,
            ack_source=internal_name,
        )

        assert payload["ack_source"] == internal_name
        assert payload["phone_app"] is None
        assert "app" not in payload

        intervention = evaluate_state_event(
            StateEvent(
                event_type="enforcement_cascade_started",
                source=source,
                payload=payload,
            ),
            {"phone": {"current_app": "slay_the_spire"}},
        )
        assert intervention is not None
        assert f"ack_source={internal_name}" in intervention.prompt
        assert f"app={internal_name}" not in intervention.prompt
        assert f"phone_app={internal_name}" not in intervention.prompt
        assert "phone_app=slay_the_spire" not in intervention.prompt


def test_phone_telemetry_populates_phone_app_not_ack_source(app_env):
    payload = app_env.main._enforcement_state_payload(source="phone", app="slay_the_spire")

    assert payload["app"] == "slay_the_spire"
    assert payload["phone_app"] == "slay_the_spire"
    assert "ack_source" not in payload

    intervention = evaluate_state_event(
        StateEvent(
            event_type="enforcement_cascade_started",
            source="phone",
            payload=payload,
        ),
        {},
    )
    assert intervention is not None
    assert "phone_app=slay_the_spire" in intervention.prompt
    assert "ack_source=slay_the_spire" not in intervention.prompt
