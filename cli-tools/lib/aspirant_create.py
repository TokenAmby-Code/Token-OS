#!/usr/bin/env python3
"""Internal aspirant creation helper for dispatch and tests.

This module is intentionally not exposed as a public bin command; dispatch is the public surface.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

VALID_KINDS = {"dispatch", "deploy_p", "deploy_d"}
VALID_DOMAINS = {"terra", "mars"}
TOKEN_OS_ROOT = Path(__file__).resolve().parents[2]
ASPIRANT_PERSONA = "aspirant"
OBSIDIAN_SYNC_ILLEGAL_FILENAME_CHARS = r'<>:"/\\|?*'


def aspirant_persona_prompt_path() -> Path:
    return TOKEN_OS_ROOT / "cli-tools" / "prompts" / "aspirant-persona.md"


def eprint(*args: object) -> None:
    print("aspirant_create:", *args, file=sys.stderr)


def vault_root() -> Path:
    imperium = os.environ.get("IMPERIUM")
    if imperium:
        return Path(imperium) / "Imperium-ENV"
    # Fallback for local mac level-1 testing. Production shells should source nas-path.sh.
    return Path("/Volumes/Imperium/Imperium-ENV")


def imperium_env_vault_root() -> str:
    """Return the default aspirant working directory (the Imperium-ENV vault root)."""
    return str(vault_root())


def non_empty_string(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def slugify(value: str, fallback: str = "aspirant") -> str:
    slug = re.sub(r"[^\w\s-]", "", value).strip().lower()
    slug = re.sub(r"\s+", "-", slug)
    return (slug or fallback)[:80]


def human_filename_stem(value: str, fallback: str = "aspirant", max_len: int = 90) -> str:
    stem = re.sub(r"[\x00-\x1f\x7f]", "", value or "")
    stem = re.sub(f"[{re.escape(OBSIDIAN_SYNC_ILLEGAL_FILENAME_CHARS)}]", " ", stem)
    stem = re.sub(r"[_\s-]+", "-", stem.lower()).strip("- .")
    if stem.lower().endswith(".md"):
        stem = stem[:-3].strip(" .")
    stem = stem or fallback
    if len(stem) > max_len:
        stem = stem[:max_len].rstrip(" .")
    return stem or fallback


def yaml_scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def yaml_list(values: list[str]) -> list[str]:
    return [*(f"  - {yaml_scalar(v)}" for v in values)]


def yaml_key_list(key: str, values: list[str]) -> list[str]:
    if not values:
        return [f"{key}: []"]
    return [f"{key}:", *yaml_list(values)]


# Mandated starter questions seeded into every aspirant at intake.
# Schema (Emperor-prescribed 2026-05-15): questions:[] of {question, answer, state, importance}.
# state in {unanswered, refining, open, closed}. importance in 1..10.
# Trials-clear gate predicate (MVP): every entry has state == "closed". No special case for q1.
STARTER_QUESTIONS: list[dict[str, object]] = [
    {
        "question": "which other questions are needed for this aspirant?",
        "answer": (
            "add the answers as additional questions in this array, "
            "do not duplicate records to this answer field"
        ),
        "state": "unanswered",
        "importance": 10,
    },
    {
        "question": (
            "is there a better way to organize the thoughts from the gene seed? "
            "can we create a descriptive reshuffle to ONLY increase readability "
            "while preserving total semantic payload"
        ),
        "answer": None,
        "state": "unanswered",
        "importance": 8,
    },
]


def yaml_starter_questions(key: str = "questions") -> list[str]:
    out: list[str] = [f"{key}:"]
    for q in STARTER_QUESTIONS:
        out.append(f"  - question: {yaml_scalar(q['question'])}")
        out.append(f"    answer: {yaml_scalar(q['answer'])}")
        out.append(f"    state: {yaml_scalar(q['state'])}")
        out.append(f"    importance: {yaml_scalar(q['importance'])}")
    return out


def questions_panel_lines(max_rows: int = 8) -> list[str]:
    lines = [
        "> [!question]- Questions Gate — frontmatter editor",
        "> Questions live in frontmatter as `questions: []`. StopValidate clears only when every question has `state: closed`.",
        "> Add new question objects in source/YAML if all visible rows are full; Meta Bind inputs below edit existing array entries.",
        ">",
        "> ```dataview",
        "> TABLE WITHOUT ID q.state AS State, q.importance AS Importance, q.question AS Question, q.answer AS Answer",
        "> FLATTEN questions AS q",
        "> WHERE file.path = this.file.path",
        "> ```",
        ">",
        "> #### Editable fields",
        ">",
    ]
    for i in range(max_rows):
        lines += [
            f"> ##### Question {i}",
            f"> - Question: `INPUT[textArea:questions[{i}].question]`",
            f"> - Answer: `INPUT[textArea:questions[{i}].answer]`",
            f"> - State: `INPUT[inlineSelect(option(unanswered), option(refining), option(open), option(closed)):questions[{i}].state]`",
            f"> - Importance: `INPUT[slider(minValue(1), maxValue(10), stepSize(1)):questions[{i}].importance]`",
            ">",
        ]
    return lines


def unique_path(directory: Path, stem: str, suffix: str = ".md") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    counter = 2
    while True:
        candidate = directory / f"{stem} {counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def rel_to_vault(path: Path, vault: Path) -> str:
    try:
        return str(path.relative_to(vault))
    except ValueError:
        return str(path)


def infer_note_type(kind: str) -> str:
    return "descriptive" if kind == "deploy_d" else "prescriptive"


def dispatch_schema_complete(args: argparse.Namespace) -> tuple[bool, str | None]:
    missing: list[str] = []
    if not args.objective.strip():
        missing.append("objective")
    if not args.engine:
        missing.append("engine")
    if not args.persona:
        missing.append("persona")
    if not args.dir:
        missing.append("target_working_dir")
    elif not Path(args.dir).expanduser().exists():
        missing.append("target_working_dir_exists")
    if not args.target:
        missing.append("dispatch_target")
    if not args.victory_condition:
        missing.append("victory_conditions")
    if missing:
        return False, "missing " + ", ".join(missing)
    return True, None


def build_note_content(
    args: argparse.Namespace,
    note_status: str,
    dispatch_schema_is_complete: bool,
    blocked_reason: str | None,
    session_doc_rel: str | None,
) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    persona_prompt = str(aspirant_persona_prompt_path())
    note_type = infer_note_type(args.kind)
    tags = [f"type/{note_type}", "inbox/aspirant", f"aspirant/{args.kind}"]
    if args.kind in {"dispatch", "deploy_p"}:
        tags.append("mars/task")
    else:
        tags.append("terra/ultramar")

    lines: list[str] = [
        "---",
        f"title: {yaml_scalar(args.title)}",
        f"type: {note_type}",
        "prescriptive: " + ("false" if note_type == "descriptive" else "true"),
        f"created: {today}",
        f"status: {note_status}",
        "aspirant: true",
        f"aspirant_kind: {args.kind}",
        f"source: {yaml_scalar(args.source)}",
        "creation_surface: dispatch",
        "trials_verdict: pending",
        *yaml_starter_questions(),
        "tags:",
        *[f"  - {yaml_scalar(t)}" for t in tags],
    ]

    if args.kind == "dispatch":
        lines += [
            f"aspirant_persona: {ASPIRANT_PERSONA}",
            f"aspirant_persona_prompt: {yaml_scalar(persona_prompt)}",
            "dispatch_boundary: true",
            f"dispatch_schema_complete: {str(dispatch_schema_is_complete).lower()}",
            "dispatch_ready: false",
            f"dispatch_blocked_reason: {yaml_scalar(blocked_reason)}",
            "operator_approved_dispatch: false",
            f"engine: {yaml_scalar(args.engine)}",
            f"persona: {yaml_scalar(args.persona)}",
            f"target_working_dir: {yaml_scalar(str(Path(args.dir).expanduser()) if args.dir else None)}",
            f"dispatch_target: {yaml_scalar(args.target)}",
            f"zealotry: {yaml_scalar(args.zealotry)}",
            f"aspirant_session_doc: {yaml_scalar(session_doc_rel)}",
            f"system_prompt_file: {yaml_scalar(args.system_prompt_file)}",
            f"prompt_file: {yaml_scalar(args.prompt_file)}",
            *yaml_key_list("victory_conditions", args.victory_condition),
        ]
    elif args.kind == "deploy_p":
        lines += [
            "deployment_ready: false",
            "deployment_kind: prescriptive",
            "deployment_target: Mars/Tasks",
            "progress: 0",
            "completed: false",
        ]
    else:
        lines += [
            "deployment_ready: false",
            "deployment_kind: descriptive",
            "deployment_target: Terra/Ultramar",
        ]

    lines += ["---", "", f"# {args.title}", "", "> [!dna] Gene-Seed"]
    objective_lines = args.objective.strip().splitlines() or ["(empty)"]
    lines += [f"> {line}" if line else ">" for line in objective_lines]
    lines += ["", "## Intake", "", f"- Kind: `{args.kind}`", f"- Source: `{args.source}`"]
    if blocked_reason:
        lines.append(f"- Dispatch blocked: {blocked_reason}")
    lines += ["", *questions_panel_lines()]
    return "\n".join(lines) + "\n"


def build_session_doc(
    args: argparse.Namespace,
    note_rel: str,
    status: str,
    dispatch_schema_is_complete: bool,
    blocked_reason: str | None,
) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    persona_prompt = str(aspirant_persona_prompt_path())
    vc = args.victory_condition or [
        "Aspirant identifies blocking open questions and validates dispatch metadata without launching workers."
    ]
    tags = ["mars/session", "aspirant/dispatch", "system/dispatch"]
    lines = [
        "---",
        "session_doc_id: null",
        "vault: Imperium-ENV",
        f"created: {today}",
        "project: aspirants",
        "agents: []",
        f"status: {status}",
        "type: session",
        "aspirant: true",
        "aspirant_kind: dispatch",
        f"aspirant_persona: {ASPIRANT_PERSONA}",
        f"aspirant_persona_prompt: {yaml_scalar(persona_prompt)}",
        f"aspirant_note: {yaml_scalar(note_rel)}",
        f"dispatch_schema_complete: {str(dispatch_schema_is_complete).lower()}",
        "dispatch_ready: false",
        f"dispatch_blocked_reason: {yaml_scalar(blocked_reason)}",
        "trials_verdict: pending",
        "operator_approved_dispatch: false",
        *yaml_starter_questions(),
        f"engine: {yaml_scalar(args.engine)}",
        f"persona: {yaml_scalar(args.persona)}",
        f"target_working_dir: {yaml_scalar(str(Path(args.dir).expanduser()) if args.dir else None)}",
        f"dispatch_target: {yaml_scalar(args.target)}",
        f"zealotry: {yaml_scalar(args.zealotry)}",
        "related_session_docs: []",
        "linked_docs:",
        f"  - {yaml_scalar(note_rel)}",
        "tags:",
        *[f"  - {yaml_scalar(t)}" for t in tags],
        *yaml_key_list("victory_conditions", vc),
        "---",
        "",
        f"# Aspirant Dispatch — {args.title}",
        "",
        "## Objective",
        "",
        args.objective.strip() or "_No objective supplied._",
        "",
        "## Dispatch Intake",
        "",
        f"- Aspirant note: [[{note_rel.replace('.md', '')}]]",
        f"- Dispatch schema complete: `{str(dispatch_schema_is_complete).lower()}`",
        "- Dispatch ready: `false`",
        "- Trials verdict: `pending`",
        "- Operator approved dispatch: `false`",
    ]
    if blocked_reason:
        lines.append(f"- Blocked reason: `{blocked_reason}`")
    lines += [
        "",
        "## Dispatch Boundary",
        "",
        "This is an adversarial trials session for future dispatch; no downstream agent has been launched yet.",
        "The aspirant must generate and maintain proactive `questions` (closing each entry only when answered or waived) until none remain non-closed.",
        "Repeated wakeups are not approval. `dispatch_ready` stays false until a separate explicit operator-authorized dispatch/worker phase.",
        "",
        *questions_panel_lines(),
        "## Activity Log",
        "",
    ]
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Internal aspirant creation helper. Use dispatch --aspirant publicly."
    )
    p.add_argument("--kind", required=True, choices=sorted(VALID_KINDS))
    p.add_argument("--title", required=True)
    p.add_argument("--objective", required=True, type=non_empty_string)
    p.add_argument("--source", default="dispatch")
    p.add_argument("--session-domain", choices=sorted(VALID_DOMAINS), default="mars")
    p.add_argument("--engine", choices=["claude", "codex"], default=None)
    p.add_argument("--dir", default=imperium_env_vault_root())
    p.add_argument("--persona", default=None)
    p.add_argument("--target", default=None)
    p.add_argument("--zealotry", type=int, default=None)
    p.add_argument("--session-doc", default=None)
    p.add_argument("--system-prompt-file", default=None)
    p.add_argument("--prompt-file", default=None)
    p.add_argument("--victory-condition", action="append", default=[])
    p.add_argument("--json", action="store_true")
    return p.parse_args(argv)


def aspirant_create(args: argparse.Namespace) -> dict[str, object]:
    vault = vault_root()
    if not vault.exists():
        raise FileNotFoundError(f"vault not found: {vault}")

    dispatch_schema_is_complete = False
    blocked_reason = None
    note_status = "aspirant_intake"
    session_doc_path: Path | None = None
    session_doc_rel: str | None = None

    if args.kind == "dispatch":
        dispatch_schema_is_complete, blocked_reason = dispatch_schema_complete(args)
        if dispatch_schema_is_complete:
            note_status = "aspirant_trials"
            blocked_reason = "pending_aspirant_trials"
        else:
            note_status = "aspirant_intake"

    note_stem = slugify(args.title)
    note_path = unique_path(vault / "Aspirants", note_stem)
    note_rel = rel_to_vault(note_path, vault)

    if args.kind == "dispatch":
        if args.session_doc:
            session_doc_path = Path(args.session_doc).expanduser()
            if not session_doc_path.is_absolute():
                session_doc_path = vault / args.session_doc
        else:
            session_dir = vault / (
                "Terra/Sessions" if args.session_domain == "terra" else "Mars/Sessions"
            )
            session_doc_path = unique_path(
                session_dir,
                human_filename_stem(f"Aspirant - {args.title}", fallback="aspirant"),
            )
        session_doc_rel = rel_to_vault(session_doc_path, vault)

    note_path.write_text(
        build_note_content(
            args, note_status, dispatch_schema_is_complete, blocked_reason, session_doc_rel
        ),
        encoding="utf-8",
    )

    if args.kind == "dispatch" and session_doc_path:
        if session_doc_path.exists():
            # Do not clobber an explicitly supplied session doc; linking is enough for L1.
            pass
        else:
            session_doc_path.parent.mkdir(parents=True, exist_ok=True)
            session_doc_path.write_text(
                build_session_doc(
                    args, note_rel, note_status, dispatch_schema_is_complete, blocked_reason
                ),
                encoding="utf-8",
            )

    return {
        "created": True,
        "kind": args.kind,
        "status": note_status,
        "note_path": note_rel,
        "session_doc": session_doc_rel,
        "dispatch_schema_complete": dispatch_schema_is_complete
        if args.kind == "dispatch"
        else None,
        "dispatch_ready": False if args.kind == "dispatch" else None,
        "dispatch_blocked_reason": blocked_reason,
        "trials_verdict": "pending",
        "operator_approved_dispatch": False if args.kind == "dispatch" else None,
    }


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.zealotry is not None and not (1 <= args.zealotry <= 10):
        eprint("--zealotry must be 1-10")
        return 64

    try:
        result = aspirant_create(args)
    except FileNotFoundError as exc:
        eprint(str(exc))
        return 65

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        for key, value in result.items():
            if value is not None:
                print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
