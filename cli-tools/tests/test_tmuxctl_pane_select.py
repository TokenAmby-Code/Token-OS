from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.pane_select import select_pane


class FakePaneSelectAdapter:
    def __init__(
        self,
        *,
        session: str = "main",
        window_index: str = "1",
        window_name: str = "palace",
        panes: list[dict[str, object]] | None = None,
        current: str = "",
        window_options: dict[str, str] | None = None,
        zoomed: bool = False,
        select_z_keeps_zoom: bool = True,
    ) -> None:
        self.session = session
        self.window_index = window_index
        self.window_name = window_name
        self.panes = panes or []
        self.current = current or str(self.panes[0]["pane_id"])
        self.window_options = window_options or {}
        self.zoomed = zoomed
        self.select_z_keeps_zoom = select_z_keeps_zoom
        self.global_options: dict[str, str] = {}
        self.commands: list[tuple[str, ...]] = []

    def _pane(self, target: str | None = None) -> dict[str, object]:
        target = target or self.current
        for pane in self.panes:
            if pane["pane_id"] == target or pane.get("role") == target:
                return pane
        raise AssertionError(f"unknown pane target: {target}")

    def show_pane_option(self, pane_id: str, option: str) -> str:
        pane = self._pane(pane_id)
        if option == "@PANE_ID":
            return str(pane.get("role", ""))
        if option == "@PANE_TYPE":
            return str(pane.get("type", ""))
        return ""

    def show_window_option(self, target: str, option: str) -> str:
        return self.window_options.get(option, "")

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.commands.append(args)
        if args[0] == "set-option" and "-g" in args:
            self.global_options[args[-2]] = args[-1]
            return ""
        if args[0] == "display-message":
            target = args[args.index("-t") + 1] if "-t" in args else self.current
            fmt = args[-1]
            if fmt == "#{window_zoomed_flag}":
                return "1\n" if self.zoomed else "0\n"
            if target == f"{self.session}:{self.window_index}" or target == self.window_name:
                target = self.current
            pane = self._pane(target)
            if fmt == "#{pane_id}\t#{session_name}\t#{window_index}\t#{window_name}":
                return (
                    f"{pane['pane_id']}\t{self.session}\t{self.window_index}\t{self.window_name}\n"
                )
            if fmt == (
                "#{pane_id}\t#{session_name}\t#{window_index}\t#{window_name}"
                "\t#{window_zoomed_flag}"
            ):
                zoomed = "1" if self.zoomed else "0"
                return (
                    f"{pane['pane_id']}\t{self.session}\t{self.window_index}"
                    f"\t{self.window_name}\t{zoomed}\n"
                )
            if fmt == "#{pane_id}":
                return f"{pane['pane_id']}\n"
            if fmt == "#{session_name}":
                return f"{self.session}\n"
            if fmt == "#{window_index}":
                return f"{self.window_index}\n"
            if fmt == "#{window_name}":
                return f"{self.window_name}\n"
        if args[0] == "list-panes":
            lines = []
            for pane in self.panes:
                lines.append(
                    "\t".join(
                        [
                            str(pane["pane_id"]),
                            str(pane.get("role", "")),
                            str(pane.get("type", "")),
                            str(pane.get("left", 0)),
                            str(pane.get("top", 0)),
                        ]
                    )
                )
            return "\n".join(lines)
        if args[0] == "select-pane":
            before = self.current
            if "-t" in args and args[-1] not in {"-L", "-R", "-U", "-D"}:
                target = args[args.index("-t") + 1]
                self.current = str(self._pane(target)["pane_id"])
            elif args[-1] in {"-L", "-R", "-U", "-D"}:
                target = args[args.index("-t") + 1] if "-t" in args else self.current
                self.current = str(self._pane(target)["pane_id"])
                self._relative(args[-1])
            if self.zoomed and self.current != before:
                if "-Z" not in args or not self.select_z_keeps_zoom:
                    self.zoomed = False
            return ""
        if args[0] == "resize-pane" and "-Z" in args:
            self.zoomed = not self.zoomed
            return ""
        return ""

    def _relative(self, flag: str) -> None:
        current = self._pane()
        role = current.get("role", "")
        if self.window_name == "somnium" and flag == "-D" and role == "somnium:NE":
            self.current = str(self._pane("somnium:SE")["pane_id"])
            return
        if self.window_name == "somnium" and flag == "-U" and role == "somnium:SE":
            self.current = str(self._pane("somnium:NE")["pane_id"])
            return


