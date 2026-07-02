from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

from .api import RegistryError
from .service import TmuxControlPlane
from .tmux_adapter import TmuxError

_RAW_TMUX_ID_RX = re.compile(r"%\d+")
_PUBLIC_PANE_ID_RX = re.compile(r"^[^:%\s]+:[^:%\s]+$")


def _public_or_unresolved(control: TmuxControlPlane, target: str | None) -> str:
    if not target:
        return "unresolved"
    try:
        return control.public_pane_id(target)
    except Exception:
        return "unresolved"


def _safe_public_role(role: str | None) -> str:
    value = (role or "").strip()
    if not value:
        return "unresolved"
    if _RAW_TMUX_ID_RX.search(value):
        return "unresolved"
    return value if _PUBLIC_PANE_ID_RX.fullmatch(value) else "unresolved"


def _tmuxctld_url() -> str:
    return os.environ.get("TMUXCTLD_URL", "http://127.0.0.1:7778").rstrip("/")


def _post_tmuxctld(path: str, payload: dict, *, timeout: float = 30.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{_tmuxctld_url()}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            envelope = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.URLError as exc:
        raise ValueError(f"tmuxctld unreachable for {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"tmuxctld returned invalid JSON for {path}: {exc}") from exc
    if not isinstance(envelope, dict):
        raise ValueError(f"tmuxctld returned invalid envelope for {path}: {envelope!r}")
    if not envelope.get("ok"):
        err = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
        message = err.get("message") or envelope.get("error") or "unknown daemon error"
        raise ValueError(f"tmuxctld {path} failed: {message}")
    result = envelope.get("result", {})
    if not isinstance(result, dict):
        raise ValueError(f"tmuxctld returned invalid result for {path}: {result!r}")
    return result


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

    # `--physical` is the debug-only opt-in that re-surfaces raw tmux %NN ids.
    # Default render is canonical-only (the @PANE_ID role) per the canonical-id
    # campaign, so the normal path never leaks volatile physical ids.
    _physical_help = "also show raw tmux physical pane ids (%%NN); debug only"

    inspect_workspace = inspect_subparsers.add_parser("workspace")
    inspect_workspace.add_argument("--session", default="main")
    inspect_workspace.add_argument("--physical", action="store_true", help=_physical_help)

    inspect_restart = inspect_subparsers.add_parser("restart-plan")
    inspect_restart.add_argument("--session", default="main")

    inspect_window = inspect_subparsers.add_parser("window")
    inspect_window.add_argument("--window", default="current")
    inspect_window.add_argument("--physical", action="store_true", help=_physical_help)

    inspect_pane = inspect_subparsers.add_parser("pane")
    inspect_pane.add_argument("--pane", required=True)
    inspect_pane.add_argument("--physical", action="store_true", help=_physical_help)

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

    metal_observe_parser = subparsers.add_parser(
        "metal-observe",
        help="Read-only metal observation: per-pane engine + resume id from live tmux + filesystem (no DB).",
    )
    metal_observe_parser.add_argument("--session", required=True)
    metal_observe_parser.add_argument("--format", choices=["text", "json"], default="text")

    metal_restart_parser = subparsers.add_parser(
        "metal-restart",
        help="DB-free restart: resume each live agent pane from metal-observed transcripts (sandbox-only).",
    )
    metal_restart_parser.add_argument("--session", required=True)
    metal_restart_parser.add_argument("--dry-run", action="store_true")

    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--session", default="main")

    resolve_parser = subparsers.add_parser("resolve-pane")
    resolve_parser.add_argument(
        "--format", choices=["id", "physical", "full", "json"], default="full"
    )
    resolve_parser.add_argument("target")

    resolve_instance_parser = subparsers.add_parser(
        "resolve-instance",
        help="Resolve an instance UUID to its live tmux pane (pure tmux, fail-closed).",
    )
    resolve_instance_parser.add_argument(
        "--format", choices=["json", "physical", "role", "full"], default="full"
    )
    resolve_instance_parser.add_argument("instance_id")

    translate_ids_parser = subparsers.add_parser(
        "translate-ids",
        help="Translate raw tmux %%pane ids on stdin to public {page}:{id} ids.",
    )
    translate_ids_parser.add_argument(
        "--unresolved",
        default="unresolved",
        help="Replacement for raw ids that have no live public @PANE_ID mapping.",
    )

    freelist_parser = subparsers.add_parser(
        "freelist",
        help="List unoccupied, agent-free panes (derived from the daemon occupancy ledger).",
    )
    freelist_parser.add_argument("--format", choices=["text", "json", "ids"], default="text")
    freelist_parser.add_argument(
        "--first",
        action="store_true",
        help="Print only the first free pane id (empty + exit 1 if none).",
    )

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

    clear_runtime_parser = subparsers.add_parser(
        "clear-runtime",
        help="Clear pane runtime stamps plus tint/style/close overlays atomically.",
    )
    clear_runtime_parser.add_argument("--pane", required=True)

    close_pane_parser = subparsers.add_parser(
        "close-pane",
        help="Close a live agent pane through the tmux-owned close primitive.",
    )
    close_pane_parser.add_argument("--pane", required=True)
    close_pane_parser.add_argument("--timeout", type=float, default=3.0)

    close_parser = subparsers.add_parser(
        "close",
        help="Close an instance: Token-API lifecycle + tmuxctl pane close.",
    )
    close_parser.add_argument("--instance-id", required=True)
    close_parser.add_argument(
        "--lifecycle",
        default="retire",
        choices=["retire", "archive-session-doc", "banish"],
    )
    close_parser.add_argument("--mode", default="now", choices=["now", "after-stop"])
    close_parser.add_argument("--pane", default="")
    close_parser.add_argument("--timeout", type=float, default=3.0)
    close_parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Override the refuse-retire-while-TUI-live guard: kill a live agent "
            "process and retire atomically. Without it, a live TUI fails closed."
        ),
    )

    send_text_parser = subparsers.add_parser("send-text")
    send_text_parser.add_argument("--pane", required=True)
    text_source = send_text_parser.add_mutually_exclusive_group(required=True)
    text_source.add_argument("--text")
    text_source.add_argument("--stdin", action="store_true")
    send_text_parser.add_argument("--clear-prompt", action="store_true")
    send_text_parser.add_argument(
        "--no-submit",
        action="store_true",
        help=(
            "Insert the text as a DRAFT at the prompt — no C-m, no submit. Routes "
            "through the insert-only primitive (insert_text) instead of "
            "send_text_then_submit, so it can never auto-submit over a human's "
            "draft. Mutually exclusive with --clear-prompt."
        ),
    )

    invoke_skill_parser = subparsers.add_parser(
        "invoke-skill",
        help="Insert a harness-correct explicit skill invocation at the prompt start.",
    )
    invoke_skill_parser.add_argument("skill")
    invoke_skill_parser.add_argument("--pane", default="current")
    invoke_skill_parser.add_argument(
        "--instance-id",
        default="",
        help="Resolve a live pane by instance UUID instead of --pane.",
    )
    invoke_skill_parser.add_argument("--agent", default="auto")
    invoke_skill_parser.add_argument(
        "--arguments",
        default="",
        help="Text appended after the skill invocation.",
    )
    invoke_skill_parser.add_argument(
        "--submit",
        action="store_true",
        help="Submit the skill invocation through tmuxctl's gated send primitive.",
    )
    invoke_skill_parser.add_argument(
        "--clear-prompt",
        action="store_true",
        help="Clear the prompt before --submit delivery.",
    )
    invoke_skill_parser.add_argument("--dry-run", action="store_true")

    # Decomposed prompt-cursor ops. The plan-menu pre-buffers prompt-start (the
    # expensive, selection-independent 50x PgUp) while the picker is open, inserts
    # the chosen leader, then always runs prompt-end on exit to restore the cursor.
    prompt_start_parser = subparsers.add_parser(
        "prompt-start",
        help="Move the pane cursor to the prompt start (N x PgUp + Home).",
    )
    prompt_start_parser.add_argument("--pane", required=True)
    prompt_start_parser.add_argument("--page-ups", type=int, default=50)

    insert_text_parser = subparsers.add_parser(
        "insert-text",
        help="Insert literal text at the cursor (no leader logic, no submit).",
    )
    insert_text_parser.add_argument("--pane", required=True)
    insert_text_parser.add_argument("--text", required=True)

    prompt_end_parser = subparsers.add_parser(
        "prompt-end",
        help="Return the pane cursor to the prompt end (N x PgDn + End).",
    )
    prompt_end_parser.add_argument("--pane", required=True)
    prompt_end_parser.add_argument("--page-downs", type=int, default=50)

    resolve_agent_parser = subparsers.add_parser(
        "resolve-agent",
        help="Print the harness (claude|codex) bound to a pane.",
    )
    resolve_agent_parser.add_argument("--pane", default="current")
    resolve_agent_parser.add_argument("--agent", default="auto")
    resolve_agent_parser.add_argument(
        "--default",
        default="claude",
        choices=["claude", "codex", "auto"],
        help="Value to print when detection is inconclusive (use 'auto' to fail closed).",
    )

    persona_engine_parser = subparsers.add_parser(
        "persona-engine",
        help="Hot-swap a protected persona singleton pane between Claude and Codex.",
    )
    persona_engine_parser.add_argument("--pane", default="current")
    persona_engine_parser.add_argument("--session", default=None)
    persona_engine_mode = persona_engine_parser.add_mutually_exclusive_group(required=True)
    persona_engine_mode.add_argument("--toggle", action="store_true")
    persona_engine_mode.add_argument("--engine", choices=["claude", "codex"])

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
    stack_add.add_argument("base", help="stack window base: mechanicus, mars, kreig, reservists")
    stack_add.add_argument("--cwd", default=None)
    stack_add.add_argument("--session", default="main")
    stack_add.add_argument("--no-focus", action="store_true")
    stack_dispatch = stack_subparsers.add_parser("dispatch")
    stack_dispatch.add_argument(
        "base", help="stack window base: mechanicus, mars, kreig, reservists"
    )
    stack_dispatch.add_argument("--cwd", default=None)
    stack_dispatch.add_argument("--session", default="main")
    stack_dispatch.add_argument("--command", dest="launch_command", required=True)
    stack_dispatch.add_argument("--no-focus", action="store_true")
    stack_dispatch.add_argument("--settle", type=float, default=0.5)
    stack_adopt = stack_subparsers.add_parser("adopt")
    stack_adopt.add_argument("base", help="orchestrator stack base: legion, mechanicus")
    stack_adopt.add_argument(
        "--pane", required=True, help="existing live pane to join into the stack"
    )
    stack_adopt.add_argument("--cwd", default=None)
    stack_adopt.add_argument("--session", default="main")
    stack_adopt.add_argument("--no-focus", action="store_true")
    stack_enforce = stack_subparsers.add_parser("enforce")
    stack_enforce.add_argument("--pane", default="current")
    stack_enforce.add_argument("--window", default="")
    stack_enforce.add_argument("--focus", action="store_true")
    stack_enforce.add_argument("--admit", action="store_true")
    stack_enforce.add_argument("--kill-pending-clear", action="store_true")
    stack_sweep = stack_subparsers.add_parser("sweep")
    stack_sweep.add_argument("--session", default="main")
    stack_sweep.add_argument("--keep-pending-clear", action="store_true")

    mechanicus_parser = subparsers.add_parser("mechanicus")
    mechanicus_subparsers = mechanicus_parser.add_subparsers(
        dest="mechanicus_command", required=True
    )

    mechanicus_focus = mechanicus_subparsers.add_parser("focus-selected")
    mechanicus_focus.add_argument("--pane", default="current")

    mechanicus_enforce = mechanicus_subparsers.add_parser("enforce")
    mechanicus_enforce.add_argument("--pane", default="current")

    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--session", default="main")
    create_parser.add_argument("--attach", action="store_true")

    rebuild_parser = subparsers.add_parser("rebuild-window")
    rebuild_parser.add_argument("--window", default="current")

    assert_instance_parser = subparsers.add_parser("assert-instance")
    assert_instance_parser.add_argument("--pane", required=True)

    # `assert-personas` (the manual persona sweep) was retired with the 2-min cron
    # that drove it: persona reconcile is now event-driven via the tmuxctld daemon
    # (POST /reconcile + /event). For a manual reconcile, curl -X POST :7778/reconcile.

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

    pane_live_parser = subparsers.add_parser(
        "pane-live",
        help="Exit 0 iff a live Claude/Codex agent process is running in the pane "
        "(the dispatch launch success criterion; read from the process tree).",
    )
    pane_live_parser.add_argument("pane")
    pane_live_parser.add_argument("--format", choices=["text", "json"], default="text")

    live_agents_parser = subparsers.add_parser(
        "live-agents",
        help="List panes running a live agent rooted in a working dir "
        "(the dispatch duplicate-refusal guard).",
    )
    live_agents_parser.add_argument("--dir", required=True)
    live_agents_parser.add_argument(
        "--exclude-pane",
        default="",
        help="Drop this pane from the result (the dispatcher's own pane).",
    )
    live_agents_parser.add_argument("--format", choices=["text", "json"], default="text")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    control = TmuxControlPlane()

    try:
        if args.command == "inspect":
            if args.inspect_command == "workspace":
                print(control.inspect_workspace(args.session, physical=args.physical))
                return 0
            if args.inspect_command == "restart-plan":
                print(control.inspect_restart_plan(args.session))
                return 0
            if args.inspect_command == "window":
                session_name, window_index = _parse_window_ref(args.window, control)
                print(control.inspect_window(session_name, window_index, physical=args.physical))
                return 0
            if args.inspect_command == "pane":
                print(control.inspect_pane(args.pane, physical=args.physical))
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

        if args.command == "metal-observe":
            import json

            from .metal_resolver import (
                observation_to_dict,
                observe_and_resolve,
                render_observations,
            )

            observations = observe_and_resolve(control.adapter, args.session)
            if args.format == "json":
                print(json.dumps([observation_to_dict(obs) for obs in observations], indent=2))
            else:
                print(render_observations(observations))
            return 0

        if args.command == "metal-restart":
            from .metal_restart import metal_restart, render_metal_restart_result

            metal_result = metal_restart(control.adapter, args.session, dry_run=args.dry_run)
            print(render_metal_restart_result(metal_result))
            return 0 if metal_result.ok else 1

        if args.command == "doctor":
            output = control.doctor(args.session)
            try:
                from persona_behavior import invariant_issues

                persona_issues = invariant_issues()
            except Exception as exc:  # pragma: no cover - defensive doctor surface
                persona_issues = [f"persona behavior invariant check errored: {exc}"]
            if persona_issues:
                output = output + "\n" + "\n".join(f"  ! {issue}" for issue in persona_issues)
            print(output)
            return 1 if persona_issues else 0

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

        if args.command == "resolve-instance":
            import json

            result = control.resolve_instance(args.instance_id)
            if args.format == "json":
                print(json.dumps(result))
            elif not result["found"]:
                # Exit 1 is the not-found sentinel for physical/role/full formats;
                # nothing is printed so callers never mistake a stale value for live.
                return 1
            elif args.format == "physical":
                print(result["pane_id"])
            elif args.format == "role":
                print(_safe_public_role(result.get("pane_role")))
            else:  # full
                public_role = _safe_public_role(result.get("pane_role"))
                print(f"instance_id: {result['instance_id']}")
                print(f"public_id: {public_role}")
                print(f"pane_role: {public_role}")
                print("physical_pane_id: (internal-only; use --format physical)")
                print(f"found: {str(result['found']).lower()}")
            return 0 if result["found"] else 1

        if args.command == "translate-ids":
            from .public_ids import physical_to_public_id_map, translate_physical_ids

            text = sys.stdin.read()
            mapping = physical_to_public_id_map(control.adapter)
            translated = translate_physical_ids(text, mapping, unresolved=args.unresolved)
            if text and not text.endswith("\n"):
                translated += "\n"
            sys.stdout.write(translated)
            return 0

        if args.command == "freelist":
            import json

            panes = control.freelist()
            if args.first:
                if not panes:
                    return 1
                print(_safe_public_role(panes[0].get("pane_role")))
                return 0
            if args.format == "json":
                panes = [
                    {
                        **free_pane,
                        "pane_id": _safe_public_role(free_pane.get("pane_role")),
                        "pane_role": _safe_public_role(free_pane.get("pane_role")),
                    }
                    for free_pane in panes
                ]
                print(json.dumps(panes))
            elif args.format == "ids":
                for free_pane in panes:
                    print(_safe_public_role(free_pane.get("pane_role")))
            else:  # text
                for free_pane in panes:
                    role = _safe_public_role(free_pane.get("pane_role"))
                    print(f"{role}\t{role}\t{free_pane['window_name']}")
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

        if args.command == "clear-runtime":
            import json

            from .close import clear_runtime

            print(json.dumps(clear_runtime(control.adapter, args.pane), sort_keys=True))
            return 0

        if args.command == "close-pane":
            import json

            from .close import close_pane

            result = close_pane(control.adapter, args.pane, timeout=args.timeout)
            print(json.dumps(result, sort_keys=True))
            return 0 if result.get("status") in {"closed", "already_closed"} else 1

        if args.command == "close":
            import json

            from .close import close_instance

            result = close_instance(
                control.adapter,
                args.instance_id,
                lifecycle=args.lifecycle,
                mode=args.mode,
                pane=args.pane or None,
                timeout=args.timeout,
                force=args.force,
            )
            print(json.dumps(result, sort_keys=True))
            return 0 if result.get("status") not in {"failed", "refused"} else 1

        if args.command == "send-text":
            import json as _json

            from .assertions import assert_instance

            if args.no_submit and args.clear_prompt:
                raise ValueError(
                    "--no-submit is insert-only and cannot be combined with --clear-prompt"
                )

            assertion = assert_instance(control.adapter, args.pane)
            if not assertion.get("ok"):
                if assertion.get("action") == "persona_correction_sent":
                    raise ValueError(
                        "persona correction sent; retry after settle before sending payload: "
                        f"{_json.dumps(assertion)}"
                    )
                if not assertion.get("deliverable"):
                    raise ValueError(f"pane has no live instance: {_json.dumps(assertion)}")
                # FAIL OPEN: the live runtime is present but the persona correction is
                # stuck after bounded attempts. Payload delivery is primary — deliver
                # the byte-bearing payload + emit a loud diagnostic rather than
                # suppressing it (the persona_assert_suppressed_stuck live blocker).
                print(
                    "tmuxctl send-text: FAIL-OPEN delivering payload despite stuck persona "
                    f"correction: {_json.dumps(assertion)}",
                    file=sys.stderr,
                )
            text = sys.stdin.read() if args.stdin else args.text
            if args.no_submit:
                # Draft mode: insert the literal text at the prompt and STOP. No
                # C-m is ever issued, so this can never auto-submit over a human's
                # in-progress draft — the insert path is the only attended-safe way
                # to land text in a pane. (send_text_then_submit keeps its
                # double-C-m submit semantics for the normal path.)
                control.insert_text(args.pane, text)
            else:
                control.adapter.send_text_then_submit(
                    args.pane,
                    text,
                    clear_prompt=args.clear_prompt,
                )
            return 0

        if args.command == "invoke-skill":
            from .skill_invoke import normalize_agent, skill_invocation_text

            if args.instance_id and args.pane != "current":
                raise ValueError("use either --instance-id or --pane, not both")
            if args.clear_prompt and not args.submit:
                raise ValueError("--clear-prompt requires --submit")
            pane = args.pane
            if args.instance_id:
                instance_resolution = control.resolve_instance(args.instance_id)
                if not instance_resolution["found"]:
                    raise ValueError(f"instance has no live pane: {args.instance_id}")
                pane = str(instance_resolution["pane_id"])
            if pane == "current" and not args.dry_run:
                pane = control.adapter.run("display-message", "-p", "#{pane_id}").strip()
            if args.dry_run:
                agent = normalize_agent(args.agent)
                if agent == "auto":
                    agent = "claude"
                print(skill_invocation_text(args.skill, agent, args.arguments))
                return 0
            if args.submit:
                print(
                    control.send_skill(
                        pane,
                        args.skill,
                        agent=args.agent,
                        arguments=args.arguments,
                        clear_prompt=args.clear_prompt,
                    ),
                    end="",
                )
            else:
                print(
                    control.invoke_skill(
                        pane,
                        args.skill,
                        agent=args.agent,
                        arguments=args.arguments,
                    ),
                    end="",
                )
            return 0

        if args.command == "prompt-start":
            control.move_to_prompt_start(args.pane, page_ups=args.page_ups)
            return 0

        if args.command == "insert-text":
            control.insert_text(args.pane, args.text)
            return 0

        if args.command == "prompt-end":
            control.move_to_prompt_end(args.pane, page_downs=args.page_downs)
            return 0

        if args.command == "resolve-agent":
            from .skill_invoke import resolve_agent_for_pane

            print(
                resolve_agent_for_pane(
                    control.adapter,
                    args.pane,
                    args.agent,
                    default=args.default,
                )
            )
            return 0

        if args.command == "persona-engine":
            pane = args.pane
            if pane == "current":
                # Capture the invoking pane once in the foreground client, then hand
                # that concrete target to tmuxctld. The daemon performs the only
                # respawn and never sweeps adjacent persona seats for this action.
                pane = control.adapter.run("display-message", "-p", "#{pane_id}").strip()
            result = _post_tmuxctld(
                "/persona-engine",
                {
                    "pane": pane,
                    "engine": args.engine or "",
                    "toggle": bool(args.toggle),
                    "session": args.session or "",
                },
            )
            import json as _persona_json

            print(_persona_json.dumps(result, sort_keys=True))
            return 0 if result.get("ok") else 1

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

                pane_id = add_stack_pane(
                    control.adapter,
                    args.session,
                    args.base,
                    cwd=args.cwd,
                    focus=not args.no_focus,
                )
                print(_public_or_unresolved(control, pane_id))
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
                print(_public_or_unresolved(control, pane_id))
                return 0
            if args.stack_command == "adopt":
                from .focus_guard import preserve_focus
                from .stack import add_stack_pane

                # join-pane is focus-mutating, so guard it like stack dispatch.
                with preserve_focus(
                    control.adapter,
                    source="tmuxctl stack adopt",
                    attempted_target=f"{args.session}:{args.base}",
                    enabled=os.environ.get("IMPERIUM_ALLOW_TMUX_FOCUS") != "1",
                ):
                    pane_id = add_stack_pane(
                        control.adapter,
                        args.session,
                        args.base,
                        cwd=args.cwd,
                        focus=not args.no_focus,
                        adopt_pane=args.pane,
                    )
                print(_public_or_unresolved(control, pane_id))
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
                layout_result = enforce_stack_layout(
                    control.adapter,
                    target,
                    focused_pane=pane,
                    focus=args.focus,
                    admit=args.admit,
                    kill_pending_clear=args.kill_pending_clear,
                )
                print(layout_result)
                return 0

        if args.command == "mechanicus":
            pane = args.pane
            if pane == "current":
                pane = control.adapter.run("display-message", "-p", "#{pane_id}").strip()
            if args.mechanicus_command == "focus-selected":
                from .stack import focus_selected

                print(focus_selected(control.adapter, pane))
                return 0
            if args.mechanicus_command == "enforce":
                from .stack import enforce_stack_layout

                target = control.adapter.run(
                    "display-message", "-t", pane, "-p", "#{session_name}:#{window_index}"
                ).strip()
                print(enforce_stack_layout(control.adapter, target, focused_pane=pane, focus=True))
                return 0

        if args.command == "mechanicus":
            pane = args.pane
            if pane == "current":
                pane = control.adapter.run("display-message", "-p", "#{pane_id}").strip()
            if args.mechanicus_command == "focus-selected":
                from .stack import focus_selected

                print(focus_selected(control.adapter, pane))
                return 0
            if args.mechanicus_command == "enforce":
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

            assertion_result = assert_instance(control.adapter, args.pane)
            print(_json.dumps(assertion_result))
            return 0 if assertion_result.get("ok") else 1

        if args.command == "mechanicus-focus-guard":
            import json as _json

            from .focus_guard import remember_or_bounce

            guard_result = remember_or_bounce(
                control.adapter,
                pane=args.pane,
                client=args.client,
                surface=args.surface,
            )
            print(_json.dumps(guard_result))
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

        if args.command == "pane-live":
            from .liveness import detect_pane_tui

            pane = args.pane
            if pane == "current":
                pane = control.adapter.run("display-message", "-p", "#{pane_id}").strip()
            tui = detect_pane_tui(control.adapter, pane)
            if args.format == "json":
                import json as _json

                print(
                    _json.dumps(
                        {
                            "pane_id": tui.pane_id,
                            "pane_pid": tui.pane_pid,
                            "agent_pid": tui.agent_pid,
                            "agent_command": tui.agent_command,
                            "live": tui.live,
                        }
                    )
                )
            elif tui.live:
                print(f"{tui.agent_pid}\t{tui.agent_command}")
            # Exit code IS the contract: 0 = live agent present, 1 = not live.
            return 0 if tui.live else 1

        if args.command == "live-agents":
            from .dispatch_liveness import live_agents_in_dir

            matches = live_agents_in_dir(
                control.adapter,
                args.dir,
                exclude_pane=args.exclude_pane or None,
            )
            if args.format == "json":
                import json as _json

                print(
                    _json.dumps(
                        [
                            {
                                "pane_id": m.pane_id,
                                "pane_pid": m.pane_pid,
                                "agent_pid": m.agent_pid,
                                "agent_command": m.agent_command,
                                "cwd": m.cwd,
                            }
                            for m in matches
                        ]
                    )
                )
            else:
                for m in matches:
                    print(
                        f"{control.public_pane_id(m.pane_id)}\t{m.pane_id}\t"
                        f"{m.agent_command or '?'}\t{m.cwd}"
                    )
            # Enumeration always succeeds; the caller decides refusal from the lines.
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
