"""Bounty tests for Coherence Map §3 vault-feature candidates.

These tests describe future vault-intended tmuxctld/runtime behavior. They are
advisory bounty-board tests: current code is expected to fail them until the
feature is deliberately built, at which point an XPASS means the bounty should be
graduated into the blocking regression suite.
"""

from __future__ import annotations

import importlib
import pathlib
import sys
from dataclasses import dataclass

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib"))

pytestmark = [
    pytest.mark.bounty,
    pytest.mark.xfail(
        strict=False,
        reason="bounty: future vault feature from coherence-map §3",
    ),
]


@dataclass(frozen=True)
class RuntimeTarget:
    """Expected canonical answer shape for the future runtime oracle."""

    instance_id: str
    pane_id: str
    public_id: str
    persona_id: str


def _require_attr(module_name: str, attr_name: str):
    """Load an intended future API inside the test body so xfail can catch it."""

    module = importlib.import_module(module_name)
    try:
        return getattr(module, attr_name)
    except AttributeError as exc:  # pragma: no cover - this is the open bounty today.
        pytest.fail(f"open bounty: {module_name}.{attr_name} is not implemented: {exc}")


def test_future_runtime_oracle_accepts_instance_pane_and_singleton_ids_interchangeably():
    """Future intent: one tmuxctld oracle resolves instance, pane, and singleton ids.

    Current behavior is split across stamp-only instance resolution, public pane
    ids, and singleton labels; there is no generic triple-ID runtime oracle.
    """

    RuntimeOracle = _require_attr("tmuxctl.runtime_oracle", "RuntimeOracle")
    oracle = RuntimeOracle.from_rows(
        [
            RuntimeTarget(
                instance_id="iid-custodes",
                pane_id="%11",
                public_id="legion:custodes",
                persona_id="custodes",
            )
        ]
    )

    by_instance = oracle.resolve("iid-custodes")
    by_pane = oracle.resolve("legion:custodes")
    by_singleton = oracle.resolve("custodes")

    assert by_instance == by_pane == by_singleton
    assert by_instance.pane_id == "%11"
    assert by_instance.public_id == "legion:custodes"


def test_future_persisted_occupancy_ledger_state_machine_defines_occupied_semantics(tmp_path):
    """Future intent: persisted SHIPPED/OPEN/CLOSED rows, not live stamps, own occupancy."""

    PersistedOccupancyLedger = _require_attr("tmuxctl.occupancy_ledger", "PersistedOccupancyLedger")
    ledger = PersistedOccupancyLedger(tmp_path / "tmuxctld.sqlite")

    ledger.ship(contract_id="c1", pane_id="%20", instance_id="iid-20")
    assert ledger.status("c1") == "SHIPPED"
    assert ledger.occupied("%20") is True

    ledger.open(contract_id="c1")
    assert ledger.status("c1") == "OPEN"
    assert ledger.occupied("%20") is True

    ledger.close(contract_id="c1", reason="wrapper-end")
    assert ledger.status("c1") == "CLOSED"
    assert ledger.occupied("%20") is False


def test_future_boot_reconcile_prunes_dead_persisted_ledger_rows(tmp_path):
    """Future intent: tmuxctld boot reconcile prunes dead-pane ledger occupancy once."""

    PersistedOccupancyLedger = _require_attr("tmuxctl.occupancy_ledger", "PersistedOccupancyLedger")
    ledger = PersistedOccupancyLedger(tmp_path / "tmuxctld.sqlite")
    ledger.ship(contract_id="dead", pane_id="%404", instance_id="iid-dead")
    ledger.open(contract_id="live", pane_id="%21", instance_id="iid-live")

    pruned = ledger.prune_dead_panes(live_pane_ids={"%21"}, reason="boot-reconcile")

    assert pruned == ["dead"]
    assert ledger.status("dead") == "CLOSED"
    assert ledger.occupied("%404") is False
    assert ledger.occupied("%21") is True