def palace_adapter() -> FakePaneSelectAdapter:
    return FakePaneSelectAdapter(
        window_name="palace",
        panes=[
            {"pane_id": "%W", "role": "palace:W"},
            {"pane_id": "%N", "role": "palace:N"},
            {"pane_id": "%S", "role": "palace:S"},
            {"pane_id": "%E", "role": "palace:E"},
        ],
        current="%N",
    )


def test_palace_absolute_arrows_select_cardinal_targets():
    expected = {
        "left": "palace:W",
        "right": "palace:E",
        "up": "palace:N",
        "down": "palace:S",
    }
    for direction, target in expected.items():
        adapter = palace_adapter()

        result = select_pane(adapter, mode="absolute", direction=direction, client="/dev/ttys001")

        assert result.endswith(target)
        assert ("select-pane", "-t", target) in adapter.commands
        assert adapter.global_options["@IMPERIUM_HUMAN_MECHANICUS_FOCUS_CLIENT"] == "/dev/ttys001"


def test_somnium_absolute_right_then_relative_down_reaches_se():
    adapter = FakePaneSelectAdapter(
        window_index="2",
        window_name="somnium",
        panes=[
            {"pane_id": "%W", "role": "somnium:W"},
            {"pane_id": "%N", "role": "somnium:N"},
            {"pane_id": "%NE", "role": "somnium:NE"},
            {"pane_id": "%S", "role": "somnium:S"},
            {"pane_id": "%SE", "role": "somnium:SE"},
        ],
        current="%W",
    )

    select_pane(adapter, mode="absolute", direction="right", client="/dev/ttys001")
    assert adapter._pane()["role"] == "somnium:NE"

    select_pane(adapter, mode="relative", direction="down", client="/dev/ttys001")

    assert adapter._pane()["role"] == "somnium:SE"
    assert ("select-pane", "-t", "%NE", "-D") in adapter.commands


def mechanicus_adapter(
    *,
    current: str = "%F",
    focused: str = "%2",
    workers: bool = True,
) -> FakePaneSelectAdapter:
    opts = {"@STACK_FOCUSED_PANE": focused} if focused else {}
    panes: list[dict[str, object]] = [
        {
            "pane_id": "%F",
            "role": "mechanicus:fabricator-general",
            "type": "mechanicus",
            "left": 0,
            "top": 0,
        },
        {
            "pane_id": "%A",
            "role": "mechanicus:admin",
            "type": "mechanicus",
            "left": 0,
            "top": 25,
        },
    ]
    if workers:
        panes.extend(
            [
                {
                    "pane_id": "%1",
                    "role": "mechanicus:1",
                    "type": "stack-worker",
                    "left": 81,
                    "top": 8,
                },
                {
                    "pane_id": "%2",
                    "role": "mechanicus:2",
                    "type": "stack-worker",
                    "left": 81,
                    "top": 20,
                },
            ]
        )
    return FakePaneSelectAdapter(
        window_index="4",
        window_name="mechanicus",
        panes=panes,
        current=current,
        window_options=opts,
    )


