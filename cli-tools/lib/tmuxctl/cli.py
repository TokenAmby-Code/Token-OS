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

    restart_parser = subparsers.add_parser("restart")
    restart_parser.add_argument("--session", default="main")
    mode = restart_parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")

    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--session", default="main")

    resolve_parser = subparsers.add_parser("resolve-pane")
    resolve_parser.add_argument("target")

    audience_parser = subparsers.add_parser("audience")
    audience_subparsers = audience_parser.add_subparsers(dest="audience_command", required=True)

    audience_toggle = audience_subparsers.add_parser("toggle")
    audience_toggle.add_argument("--pane", default="current")

    audience_return = audience_subparsers.add_parser("return")
    audience_return.add_argument("--pane", default="current")

    stack_parser = subparsers.add_parser("stack")
    stack_subparsers = stack_parser.add_subparsers(dest="stack_command", required=True)

    stack_add = stack_subparsers.add_parser("add")
    stack_add.add_argument("base", help="stack window base: legion, mechanicus, mars, kreig")
    stack_add.add_argument("--cwd", default=None)
    stack_add.add_argument("--session", default="main")

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
            print(control.resolve_pane(args.target))
            return 0

        if args.command == "audience":
            pane = args.pane
            if pane == "current":
                pane = control.adapter.run("display-message", "-p", "#{pane_id}").strip()
            if args.audience_command == "toggle":
                print(control.audience_toggle(pane))
                return 0
            if args.audience_command == "return":
                print(control.audience_return(pane))
                return 0

        if args.command == "stack":
            if args.stack_command == "add":
                from .stack import add_stack_pane
                pane_id = add_stack_pane(
                    control.adapter, args.session, args.base, cwd=args.cwd
                )
                print(pane_id)
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