def test_future_identity_reads_are_ledger_backed_without_instance_stamp(tmp_path):
    """Future intent: identity facts move off pane @ stamps into the daemon ledger."""

    PersistedOccupancyLedger = _require_attr("tmuxctl.occupancy_ledger", "PersistedOccupancyLedger")
    RuntimeOracle = _require_attr("tmuxctl.runtime_oracle", "RuntimeOracle")
    ledger = PersistedOccupancyLedger(tmp_path / "tmuxctld.sqlite")
    ledger.upsert_identity(
        pane_id="%30",
        instance_id="iid-ledger-only",
        public_id="mechanicus:3",
        wrapper_launch_id="wrap-3",
        engine="codex",
        launcher="dispatch",
        persona="mechanicus-worker",
        born_epoch=1780000000,
    )

    oracle = RuntimeOracle(ledger=ledger, tmux_rows=[{"pane_id": "%30", "INSTANCE_ID": ""}])

    resolved = oracle.resolve("iid-ledger-only")
    assert resolved.pane_id == "%30"
    assert resolved.public_id == "mechanicus:3"
    assert resolved.identity_source == "daemon-ledger"


def test_future_daemon_ship_accepts_contract_respawns_and_returns_bare_accepted():
    """Future intent: token-api creates pane-free contract; tmuxctld ships via respawn-pane."""

    TmuxControlPlane = _require_attr("tmuxctl.service", "TmuxControlPlane")

    class Adapter:
        def __init__(self):
            self.commands: list[tuple[str, ...]] = []

        def run(self, *args: str, **_kwargs):
            self.commands.append(args)
            return ""

    adapter = Adapter()
    plane = TmuxControlPlane(adapter=adapter)

    result = plane.ship_contract(
        {
            "contract_id": "contract-1",
            "instance_id": "iid-new",
            "engine": "codex",
            "cwd": "/work/tree",
            "target": "mechanicus:4",
        }
    )

    assert result == {"accepted": True, "contract_id": "contract-1"}
    assert any(cmd[:3] == ("respawn-pane", "-k", "-t") for cmd in adapter.commands)


def test_future_wrapper_close_ping_is_fire_and_forget_and_pane_died_closes_row(tmp_path):
    """Future intent: WrapperEnd close ping never stalls; pane-died is authoritative backstop."""

    WrapperLifecycleLedger = _require_attr("tmuxctl.occupancy_ledger", "WrapperLifecycleLedger")
    ledger = WrapperLifecycleLedger(tmp_path / "tmuxctld.sqlite")
    ledger.open(wrapper_launch_id="wrap-9", pane_id="%9", instance_id="iid-9")

    close_future = ledger.wrapper_end_async(wrapper_launch_id="wrap-9", timeout_seconds=0.01)
    assert close_future.fire_and_forget is True
    assert ledger.occupied("%9") is True  # ping has not been awaited as authority

    ledger.pane_died(pane_id="%9")
    assert ledger.occupied("%9") is False
    assert ledger.closed_by("wrap-9") == "pane-died"


def test_future_no_prewarm_wrapper_and_session_registration_converge_both_systems(tmp_path):
    """Future intent: wrapper ping + SessionStart ping are sufficient without prewarm."""

    RegistrationCoordinator = _require_attr("tmuxctl.registration", "RegistrationCoordinator")
    coordinator = RegistrationCoordinator(tmp_path / "tmuxctld.sqlite")

    coordinator.wrapper_start(wrapper_launch_id="wrap-a", pane_id="%40", public_id="mechanicus:4")
    coordinator.session_start(
        wrapper_launch_id="wrap-a",
        session_id="iid-a",
        engine="codex",
        cwd="/work/a",
    )

    assert coordinator.daemon_identity("%40").instance_id == "iid-a"
    assert coordinator.token_api_instance("iid-a").pane_id == "%40"
    assert coordinator.token_api_instance("iid-a").registration_source == "SessionStart"


def test_future_singleton_identity_survives_resume_restart_churn(tmp_path):
    """Future intent: singleton seats reacquire valid identity instead of relying on guard rails."""

    SingletonSeatSupervisor = _require_attr("tmuxctl.registration", "SingletonSeatSupervisor")
    supervisor = SingletonSeatSupervisor(tmp_path / "tmuxctld.sqlite")

    supervisor.register_singleton(
        persona_id="custodes",
        public_id="legion:custodes",
        pane_id="%11",
        instance_id="iid-old",
    )
    supervisor.wrapper_end(wrapper_launch_id="wrap-old", pane_id="%11")
    supervisor.wrapper_start(
        wrapper_launch_id="wrap-new", pane_id="%11", public_id="legion:custodes"
    )
    supervisor.session_start(wrapper_launch_id="wrap-new", session_id="iid-new")

    identity = supervisor.identity_for("legion:custodes")
    assert identity.instance_id == "iid-new"
    assert identity.pane_id == "%11"
    assert identity.routing_key == "council:custodes"