def legion_adapter(*, current: str = "%C", workers: bool = True) -> FakePaneSelectAdapter:
    panes: list[dict[str, object]] = [
        {
            "pane_id": "%C",
            "role": "legion:custodes",
            "type": "legion",
            "left": 0,
            "top": 0,
        },
        {
            "pane_id": "%M",
            "role": "legion:malcador",
            "type": "legion",
            "left": 0,
            "top": 25,
        },
    ]
    if workers:
        panes.extend(
            [
                {
                    "pane_id": "%1",
                    "role": "legion:1",
                    "type": "stack-worker",
                    "left": 81,
                    "top": 8,
                },
                {
                    "pane_id": "%2",
                    "role": "legion:2",
                    "type": "stack-worker",
                    "left": 81,
                    "top": 20,
                },
            ]
        )
    return FakePaneSelectAdapter(
        window_index="3",
        window_name="legion",
        panes=panes,
        current=current,
    )


def koronus_adapter(*, current: str = "%P", workers: bool = True) -> FakePaneSelectAdapter:
    panes: list[dict[str, object]] = [
        {
            "pane_id": "%P",
            "role": "koronus:pax",
            "type": "koronus",
            "left": 0,
            "top": 0,
        },
        {
            "pane_id": "%O",
            "role": "koronus:orchestrator",
            "type": "koronus",
            "left": 0,
            "top": 25,
        },
    ]
    if workers:
        panes.extend(
            [
                {
                    "pane_id": "%1",
                    "role": "koronus:1",
                    "type": "stack-worker",
                    "left": 81,
                    "top": 8,
                },
                {
                    "pane_id": "%2",
                    "role": "koronus:2",
                    "type": "stack-worker",
                    "left": 81,
                    "top": 20,
                },
            ]
        )
    return FakePaneSelectAdapter(
        window_index="7",
        window_name="koronus",
        panes=panes,
        current=current,
    )


def test_legion_absolute_arrows_select_persona_and_worker_extremes():
    expected = {
        "left": "legion:custodes",
        "right": "legion:malcador",
        "up": "legion:1",
        "down": "legion:2",
    }
    for direction, target in expected.items():
        adapter = legion_adapter(current="%2")

        result = select_pane(adapter, mode="absolute", direction=direction, client="/dev/ttys001")

        assert result.endswith(target)
        assert adapter._pane()["role"] == target
        assert ("select-pane", "-t", target) in adapter.commands


def test_mechanicus_absolute_arrows_select_persona_and_worker_extremes():
    expected = {
        "left": "mechanicus:fabricator-general",
        "right": "mechanicus:admin",
        "up": "mechanicus:1",
        "down": "mechanicus:2",
    }
    for direction, target in expected.items():
        adapter = mechanicus_adapter(current="%2")

        result = select_pane(adapter, mode="absolute", direction=direction, client="/dev/ttys001")

        assert result.endswith(target)
        assert adapter._pane()["role"] == target
        assert ("select-pane", "-t", target) in adapter.commands


def test_koronus_absolute_arrows_select_persona_and_worker_extremes():
    expected = {
        "left": "koronus:pax",
        "right": "koronus:orchestrator",
        "up": "koronus:1",
        "down": "koronus:2",
    }
    for direction, target in expected.items():
        adapter = koronus_adapter(current="%2")

        result = select_pane(adapter, mode="absolute", direction=direction, client="/dev/ttys001")

        assert result.endswith(target)
        assert adapter._pane()["role"] == target
        assert ("select-pane", "-t", target) in adapter.commands


def test_stack_absolute_up_down_noop_when_no_workers():
    for direction in ("up", "down"):
        adapter = mechanicus_adapter(current="%F", workers=False, focused="")

        result = select_pane(adapter, mode="absolute", direction=direction, client="/dev/ttys001")

        assert result.endswith("noop")
        assert adapter._pane()["role"] == "mechanicus:fabricator-general"
        assert not any(command[0] == "select-pane" for command in adapter.commands)


