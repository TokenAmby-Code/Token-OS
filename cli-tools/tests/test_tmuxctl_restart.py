from __future__ import annotations

import pathlib
import sys
from datetime import UTC, datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.api import build_client_attachments
from tmuxctl.builder import (
    PERSONA_WINDOWS,
    _window_dir,
    build_council_window,
    build_mechanicus_window,
    build_workspace,
)
from tmuxctl.enums import (
    AttachmentClass,
    CoherenceSeverity,
    GridState,
    InstanceStatus,
    PaneKind,
    ResumeDisposition,
    WindowArchetype,
)
from tmuxctl.executor import RestartExecutor
from tmuxctl.models import (
    GroupedSessionSnapshot,
    InstanceRegistryEntry,
    InstanceRegistrySnapshot,
    PaneSnapshot,
    PlannedResume,
    WindowSnapshot,
    WorkspaceSnapshot,
)
from tmuxctl.planner import build_restart_plan


def _pane(
    pane_id: str,
    role: str,
    *,
    command: str = "zsh",
    window: str = "somnium",
    stamp_instance_id: str = "",
    cwd: str = "/Volumes/Imperium/Imperium-ENV",
    runtime_engine: str = "",
) -> PaneSnapshot:
    return PaneSnapshot(
        pane_id=pane_id,
        session_name="main",
        window_index=1,
        window_name=window,
        pane_index=0,
        width=100,
        height=40,
        current_command=command,
        tty="/dev/ttys001",
        pane_role=role,
        grid_state=GridState.SMALL,
        pane_kind=PaneKind.UNKNOWN,
        reserved=False,
        active=False,
        instance_id=stamp_instance_id,
        cwd=cwd,
        runtime_engine=runtime_engine,
    )


def _workspace(
    *panes: PaneSnapshot,
    window: str = "somnium",
    archetype: WindowArchetype = WindowArchetype.SOMNIUM,
) -> WorkspaceSnapshot:
    window_snapshot = WindowSnapshot(
        session_name="main",
        window_index=1,
        window_name=window,
        archetype=archetype,
        focused=False,
        grid_expanded="none",
        grid_stash="",
        side_expanded="none",
        panes=tuple(panes),
    )
    return WorkspaceSnapshot(session_name="main", windows=(window_snapshot,))


def _instance(
    instance_id: str,
    pane_label: str,
    *,
    status: InstanceStatus = InstanceStatus.IDLE,
    pre_stop_status: InstanceStatus = InstanceStatus.IDLE,
    last_activity: str | None = None,
    stopped_at: str = "",
    tmux_pane: str = "%1",
    is_subagent: bool = False,
    legion: str = "",
    rank: str = "",
    engine: str = "",
) -> InstanceRegistryEntry:
    return InstanceRegistryEntry(
        instance_id=instance_id,
        device_id="Mac-Mini",
        pane_label=pane_label,
        tmux_pane=tmux_pane,
        working_dir="/Volumes/Imperium/Imperium-ENV",
        status=status,
        pre_stop_status=pre_stop_status,
        is_subagent=is_subagent,
        legion=legion,
        rank=rank,
        engine=engine,
        last_activity=last_activity if last_activity is not None else _iso_ago(hours=1),
        stopped_at=stopped_at,
    )


def _iso_ago(*, hours: int = 0, seconds: int = 0) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours, seconds=seconds)).isoformat()


def test_restart_plan_dedupes_by_pane_label_and_keeps_newest():
    workspace = _workspace(
        _pane("%1", "somnium:NW", stamp_instance_id="old"),
        _pane("%2", "somnium:NW", stamp_instance_id="new"),
    )
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(
            _instance("old", "somnium:NW", last_activity=_iso_ago(hours=2)),
            _instance("new", "somnium:NW", last_activity=_iso_ago(hours=1)),
        ),
    )

    plan = build_restart_plan(workspace, registry)

    assert [resume.instance_id for resume in plan.resumes] == ["new"]
    assert any(
        issue.code == "duplicate_pane_label" and issue.severity is CoherenceSeverity.WARNING
        for issue in plan.coherence_issues
    )


