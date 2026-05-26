from __future__ import annotations

import argparse
import sys

from .api import RegistryError
from .service import TmuxControlPlane
from .tmux_adapter import TmuxError


def _parse_window_ref(value: str, control: TmuxControlPlane) -> tuple[str, int]:
    if value == "current":
        session = control.adapter.current_session_name()
        raw = control.adapter.run("display-message", "-p", "#{window_index}").strip()
        return session, int(raw)
    if ":" not in value:
        raise argparse.ArgumentTypeError("window must look like session:index or use 'current'")
    session_name, raw_index = value.split(":", 1)
    try:
        return session_name, int(raw_index)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("window index must be an integer") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tmuxctl", add_help=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_subparsers = inspect_parser.add_subparsers(dest="inspect_command", required=True)

    inspect_workspace = inspect_subparsers.add_parser("workspace")
    inspect_workspace.add_argument("--session", default="main")

    inspect_restart = inspect_subparsers.add_parser("restart-plan")
    inspect_restart.add_argument("--session", default="main")

    inspect_window = inspect_subparsers.add_parser("window")
    inspect_window.add_argument("--window", default="current")

    inspect_pane = inspect_subparsers.add_parser("pane")
    inspect_pane.add_argument("--pane", required=True)

    normalize_parser = subparsers.add_parser("normalize")
    normalize_parser.add_argument("--window", default="current")

    focus_parser = subparsers.add_parser("focus")
    focus_parser.add_argument(
        "mode",
        nargs="?",
        default="toggle",
        choices=["toggle", "focus-grid", "unfocus-grid", "focus-side", "unfocus-side"],
    )
    focus_parser.add_argument("--window", default="current")

    restart_parser = subparsers.add_parser("restart")
    restart_parser.add_argument("--session", default="main")
    mode = restart_parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")

    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--session", default="main")

    resolve_parser = subparsers.add_parser("resolve-pane")
    resolve_parser.add_argument("--format", choices=["id", "physical", "full", "json"], default="full")
    resolve_parser.add_argument("target")

    session_doc_parser = subparsers.add_parser(
        "session-doc",
        help="Resolve a cardinal pane id to its linked session document.",
    )
    session_doc_parser.add_argument("--pane", default="current")
    session_doc_parser.add_argument(
        "--format",
        choices=["json", "id", "path", "title", "cardinal"],
        default="json",
    )

    send_text_parser = subparsers.add_parser("send-text")
    send_text_parser.add_argument("--pane", required=True)
    text_source = send_text_parser.add_mutually_exclusive_group(required=True)
    text_source.add_argument("--text")
    text_source.add_argument("--stdin", action="store_true")
    send_text_parser.add_argument("--clear-prompt", action="store_true")

    invoke_skill_parser = subparsers.add_parser(
        "invoke-skill",
        help="Insert a harness-correct explicit skill invocation at the prompt start.",
    )
    invoke_skill_parser.add_argument("skill")
    invoke_skill_parser.add_argument("--pane", default="current")
    invoke_skill_parser.add_argument("--agent", default="auto")
    invoke_skill_parser.add_argument("--dry-run", action="store_true")

    audience_parser = subparsers.add_parser("audience")
    audience_subparsers = audience_parser.add_subparsers(dest="audience_command", required=True)

    audience_toggle = audience_subparsers.add_parser("toggle")
    audience_toggle.add_argument("--pane", default="current")
    audience_toggle.add_argument("--client", default="")

    audience_return = audience_subparsers.add_parser("return")
    audience_return.add_argument("--pane", default="current")
    audience_return.add_argument("--client", default="")

    tombstone_parser = subparsers.add_parser("tombstone")
    tombstone_subparsers = tombstone_parser.add_subparsers(dest="tombstone_command", required=True)

    tombstone_jump = tombstone_subparsers.add_parser("jump")
    tombstone_jump.add_argument("--pane", default="current")
    tombstone_jump.add_argument("--client", default="")

    tombstone_install = tombstone_subparsers.add_parser("install")
    tombstone_install.add_argument("--slot-pane", required=True)
    tombstone_install.add_argument("--source-role", required=True)
    tombstone_install.add_argument("--target-pane", required=True)

    stack_parser = subparsers.add_parser("stack")
    stack_subparsers = stack_parser.add_subparsers(dest="stack_command", required=True)

    stack_add = stack_subparsers.add_parser("add")
    stack_add.add_argument("base", help="stack window base: legion, mechanicus, mars, kreig")
    stack_add.add_argument("--cwd", default=None)
    stack_add.add_argument("--session", default="main")
    stack_dispatch = stack_subparsers.add_parser("dispatch")
    stack_dispatch.add_argument("base", help="stack window base: legion, mechanicus, mars, kreig")
    stack_dispatch.add_argument("--cwd", default=None)
    stack_dispatch.add_argument("--session", default="main")
    stack_dispatch.add_argument("--command", dest="launch_command", required=True)
    stack_dispatch.add_argument("--no-focus", action="store_true")
    stack_dispatch.add_argument("--settle", type=float, default=0.5)
    stack_enforce = stack_subparsers.add_parser("enforce")
    stack_enforce.add_argument("--pane", default="current")
    stack_enforce.add_argument("--window", default="")
    stack_enforce.add_argument("--focus", action="store_true")
    stack_enforce.add_argument("--admit", action="store_true")
    stack_enforce.add_argument("--kill-pending-clear", action="store_true")
    stack_sweep = stack_subparsers.add_parser("sweep")
    stack_sweep.add_argument("--session", default="main")
    stack_sweep.add_argument("--keep-pending-clear", action="store_true")

    legion_parser = subparsers.add_parser("legion")
    legion_subparsers = legion_parser.add_subparsers(dest="legion_command", required=True)

    legion_focus = legion_subparsers.add_parser("focus-selected")
    legion_focus.add_argument("--pane", default="current")

    legion_enforce = legion_subparsers.add_parser("enforce")
    legion_enforce.add_argument("--pane", default="current")

    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--session", default="main")
    create_parser.add_argument("--attach", action="store_true")

    rebuild_parser = subparsers.add_parser("rebuild-window")
    rebuild_parser.add_argument("--window", default="current")

    assert_instance_parser = subparsers.add_parser("assert-instance")
    assert_instance_parser.add_argument("--pane", required=True)

    guard_parser = subparsers.add_parser("mechanicus-focus-guard")
    guard_parser.add_argument("--pane", default="")
    guard_parser.add_argument("--client", default="")
    guard_parser.add_argument("--surface", default="after-select")

    allow_mech_parser = subparsers.add_parser("allow-mechanicus-focus")
    allow_mech_parser.add_argument("--seconds", type=float, default=4.0)
    allow_mech_parser.add_argument("--reason", default="explicit")
    allow_human_parser = subparsers.add_parser("allow-human-mechanicus-focus")
    allow_human_parser.add_argument("--client", default="")
    allow_human_parser.add_argument("--reason", default="explicit-human-navigation")

    pane_select_parser = subparsers.add_parser(
        "pane-select",
        help="Explicit human pane selection with cardinal routing.",
    )
    pane_select_parser.add_argument("--mode", choices=["absolute", "relative"], required=True)
    pane_select_parser.add_argument(
        "--direction", choices=["up", "down", "left", "right"], required=True
    )
    pane_select_parser.add_argument("--client", default="")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    control = TmuxControlPlane()

    try:
        if args.command == "inspect":
            if args.inspect_command == "workspace":
                print(control.inspect_workspace(args.session))
                return 0
            if args.inspect_command == "restart-plan":
                print(control.inspect_restart_plan(args.session))
                return 0
            if args.inspect_command == "window":
                session_name, window_index = _parse_window_ref(args.window, control)
                print(control.inspect_window(session_name, window_index))
                return 0
            if args.inspect_command == "pane":
                print(control.inspect_pane(args.pane))
                return 0

        if args.command == "normalize":
            session_name, window_index = _parse_window_ref(args.window, control)
            print(control.normalize(session_name=session_name, window_index=window_index))
            return 0

        if args.command == "focus":
            session_name, window_index = _parse_window_ref(args.window, control)
            print(
                control.focus(session_name=session_name, window_index=window_index, mode=args.mode)
            )
            return 0

        if args.command == "restart":
            if args.dry_run:
                print(control.dry_run_restart(args.session))
                return 0
            if args.execute:
                output, ok = control.execute_restart(args.session)
                print(output)
                return 0 if ok else 1

        if args.command == "doctor":
            print(control.doctor(args.session))
            return 0

        if args.command == "resolve-pane":
            resolved = control.resolve_pane(args.target)
            if args.format == "id":
                print(control.public_pane_id(args.target))
            elif args.format == "physical":
                print(control.physical_pane_id(args.target))
            elif args.format == "json":
                import json

                values = {}
                for line in resolved.splitlines():
                    key, value = line.split(": ", 1)
                    values[key] = value
                print(json.dumps(values))
            else:
                print(resolved)
            return 0

        if args.command == "session-doc":
            import json

            doc = control.session_doc_for_pane(args.pane)
            if args.format == "json":
                print(json.dumps(doc))
            elif args.format == "id":
                print(doc["id"])
            elif args.format == "path":
                print(doc.get("file_path") or "")
            elif args.format == "title":
                print(doc.get("title") or "")
            elif args.format == "cardinal":
                print(doc.get("pane_label") or control.cardinal_pane_label(args.pane))
            return 0

        if args.command == "send-text":
            import json as _json
            from .assertions import assert_instance

            assertion = assert_instance(control.adapter, args.pane)
            if not assertion.get("ok"):
                if assertion.get("action") == "persona_correction_sent":
                    raise ValueError(
                        "persona correction sent; retry after settle before sending payload: "
                        f"{_json.dumps(assertion)}"
                    )
                raise ValueError(f"pane has no live instance: {_json.dumps(assertion)}")
            text = sys.stdin.read() if args.stdin else args.text
            control.adapter.send_text_then_submit(
                args.pane,
                text,
                clear_prompt=args.clear_prompt,
            )
            return 0

        if args.command == "invoke-skill":
            from .skill_invoke import normalize_agent, skill_invocation_text

            pane = args.pane
            if pane == "current" and not args.dry_run:
                pane = control.adapter.run("display-message", "-p", "#{pane_id}").strip()
            if args.dry_run:
                agent = normalize_agent(args.agent)
                if agent == "auto":
                    agent = "claude"
                print(skill_invocation_text(args.skill, agent))
                return 0
            print(control.invoke_skill(pane, args.skill, agent=args.agent), end="")
            return 0

        if args.command == "audience":
            import os as _os

            _os.environ.setdefault("IMPERIUM_ALLOW_TMUX_FOCUS", "1")
            _os.environ.setdefault("IMPERIUM_ALLOW_MECHANICUS_FOCUS", "1")
            pane = args.pane
            if pane == "current":
                pane = control.adapter.run("display-message", "-p", "#{pane_id}").strip()
            if args.audience_command == "toggle":
                print(control.audience_toggle(pane, client=args.client))
                return 0
            if args.audience_command == "return":
                print(control.audience_return(pane, client=args.client))
                return 0

        if args.command == "tombstone":
            import os as _os

            _os.environ.setdefault("IMPERIUM_ALLOW_TMUX_FOCUS", "1")
            _os.environ.setdefault("IMPERIUM_ALLOW_MECHANICUS_FOCUS", "1")
            if args.tombstone_command == "jump":
                pane = args.pane
                if pane == "current":
                    pane = control.adapter.run("display-message", "-p", "#{pane_id}").strip()
                print(control.tombstone_jump(pane, client=args.client))
                return 0
            if args.tombstone_command == "install":
                print(
                    control.tombstone_install(
                        args.slot_pane,
                        args.source_role,
                        args.target_pane,
                    )
                )
                return 0

        if args.command == "stack":
            if args.stack_command == "add":
                from .stack import add_stack_pane

                pane_id = add_stack_pane(control.adapter, args.session, args.base, cwd=args.cwd)
                print(pane_id)
                return 0
            if args.stack_command == "dispatch":
                from .stack import dispatch_stack_command

                pane_id = dispatch_stack_command(
                    control.adapter,
                    args.session,
                    args.base,
                    args.launch_command,
                    cwd=args.cwd,
                    focus=not args.no_focus,
                    settle_seconds=args.settle,
                )
                print(pane_id)
                return 0
            if args.stack_command == "sweep":
                from .stack import sweep_stack_assertions

                print(
                    sweep_stack_assertions(
                        control.adapter,
                        args.session,
                        kill_pending_clear=not args.keep_pending_clear,
                    )
                )
                return 0
            if args.stack_command == "enforce":
                from .stack import enforce_stack_layout

                if args.window:
                    target = args.window
                    pane = ""
                else:
                    pane = args.pane
                    if pane == "current":
                        pane = control.adapter.run("display-message", "-p", "#{pane_id}").strip()
                    target = control.adapter.run(
                        "display-message", "-t", pane, "-p", "#{session_name}:#{window_index}"
                    ).strip()
                result = enforce_stack_layout(
                        control.adapter,
                        target,
                        focused_pane=pane,
                        focus=args.focus,
                        admit=args.admit,
                        kill_pending_clear=args.kill_pending_clear,
                    )
                print(result)
                return 0

        if args.command == "legion":
            pane = args.pane
            if pane == "current":
                pane = control.adapter.run("display-message", "-p", "#{pane_id}").strip()
            if args.legion_command == "focus-selected":
                from .stack import focus_selected

                print(focus_selected(control.adapter, pane))
                return 0
            if args.legion_command == "enforce":
                from .stack import enforce_stack_layout

                target = control.adapter.run(
                    "display-message", "-t", pane, "-p", "#{session_name}:#{window_index}"
                ).strip()
                print(enforce_stack_layout(control.adapter, target, focused_pane=pane, focus=True))
                return 0

        if args.command == "legion":
            pane = args.pane
            if pane == "current":
                pane = control.adapter.run("display-message", "-p", "#{pane_id}").strip()
            if args.legion_command == "focus-selected":
                from .stack import focus_selected

                print(focus_selected(control.adapter, pane))
                return 0
            if args.legion_command == "enforce":
                from .stack import enforce_stack_layout

                target = control.adapter.run(
                    "display-message", "-t", pane, "-p", "#{session_name}:#{window_index}"
                ).strip()
                print(enforce_stack_layout(control.adapter, target, focused_pane=pane))
                return 0

        if args.command == "create":
            print(control.create_workspace(args.session))
            if args.attach:
                from .builder import attach_workspace

                attach_workspace(args.session)
            return 0

        if args.command == "rebuild-window":
            session_name, window_index = _parse_window_ref(args.window, control)
            print(control.rebuild_window(session_name=session_name, window_index=window_index))
            return 0

        if args.command == "assert-instance":
            import json as _json
            from .assertions import assert_instance

            result = assert_instance(control.adapter, args.pane)
            print(_json.dumps(result))
            return 0 if result.get("ok") else 1

        if args.command == "mechanicus-focus-guard":
            import json as _json
            from .focus_guard import remember_or_bounce

            result = remember_or_bounce(
                control.adapter,
                pane=args.pane,
                client=args.client,
                surface=args.surface,
            )
            print(_json.dumps(result))
            return 0

        if args.command == "allow-mechanicus-focus":
            from .focus_guard import allow_temporarily

            until = allow_temporarily(
                control.adapter,
                seconds=args.seconds,
                reason=args.reason,
                actor="tmuxctl",
            )
            print(f"{until:.3f}")
            return 0

        if args.command == "allow-human-mechanicus-focus":
            from .focus_guard import allow_human_focus

            allow_human_focus(
                control.adapter,
                client=args.client,
                reason=args.reason,
                actor="tmuxctl",
            )
            print("ok")
            return 0

        if args.command == "pane-select":
            from .pane_select import select_pane

            print(
                select_pane(
                    control.adapter,
                    mode=args.mode,
                    direction=args.direction,
                    client=args.client,
                )
            )
            return 0

        parser.error(f"unhandled command: {args.command}")
    except (
        TmuxError,
        RegistryError,
        ValueError,
        argparse.ArgumentTypeError,
        NotImplementedError,
    ) as exc:
        print(f"tmuxctl: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