def test_stack_absolute_selection_does_not_mutate_focused_worker_option():
    adapter = mechanicus_adapter(current="%F", focused="%1")

    select_pane(adapter, mode="absolute", direction="down", client="/dev/ttys001")

    assert adapter.window_options["@STACK_FOCUSED_PANE"] == "%1"
    assert not any(command[:2] == ("set-option", "-w") for command in adapter.commands)


def test_mechanicus_absolute_left_selects_fabricator_general():
    adapter = mechanicus_adapter(current="%2")

    select_pane(adapter, mode="absolute", direction="left", client="/dev/ttys001")

    assert adapter._pane()["role"] == "mechanicus:fabricator-general"
    assert ("select-pane", "-t", "mechanicus:fabricator-general") in adapter.commands


def test_mechanicus_absolute_right_selects_admin():
    adapter = mechanicus_adapter(current="%F", focused="%2")

    select_pane(adapter, mode="absolute", direction="right", client="/dev/ttys001")

    assert adapter._pane()["role"] == "mechanicus:admin"
    assert ("select-pane", "-t", "mechanicus:admin") in adapter.commands


def test_mechanicus_absolute_up_down_select_worker_top_bottom():
    for direction, target in {"up": "mechanicus:1", "down": "mechanicus:2"}.items():
        adapter = mechanicus_adapter(current="%F", focused="%77")

        select_pane(adapter, mode="absolute", direction=direction, client="/dev/ttys001")

        assert adapter._pane()["role"] == target
        assert ("select-pane", "-t", target) in adapter.commands


def test_relative_right_from_fabricator_general_selects_focused_worker():
    adapter = mechanicus_adapter(current="%F", focused="%2")

    select_pane(adapter, mode="relative", direction="right", client="/dev/ttys001")

    assert adapter._pane()["role"] == "mechanicus:2"
    assert ("select-pane", "-t", "mechanicus:2") in adapter.commands


def test_absolute_selection_reexpands_when_native_zoom_drops_on_pane_swap():
    adapter = FakePaneSelectAdapter(
        window_name="palace",
        panes=[
            {"pane_id": "%N", "role": "palace:N"},
            {"pane_id": "%E", "role": "palace:E"},
        ],
        current="%N",
        zoomed=True,
    )

    select_pane(adapter, mode="absolute", direction="right", client="/dev/ttys001")

    assert adapter._pane()["role"] == "palace:E"
    assert adapter.zoomed is True
    assert ("select-pane", "-Z", "-t", "palace:E") in adapter.commands
    assert not any(command[0] == "resize-pane" for command in adapter.commands)


def test_relative_selection_reexpands_when_native_zoom_drops_on_pane_swap():
    adapter = FakePaneSelectAdapter(
        window_index="2",
        window_name="somnium",
        panes=[
            {"pane_id": "%NE", "role": "somnium:NE"},
            {"pane_id": "%SE", "role": "somnium:SE"},
        ],
        current="%NE",
        zoomed=True,
    )

    select_pane(adapter, mode="relative", direction="down", client="/dev/ttys001")

    assert adapter._pane()["role"] == "somnium:SE"
    assert adapter.zoomed is True
    assert ("select-pane", "-Z", "-t", "%NE", "-D") in adapter.commands
    assert not any(command[0] == "resize-pane" for command in adapter.commands)


def test_zoomed_selection_reexpands_if_select_pane_z_does_not_preserve_zoom():
    adapter = FakePaneSelectAdapter(
        window_name="palace",
        panes=[
            {"pane_id": "%N", "role": "palace:N"},
            {"pane_id": "%E", "role": "palace:E"},
        ],
        current="%N",
        zoomed=True,
        select_z_keeps_zoom=False,
    )

    select_pane(adapter, mode="absolute", direction="right", client="/dev/ttys001")

    assert adapter._pane()["role"] == "palace:E"
    assert adapter.zoomed is True
    assert ("resize-pane", "-Z", "-t", "%E") in adapter.commands