def test_restart_plan_ignores_stale_stopped_duplicate_claims():
    workspace = _workspace(
        _pane("%1", "somnium:NW", stamp_instance_id="old-stopped"),
        _pane("%2", "somnium:NW", stamp_instance_id="active"),
    )
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(
            _instance(
                "old-stopped",
                "somnium:NW",
                status=InstanceStatus.STOPPED,
                stopped_at=_iso_ago(hours=1),
                tmux_pane="%9",
            ),
            _instance("active", "somnium:NW", last_activity=_iso_ago(seconds=30)),
        ),
    )

    plan = build_restart_plan(workspace, registry)

    assert [resume.instance_id for resume in plan.resumes] == ["active"]
    assert any(issue.code == "duplicate_pane_label" for issue in plan.coherence_issues)


def test_restart_plan_ignores_db_only_recent_stop_and_stale_activity():
    workspace = _workspace(_pane("%1", "somnium:NW"), _pane("%2", "somnium:NE"))
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(
            _instance("stale", "somnium:NW", last_activity=_iso_ago(hours=72)),
            _instance(
                "recent-stop",
                "somnium:NE",
                status=InstanceStatus.STOPPED,
                pre_stop_status=InstanceStatus.PROCESSING,
                stopped_at=_iso_ago(seconds=30),
                tmux_pane="%9",
            ),
        ),
    )

    plan = build_restart_plan(workspace, registry)

    assert plan.resumes == ()


def test_restart_plan_ignores_unmanaged_blank_pane_roles() -> None:
    # Transient/unmanaged windows in the main session (e.g. stash/verify panes)
    # can have no @PANE_ID. They must not crash restart planning.
    workspace = _workspace(
        _pane("%1", "somnium:N", stamp_instance_id="live"),
        _pane("%2", None),  # type: ignore[arg-type]
    )
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(_instance("live", "somnium:N", tmux_pane="%1"),),
    )

    plan = build_restart_plan(workspace, registry)

    assert [(r.instance_id, r.pane_label) for r in plan.resumes] == [("live", "somnium:N")]


def test_restart_plan_resumes_live_stopped_pane_and_continues_if_processing():
    workspace = _workspace(_pane("%2", "somnium:NE", stamp_instance_id="recent-stop"))
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(
            _instance(
                "recent-stop",
                "somnium:NE",
                status=InstanceStatus.STOPPED,
                pre_stop_status=InstanceStatus.PROCESSING,
                stopped_at=_iso_ago(hours=3),
                tmux_pane="%9",
            ),
        ),
    )

    plan = build_restart_plan(workspace, registry)

    assert [resume.instance_id for resume in plan.resumes] == ["recent-stop"]
    assert plan.resumes[0].disposition is ResumeDisposition.RESUME_AND_CONTINUE


def test_restart_plan_flags_busy_targets_without_persisting_pane_id_drift():
    workspace = _workspace(_pane("%2", "somnium:NW", command="claude", stamp_instance_id="abc"))
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(_instance("abc", "somnium:NW", tmux_pane="%1"),),
    )

    plan = build_restart_plan(workspace, registry)

    codes = {issue.code: issue.severity for issue in plan.coherence_issues}
    assert "pane_id_mismatch" not in codes
    assert codes["target_busy"] is CoherenceSeverity.WARNING
    assert not plan.has_errors


def test_restart_plan_flags_codex_targets_busy():
    workspace = _workspace(_pane("%2", "somnium:NW", command="codex", stamp_instance_id="abc"))
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(_instance("abc", "somnium:NW", tmux_pane="%2"),),
    )

    plan = build_restart_plan(workspace, registry)

    codes = {issue.code: issue.severity for issue in plan.coherence_issues}
    assert codes["target_busy"] is CoherenceSeverity.WARNING
    assert not plan.has_errors


def test_restart_plan_marks_promoted_custodes_for_council_tombstone():
    workspace = _workspace(_pane("%2", "somnium:NE", stamp_instance_id="custodes"))
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(_instance("custodes", "somnium:NE", tmux_pane="%2", legion="custodes"),),
    )

    plan = build_restart_plan(workspace, registry)

    assert plan.resumes[0].pane_label == "somnium:NE"
    # Custodes' canonical seat moved from legion to the council page.
    assert plan.resumes[0].tombstone_role == "council:custodes"


