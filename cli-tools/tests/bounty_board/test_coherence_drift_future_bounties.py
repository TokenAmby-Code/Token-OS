"""Bounty tests for Coherence Map §4 future-intent side of drift pairs."""

from __future__ import annotations

import importlib
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib"))

pytestmark = [
    pytest.mark.bounty,
    pytest.mark.xfail(
        strict=False,
        reason="bounty: future side of coherence-map §4 drift pair",
    ),
]


def _require_attr(module_name: str, attr_name: str):
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attr_name)
    except AttributeError as exc:  # pragma: no cover - this is the open bounty today.
        pytest.fail(f"open bounty: {module_name}.{attr_name} is not implemented: {exc}")


def test_future_drift_occupancy_persisted_shipped_or_open_row_marks_empty_pane_occupied(tmp_path):
    """Future intent: SHIPPED/OPEN ledger rows occupy panes even with empty stamps/processes."""

    PersistedOccupancyLedger = _require_attr("tmuxctl.occupancy_ledger", "PersistedOccupancyLedger")
    occupancy_for_pane = _require_attr("tmuxctl.occupancy_ledger", "occupancy_for_pane")
    ledger = PersistedOccupancyLedger(tmp_path / "tmuxctld.sqlite")
    ledger.ship(contract_id="c-shipped", pane_id="%70", instance_id="iid-shipped")
    ledger.open(contract_id="c-open", pane_id="%71", instance_id="iid-open")

    empty_tmux = {
        "%70": {"clean": True, "INSTANCE_ID": "", "live_agent": False, "PANE_ID": "mechanicus:7"},
        "%71": {"clean": True, "INSTANCE_ID": "", "live_agent": False, "PANE_ID": "mechanicus:8"},
    }

    assert occupancy_for_pane(ledger, empty_tmux, "%70").occupied is True
    assert occupancy_for_pane(ledger, empty_tmux, "%71").occupied is True


def test_future_drift_runtime_oracle_resolves_canonical_instance_without_matching_stamp(tmp_path):
    """Future intent: canonical ids resolve through daemon/token-api mapping, not @INSTANCE_ID."""

    RuntimeOracle = _require_attr("tmuxctl.runtime_oracle", "RuntimeOracle")
    oracle = RuntimeOracle.from_registrations(
        [
            {
                "canonical_instance_id": "iid-canonical",
                "codex_rollout_id": "rollout-orphan",
                "pane_id": "%72",
                "stamp_instance_id": "rollout-orphan",
                "public_id": "mechanicus:9",
            }
        ]
    )

    resolved = oracle.resolve("iid-canonical")
    assert resolved.pane_id == "%72"
    assert resolved.instance_id == "iid-canonical"
    assert resolved.stamp_instance_id != resolved.instance_id


def test_future_drift_restart_resume_ships_through_tmuxctld_contract_not_dispatch_cli(tmp_path):
    """Future intent: metal restart/resume uses tmuxctld ship + respawn-pane, not dispatch CLI."""

    RestartShipper = _require_attr("tmuxctl.restart_shipper", "RestartShipper")
    shipper = RestartShipper()

    result = shipper.resume(
        pane_id="%73",
        instance_id="iid-resume",
        engine="codex",
        cwd="/work/resume",
    )

    assert result.transport == "tmuxctld-contract"
    assert result.tmux_command[:3] == ("respawn-pane", "-k", "-t")
    assert "dispatch" not in " ".join(result.argv)


def test_future_drift_public_identity_comes_from_daemon_oracle_when_pane_stamp_missing(tmp_path):
    """Future intent: managed panes get public identity from daemon ledger despite missing @PANE_ID."""

    RuntimeOracle = _require_attr("tmuxctl.runtime_oracle", "RuntimeOracle")
    oracle = RuntimeOracle.from_registrations(
        [
            {
                "pane_id": "%74",
                "public_id": "mechanicus:10",
                "pane_option_public_id": "",
                "instance_id": "iid-10",
            }
        ]
    )

    resolved = oracle.resolve("%74")
    assert resolved.public_id == "mechanicus:10"
    assert resolved.public_identity_source == "daemon-ledger"


def test_future_drift_singleton_has_registered_identity_after_restart_not_only_guard_exclusion(
    tmp_path,
):
    """Future intent: singleton churn repairs identity, not just hard-excludes empty labels."""

    SingletonSeatSupervisor = _require_attr("tmuxctl.registration", "SingletonSeatSupervisor")
    supervisor = SingletonSeatSupervisor(tmp_path / "tmuxctld.sqlite")

    repaired = supervisor.reconcile_after_restart(public_id="legion:custodes", pane_id="%75")

    assert repaired.instance_id
    assert repaired.tint_applied is True
    assert repaired.protection_reason != "empty-stamp-singleton-hard-exclusion"


def test_future_drift_agent_navigation_endpoint_uses_triple_id_oracle_instead_of_direct_tmux_mutation():
    """Future intent: agent-facing navigation/control routes through tmuxctld oracle endpoints."""

    NavigationEndpoint = _require_attr("tmuxctl.navigation_endpoint", "NavigationEndpoint")
    endpoint = NavigationEndpoint()

    result = endpoint.select(target_id="iid-worker-1", direction="right", mode="relative")

    assert result.resolved_by == "runtime-oracle"
    assert result.transport == "tmuxctld"
    assert result.direct_tmux_focus_mutation is False


def test_future_drift_routing_storage_rejects_persisted_raw_pane_ids_but_allows_final_resolution():
    """Future intent: storage APIs reject raw %pane ids; only final operation may resolve them."""

    RouteStorage = _require_attr("tmuxctl.routing_contracts", "RouteStorage")
    routes = RouteStorage()

    with pytest.raises(ValueError, match="raw physical pane ids are not persistent routes"):
        routes.save_route(name="cadia", target="%11")

    routes.save_route(name="cadia", target="legion:custodes")
    final = routes.resolve_for_final_operation("cadia", resolver=lambda _target: "%11")
    assert final.physical_pane_id == "%11"
    assert routes.get("cadia").target == "legion:custodes"