def test_future_codex_resume_maps_rollout_id_to_canonical_instance_id(tmp_path):
    """Future intent: Codex rollout ids map to token-api canonical instance ids on resume."""

    CodexResumeIdentityMap = _require_attr("tmuxctl.registration", "CodexResumeIdentityMap")
    mapping = CodexResumeIdentityMap(tmp_path / "tmuxctld.sqlite")

    mapping.session_start(
        canonical_instance_id="iid-canonical",
        codex_rollout_id="rollout-2026-06-29T10-00-00-abc",
        pane_id="%55",
    )

    resolved = mapping.resolve("rollout-2026-06-29T10-00-00-abc")
    assert resolved.instance_id == "iid-canonical"
    assert resolved.pane_id == "%55"
    assert mapping.resolve("iid-canonical") == resolved


def test_future_dispatcher_freelist_consumes_daemon_ledger_not_stamp_scan(tmp_path):
    """Future intent: dispatcher/freelist reads daemon occupancy ledger, not just stamps/process."""

    PersistedOccupancyLedger = _require_attr("tmuxctl.occupancy_ledger", "PersistedOccupancyLedger")
    list_free_panes_from_ledger = _require_attr("tmuxctl.occupancy_ledger", "list_free_panes")
    ledger = PersistedOccupancyLedger(tmp_path / "tmuxctld.sqlite")
    ledger.ship(contract_id="busy", pane_id="%60", instance_id="iid-busy")

    tmux_rows = [
        {"pane_id": "%60", "clean": True, "INSTANCE_ID": "", "PANE_ID": "mechanicus:6"},
        {"pane_id": "%61", "clean": True, "INSTANCE_ID": "", "PANE_ID": "mechanicus:7"},
    ]

    free = list_free_panes_from_ledger(ledger=ledger, tmux_rows=tmux_rows)
    assert [pane.pane_id for pane in free] == ["%61"]


def test_future_voice_routes_store_public_targets_and_resolve_physical_only_at_ship():
    """Future intent: voice/static routes persist public ids, never volatile %pane ids."""

    VoiceRouteStore = _require_attr("tmuxctl.voice_routes", "VoiceRouteStore")
    store = VoiceRouteStore()

    with pytest.raises(ValueError, match="public tmuxctl target"):
        store.put(bot_name="cadia", target="%11")

    store.put(bot_name="cadia", target="legion:custodes")
    assert store.get("cadia").stored_target == "legion:custodes"
    assert store.get("cadia").physical_pane_id is None

    shipped = store.ship(
        bot_name="cadia", resolver=lambda public: "%11" if public == "legion:custodes" else None
    )
    assert shipped.physical_pane_id == "%11"
    assert store.get("cadia").physical_pane_id is None


def test_future_daemon_dispatch_refuses_vanished_or_wrong_pane_before_prompt_bytes():
    """Future intent: dead/wrong dispatch targets fail before any prompt bytes are sent."""

    TmuxControlPlane = _require_attr("tmuxctl.service", "TmuxControlPlane")

    class Adapter:
        def __init__(self):
            self.sent: list[str] = []

        def run(self, *args: str, **_kwargs):
            if args[:3] == ("display-message", "-t", "%dead"):
                return ""  # vanished after contract creation
            return ""

        def send_keys(self, target: str, *keys: str):
            self.sent.append(target)

    adapter = Adapter()
    plane = TmuxControlPlane(adapter=adapter)

    with pytest.raises(ValueError, match="target vanished|target not found"):
        plane.ship_contract(
            {
                "contract_id": "dead-contract",
                "instance_id": "iid-dead",
                "target": "%dead",
                "prompt": "do work",
            }
        )

    assert adapter.sent == []