def test_restart_plan_marks_promoted_fabricator_for_mechanicus_tombstone():
    workspace = _workspace(_pane("%2", "palace:N", window="palace", stamp_instance_id="fabricator"))
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(_instance("fabricator", "palace:NE", tmux_pane="%2", legion="fabricator"),),
    )

    plan = build_restart_plan(workspace, registry)

    assert plan.resumes[0].pane_label == "palace:N"
    assert plan.resumes[0].tombstone_role == "mechanicus:fabricator-general"


def test_db_only_hidden_palace_side_is_not_planned():
    workspace = _workspace(
        _pane("%1", "palace:N", window="palace"),
        _pane("%3", "palace:S", window="palace"),
        window="palace",
        archetype=WindowArchetype.PALACE,
    )
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(_instance("abc", "palace:W", tmux_pane="%99"),),
    )

    plan = build_restart_plan(workspace, registry)

    assert plan.resumes == ()


def test_palace_happy_path_resumes_grid_and_side_labels():
    workspace = _workspace(
        _pane("%1", "palace:W", window="palace"),
        _pane("%2", "palace:N", window="palace", stamp_instance_id="alpha"),
        _pane("%3", "palace:S", window="palace"),
        _pane("%6", "palace:E", window="palace", stamp_instance_id="beta"),
        window="palace",
        archetype=WindowArchetype.PALACE,
    )
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(
            _instance("alpha", "palace:N", tmux_pane="%2"),
            _instance("beta", "palace:E", tmux_pane="%6"),
        ),
    )

    plan = build_restart_plan(workspace, registry)

    resumed = {
        resume.instance_id: (resume.pane_label, resume.target_pane_id) for resume in plan.resumes
    }
    assert resumed == {"alpha": ("palace:N", ""), "beta": ("palace:E", "")}
    assert all(not resume.target_hidden_until_rebuild for resume in plan.resumes)


def test_restart_plan_backfills_pane_label_from_live_instance_stamp():
    # Cutover Slice B (#84) retired pane_label/tmux_pane from /api/instances, so
    # the registry now arrives with an empty pane_label. The live @INSTANCE_ID
    # pane stamp is the reverse-lookup source of truth and must rehydrate the
    # resume target; otherwise _is_candidate drops every instance and nothing
    # resumes across a tx restart.
    workspace = _workspace(
        _pane("%1", "palace:W", window="palace", stamp_instance_id="ghost"),
        _pane("%2", "palace:N", window="palace"),
        window="palace",
        archetype=WindowArchetype.PALACE,
    )
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(_instance("ghost", "", tmux_pane=""),),
    )

    plan = build_restart_plan(workspace, registry)

    assert [(r.instance_id, r.pane_label, r.target_pane_id) for r in plan.resumes] == [
        ("ghost", "palace:W", "")
    ]


def test_restart_plan_prefers_live_stamp_label_over_stale_registry_label():
    # Restart restore follows the live pane snapshot. A stale DB pane_label must
    # not move a still-open pane to a different target.
    workspace = _workspace(
        _pane("%1", "palace:W", window="palace", stamp_instance_id="abc"),
        _pane("%2", "palace:N", window="palace"),
        window="palace",
        archetype=WindowArchetype.PALACE,
    )
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(_instance("abc", "palace:N", tmux_pane="%2"),),
    )

    plan = build_restart_plan(workspace, registry)

    assert [(r.instance_id, r.pane_label, r.target_pane_id) for r in plan.resumes] == [
        ("abc", "palace:W", "")
    ]


def test_build_client_attachments_classifies_local_remote_and_grouped():
    managed = (
        GroupedSessionSnapshot("main", "main", 0, "somnium"),
        GroupedSessionSnapshot("phone", "main", 2, "somnium"),
    )
    attachments = build_client_attachments(
        [
            {
                "client_tty": "/dev/ttys001",
                "session_name": "main",
                "client_name": "local",
                "window_index": "0",
                "window_name": "somnium",
            },
            {
                "client_tty": "/dev/pts/4",
                "session_name": "phone",
                "client_name": "remote",
                "window_index": "2",
                "window_name": "somnium",
            },
        ],
        managed_sessions=managed,
    )

    assert attachments[0].attachment_class is AttachmentClass.LOCAL_LEADER
    assert attachments[1].attachment_class is AttachmentClass.REMOTE_GROUPED


