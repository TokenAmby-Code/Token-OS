from __future__ import annotations

import argparse
import json
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
    focus_parser.add_argument("mode", nargs="?", default="toggle", choices=["toggle", "focus-grid", "unfocus-grid", "focus-side", "unfocus-side"])
    focus_parser.add_argument("--window", default="current")

    restart_parser = subparsers.add_parser("restart")
    restart_parser.add_argument("--session", default="main")
    mode = restart_parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")

    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--session", default="main")

    resolve_parser = subparsers.add_parser("resolve-pane")
    resolve_parser.add_argument("target")
    resolve_parser.add_argument("--format", choices=["full", "id", "json"], default="full")

    send_text_parser = subparsers.add_parser("send-text")
    send_text_parser.add_argument("--pane", required=True)
    text_source = send_text_parser.add_mutually_exclusive_group(required=True)
    text_source.add_argument("--text")
    text_source.add_argument("--stdin", action="store_true")
    send_text_parser.add_argument("--clear-prompt", action="store_true")

    audience_parser = subparsers.add_parser("audience")
    audience_subparsers = audience_parser.add_subparsers(dest="audience_command", required=True)

    audience_toggle = audience_subparsers.add_parser("toggle")
    audience_toggle.add_argument("--pane", default="current")
    audience_toggle.add_argument("--client", default="")

    audience_return = audience_subparsers.add_parser("return")
    audience_return.add_argument("--pane", default="current")
    audience_return.add_argument("--client", default="")

    tombstone_parser = subparsers.add_parser("tombstone")
    tombstone_subparsers = tombstone_parser.add_subparsers(
        dest="tombstone_command", required=True
    )

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
            print(control.focus(session_name=session_name, window_index=window_index, mode=args.mode))
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
            resolved = control.resolve_pane_resolution(args.target)
            if args.format == "id":
                print(resolved.pane_id)
            elif args.format == "json":
                print(json.dumps({
                    "requested": resolved.requested,
                    "pane_id": resolved.pane_id,
                    "role": resolved.pane_role,
                    "kind": resolved.pane_kind.value,
                    "chain": list(resolved.chain),
                }))
            else:
                print(control.format_pane_resolution(resolved))
            return 0

        if args.command == "send-text":
            text = sys.stdin.read() if args.stdin else args.text
            control.adapter.send_text_then_submit(
                args.pane,
                text,
                clear_prompt=args.clear_prompt,
            )
            return 0

        if args.command == "audience":
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
                pane_id = add_stack_pane(
                    control.adapter, args.session, args.base, cwd=args.cwd
                )
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
            if args.stack_command == "enforce":
                from .legion import enforce_stack_layout

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
                print(
                    enforce_stack_layout(
                        control.adapter,
                        target,
                        focused_pane=pane,
                        focus=args.focus,
                        admit=args.admit,
                        kill_pending_clear=args.kill_pending_clear,
                    )
                )
                return 0

        if args.command == "legion":
            pane = args.pane
            if pane == "current":
                pane = control.adapter.run("display-message", "-p", "#{pane_id}").strip()
            if args.legion_command == "focus-selected":
                from .legion import focus_selected

                print(focus_selected(control.adapter, pane))
                return 0
            if args.legion_command == "enforce":
                from .legion import enforce_legion_layout

                target = control.adapter.run(
                    "display-message", "-t", pane, "-p", "#{session_name}:#{window_index}"
                ).strip()
                print(enforce_legion_layout(control.adapter, target, focused_pane=pane, focus=True))
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
