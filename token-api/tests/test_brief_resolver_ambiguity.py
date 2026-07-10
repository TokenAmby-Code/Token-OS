"""Cluster A P0: council:custodes-addressed sends must never pick a pane
silently when the semantic label is ambiguous, and blind retries of the same
logical send must collapse onto one operation id even if resolution drifts.

Red pack for Mars/Bugs/custodes-addressed-worker-reports-misdelivered-into-
malcador-pane: duplicate/churned @PANE_ID stamps made token-api's exact-match
scan first-writer-wins (silent wrong-recipient delivery), and the pane-scoped
auto idempotency key minted a fresh operation id per resolved pane, so a blind
retry that re-resolved differently double-delivered (redelivery storm).
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import talk  # noqa: E402


def _pane_row(pane_id: str, position_id: str) -> dict[str, str]:
    return {
        "pane_id": pane_id,
        "position_id": position_id,
        "session": "main",
        "window_index": "4",
        "window_name": "council",
    }


def _council_rows(custodes_panes: list[str]) -> list[dict[str, str]]:
    rows = [_pane_row(p, "council:custodes") for p in custodes_panes]
    rows.append(_pane_row("%29", "council:pax"))
    rows.append(_pane_row("%30", "council:malcador"))
    return rows


def _patch_scan(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, str]]) -> None:
    async def fake_scan() -> list[dict[str, str]]:
        return rows

    monkeypatch.setattr(talk, "_tmux_list_panes", fake_scan)


def _forbid_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_fallback(_target):
        raise AssertionError(
            "ambiguous label must fail loud, not fall through to the tmuxctld fallback"
        )

    monkeypatch.setattr(talk.shared, "resolve_tmux_pane_id", _no_fallback)


def test_resolve_pane_custodes_happy_path_returns_custodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DoD 4(a): unique stamps resolve council:custodes to the Custodes pane."""
    _patch_scan(monkeypatch, _council_rows(["%28"]))
    assert asyncio.run(talk.resolve_pane("council:custodes")) == "%28"
    assert asyncio.run(talk.resolve_pane("council:malcador")) == "%30"


def test_resolve_pane_duplicate_custodes_labels_fail_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The misroute red: two panes claim council:custodes → raise, do not pick
    the first enumerated pane, do not consult weaker fallbacks."""
    _patch_scan(monkeypatch, _council_rows(["%31", "%28"]))
    _forbid_fallback(monkeypatch)
    with pytest.raises(ValueError, match="ambiguous"):
        asyncio.run(talk.resolve_pane("council:custodes"))


def test_resolve_brief_targets_surfaces_ambiguity_as_loud_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """brief --pane council:custodes with duplicate stamps must deliver to ZERO
    panes and report the ambiguity, not misdeliver."""
    _patch_scan(monkeypatch, _council_rows(["%31", "%28"]))
    _forbid_fallback(monkeypatch)
    resolved, unresolved = asyncio.run(
        talk.resolve_brief_targets(panes=["council:custodes"], pages=None)
    )
    assert resolved == []
    assert len(unresolved) == 1
    assert unresolved[0]["spec"] == "council:custodes"
    assert "ambiguous" in unresolved[0]["reason"]


def test_blind_retry_same_spec_keeps_one_operation_id_across_resolution_drift(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DoD 3 red: the SAME logical brief (same spec, same payload) retried after
    resolution drifted to a different physical pane must carry the SAME
    operation id, so the daemon idempotency layer can collapse or loudly refuse
    it — never silently double-deliver as a fresh operation."""

    async def _run() -> None:
        main = app_env.main
        captured_ops: list[str | None] = []

        def _targets_for(pane_id: str):
            async def _targets(**_kwargs):
                return (
                    [
                        {
                            "pane_id": pane_id,
                            "position_id": "council:custodes",
                            "source": "pane",
                            "spec": "council:custodes",
                        }
                    ],
                    [],
                )

            return _targets

        async def _rowless(_pane):
            return None

        async def _direct(pane_id, payload, **kwargs):
            captured_ops.append(kwargs.get("operation_id"))
            return {"status": main.PANE_WRITE_SENT, "tmux_pane": pane_id}

        async def _not_custodes(_pane):
            return False

        monkeypatch.setattr(main.talk_service, "lookup_instance_for_pane", _rowless)
        monkeypatch.setattr(main, "_direct_tmux_pane_delivery", _direct)
        monkeypatch.setattr(main, "_pane_sender_is_custodes", _not_custodes)

        payload = "FINAL report for council:custodes"

        monkeypatch.setattr(main.talk_service, "resolve_brief_targets", _targets_for("%28"))
        await main.brief_send(main.BriefSendRequest(panes=["council:custodes"], payload=payload))

        # Blind retry after stamp churn: same spec + payload now resolves to %30.
        monkeypatch.setattr(main.talk_service, "resolve_brief_targets", _targets_for("%30"))
        await main.brief_send(main.BriefSendRequest(panes=["council:custodes"], payload=payload))

        assert len(captured_ops) == 2
        assert captured_ops[0], "brief must always derive an operation id"
        assert captured_ops[0] == captured_ops[1], (
            "pane-scoped operation ids mint a fresh identity per resolved pane; "
            "a blind retry of the same logical send must reuse the same operation id"
        )

    asyncio.run(_run())