def test_restart_executor_uses_public_label_not_stale_pane_id() -> None:
    resume = PlannedResume(
        instance_id="abc123",
        pane_label="council:administratum",
        target_pane_id="%73",
        working_dir="/Volumes/Imperium/Imperium-ENV",
        disposition=ResumeDisposition.RESUME,
        reason="active",
    )

    target = RestartExecutor()._resume_target_ref(resume)

    assert target == "council:administratum"


def test_restart_executor_does_not_fall_back_to_internal_pane_id() -> None:
    resume = PlannedResume(
        instance_id="abc123",
        pane_label="",
        target_pane_id="%73",
        working_dir="/Volumes/Imperium/Imperium-ENV",
        disposition=ResumeDisposition.RESUME,
        reason="legacy",
    )

    target = RestartExecutor()._resume_target_ref(resume)

    assert target == ""


def test_dry_run_emits_deterministic_action_order():
    workspace = _workspace(_pane("%1", "somnium:NW", stamp_instance_id="abc12345"))
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(_instance("abc12345", "somnium:NW"),),
    )
    grouped = (
        GroupedSessionSnapshot("main", "main", 0, "somnium"),
        GroupedSessionSnapshot("phone", "main", 2, "somnium"),
    )
    attachments = build_client_attachments(
        [
            {
                "client_tty": "/dev/ttys001",
                "session_name": "main",
                "client_name": "local",
                "window_index": "0",
                "window_name": "somnium",
            },
            {
                "client_tty": "/dev/pts/4",
                "session_name": "phone",
                "client_name": "remote",
                "window_index": "2",
                "window_name": "somnium",
            },
        ],
        managed_sessions=grouped,
    )
    plan = build_restart_plan(
        workspace,
        registry,
        client_attachments=attachments,
        grouped_sessions=grouped,
    )

    result = RestartExecutor().dry_run(plan)
    descriptions = [action.description for action in result.actions]

    assert descriptions == [
        "freeze workspace, grouped sessions, clients, and registry inputs",
        "park client /dev/ttys001 (local_leader)",
        "detach client /dev/pts/4 (remote_grouped)",
        "kill grouped session phone",
        "kill leader session main",
        "recreate workspace via builder.build_workspace",
        "normalize managed windows before restore",
        "clear transient stash windows",
        "resume abc12345 into somnium:W with resume",
        "recreate grouped session phone on somnium",
        "verify pane labels and resume outcomes",
    ]


class FakeBuilderAdapter:
    def __init__(self) -> None:
        self.sessions: set[str] = set()
        self.windows: dict[str, list[str]] = {}
        self.panes: dict[str, list[str]] = {}
        self.pane_options: dict[str, dict[str, str]] = {}
        self.window_options: dict[str, dict[str, str]] = {}
        self.commands: list[tuple[str, ...]] = []

    def has_session(self, session_name: str) -> bool:
        return session_name in self.sessions

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.commands.append(args)
        cmd = args[0]
        if cmd == "new-session":
            session = args[args.index("-s") + 1]
            window = args[args.index("-n") + 1]
            self.sessions.add(session)
            self.windows[session] = [window]
            self.panes[f"{session}:{window}"] = [f"{session}:{window}.1"]
            return ""
        if cmd == "new-window":
            session = args[args.index("-t") + 1]
            window = args[args.index("-n") + 1]
            self.windows.setdefault(session, []).append(window)
            self.panes[f"{session}:{window}"] = [f"{session}:{window}.1"]
            return ""
        if cmd == "display-message":
            target = args[args.index("-t") + 1]
            fmt = args[-1]
            if fmt == "#{window_width}":
                return "240\n"
            if fmt == "#{window_height}":
                return "60\n"
            if fmt == "#{pane_id}":
                return f"{target}\n"
            return "\n"
        if cmd == "split-window":
            target = args[args.index("-t") + 1]
            window_target = target.rsplit(".", 1)[0]
            pane_list = self.panes.setdefault(window_target, [f"{window_target}.1"])
            new_pane = f"{window_target}.{len(pane_list) + 1}"
            pane_list.append(new_pane)
            if "-P" in args:
                return f"{new_pane}\n"
            return ""
        if cmd == "set-option":
            option = args[-2]
            value = args[-1]
            target = args[args.index("-t") + 1]
            if "-p" in args:
                self.pane_options.setdefault(target, {})[option] = value
            elif "-w" in args:
                self.window_options.setdefault(target, {})[option] = value
            return ""
        if cmd in {"send-keys", "select-pane", "select-window"}:
            return ""
        raise AssertionError(f"unhandled tmux command in fake adapter: {args}")


def test_builder_creates_canonical_workspace_roles():
    adapter = FakeBuilderAdapter()

    build_workspace(adapter, "main")  # type: ignore[arg-type]

    assert adapter.windows["main"] == [
        "palace",
        "somnium",
        "council",
        "mechanicus",
        "reservists",
    ]
    roles = {
        target: options.get("@PANE_ID")
        for target, options in adapter.pane_options.items()
        if "@PANE_ID" in options
    }
    assert {
        "palace:W",
        "palace:N",
        "palace:S",
        "palace:E",
    } <= set(roles.values())
    assert {
        "somnium:W",
        "somnium:N",
        "somnium:NE",
        "somnium:S",
        "somnium:SE",
    } <= set(roles.values())
    # The council page reuses the somnium 5-pane geometry but seats fixed personas:
    # W custodes, N pax, NE malcador, SE administratum, S true-terminal (plain shell).
    assert roles["main:council.1"] == "council:custodes"
    assert roles["main:council.2"] == "council:pax"
    assert roles["main:council.3"] == "council:malcador"
    assert roles["main:council.4"] == "council:true-terminal"
    assert roles["main:council.5"] == "council:administratum"
    # The retired per-fleet stack pages are gone (enforced by the exact window-list
    # equality above).
    # Mechanicus seats the Fabricator-General (.1) over the orchestrator (.2); the
    # admin seat moved to council, the orchestrator docked in as the secondary persona.
    assert roles["main:mechanicus.1"] == "mechanicus:fabricator-general"
    assert roles["main:mechanicus.2"] == "mechanicus:orchestrator"
    assert roles["main:reservists.1"] == "reservists:civic"
    for pane in (
        "main:council.1",
        "main:council.2",
        "main:council.3",
        "main:council.5",
    ):
        assert adapter.pane_options[pane]["@PANE_TYPE"] == "council"
    # true-terminal is a plain shell seat — no persona, so no @PANE_TYPE.
    assert "@PANE_TYPE" not in adapter.pane_options["main:council.4"]
    assert adapter.pane_options["main:mechanicus.1"]["@PANE_TYPE"] == "mechanicus"
    assert adapter.pane_options["main:mechanicus.2"]["@PANE_TYPE"] == "mechanicus"
    assert adapter.pane_options["main:reservists.1"]["@PANE_TYPE"] == "reservists"
    # The civic reservist pane carries the hook the civic-thread fallthrough resolves.
    assert adapter.pane_options["main:reservists.1"]["@CIVIC_RESERVIST"] == "1"
    pane_types = [options.get("@PANE_TYPE") for options in adapter.pane_options.values()]
    assert "tui" not in pane_types
    for target in (
        "main:palace",
        "main:somnium",
        "main:council",
        "main:mechanicus",
        "main:reservists",
    ):
        assert adapter.window_options[target]["window-size"] == "latest"


def test_window_dir_persona_windows_use_vault_when_mounted(tmp_path, monkeypatch) -> None:
    vault = tmp_path / "Imperium-ENV"
    vault.mkdir()
    monkeypatch.setenv("IMPERIUM", str(tmp_path))

    # Persona windows launch from the vault; non-persona windows stay in $HOME.
    for window in PERSONA_WINDOWS:
        assert _window_dir(window) == str(vault)
    assert _window_dir("palace") == str(pathlib.Path.home())
    assert _window_dir("reservists") == str(pathlib.Path.home())


def test_window_dir_falls_back_to_home_when_vault_unmounted(tmp_path, monkeypatch) -> None:
    # IMPERIUM points at a root with no Imperium-ENV dir → not mounted.
    monkeypatch.setenv("IMPERIUM", str(tmp_path / "nonexistent"))

    for window in PERSONA_WINDOWS:
        assert _window_dir(window) == str(pathlib.Path.home())


def test_build_council_window_seats_five_personas_in_cardinal_positions() -> None:
    # The council page reuses the somnium 5-pane geometry (W rail + right 2x2) but
    # seats fixed personas by cardinal position: W custodes, N pax, NE malcador,
    # SE administratum, S true-terminal (a plain shell — no persona, no agent).
    adapter = FakeBuilderAdapter()
    adapter.sessions.add("main")
    adapter.windows["main"] = []
    adapter.panes["main:council"] = ["main:council.1"]

    build_council_window(adapter, "main")  # type: ignore[arg-type]

    seats = {
        target: options["@PANE_ID"]
        for target, options in adapter.pane_options.items()
        if options.get("@PANE_ID", "").startswith("council:")
    }
    assert seats == {
        "main:council.1": "council:custodes",
        "main:council.2": "council:pax",
        "main:council.3": "council:malcador",
        "main:council.4": "council:true-terminal",
        "main:council.5": "council:administratum",
    }
    for pane in (
        "main:council.1",
        "main:council.2",
        "main:council.3",
        "main:council.5",
    ):
        assert adapter.pane_options[pane]["@PANE_TYPE"] == "council"
    # The plain shell seat carries no persona type.
    assert "@PANE_TYPE" not in adapter.pane_options["main:council.4"]
    # The west rail is a side pane; the right 2x2 are small grid panes.
    assert adapter.pane_options["main:council.1"]["@GRID_STATE"] == "side"
    for pane in ("main:council.2", "main:council.3", "main:council.5"):
        assert adapter.pane_options[pane]["@GRID_STATE"] == "small"


def test_build_mechanicus_window_seats_fabricator_over_orchestrator() -> None:
    # Mechanicus seats the Fabricator-General (.1) over the orchestrator (.2), both
    # tagged @PANE_TYPE mechanicus. The admin seat moved to council; the orchestrator
    # docked in as the secondary persona under the Fabricator-General.
    adapter = FakeBuilderAdapter()
    adapter.sessions.add("main")
    adapter.windows["main"] = []
    adapter.panes["main:mechanicus"] = ["main:mechanicus.1"]

    build_mechanicus_window(adapter, "main")  # type: ignore[arg-type]

    seats = {
        target: options["@PANE_ID"]
        for target, options in adapter.pane_options.items()
        if options.get("@PANE_ID", "").startswith("mechanicus:")
    }
    assert seats == {
        "main:mechanicus.1": "mechanicus:fabricator-general",
        "main:mechanicus.2": "mechanicus:orchestrator",
    }
    for pane in ("main:mechanicus.1", "main:mechanicus.2"):
        assert adapter.pane_options[pane]["@PANE_TYPE"] == "mechanicus"


def test_normalize_instance_status_accepts_live_api_vocabulary():
    # /api/instances serves token-api's VALID_STATUSES vocabulary, which renames
    # "processing" to "working" and adds mid-conversation states. An unmapped
    # status normalizes to UNKNOWN, fails _is_resumable, and silently drops the
    # instance from every restart plan — an actively working instance must not
    # vanish from the plan just because it was busy at capture time.
    from tmuxctl.registry import normalize_instance_status

    for active in ("working", "questioning", "preplanning", "planning", "compacting", "reviewing"):
        assert normalize_instance_status(active) is InstanceStatus.PROCESSING, active
    assert normalize_instance_status("processing") is InstanceStatus.PROCESSING
    assert normalize_instance_status("idle") is InstanceStatus.IDLE
    assert normalize_instance_status("victorious") is InstanceStatus.IDLE
    assert normalize_instance_status("stopped") is InstanceStatus.STOPPED
    assert normalize_instance_status("archived") is InstanceStatus.UNKNOWN
    assert normalize_instance_status(None) is InstanceStatus.UNKNOWN


def test_restart_plan_resumes_instance_reported_working_by_api():
    # End-to-end through build_registry_snapshot: a registry row with the raw
    # API status "working" and a live pane stamp must land in plan.resumes.
    from tmuxctl.registry import build_registry_snapshot

    workspace = _workspace(
        _pane("%53", "palace:W", window="palace", stamp_instance_id="busy"),
        _pane("%55", "palace:E", window="palace"),
        window="palace",
        archetype=WindowArchetype.PALACE,
    )
    registry = build_registry_snapshot(
        device_id="Mac-Mini",
        instances=[
            {
                "id": "busy",
                "device_id": "Mac-Mini",
                "status": "working",
                "last_activity": _iso_ago(seconds=5),
            }
        ],
    )

    plan = build_restart_plan(workspace, registry)

    assert [(r.instance_id, r.pane_label, r.target_pane_id) for r in plan.resumes] == [
        ("busy", "palace:W", "")
    ]
