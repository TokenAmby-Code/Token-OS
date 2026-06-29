#!/usr/bin/env python3
"""Generic command-boundary PreToolUse guard engine.

Reads a Claude/Codex hook payload from stdin, evaluates JSON-configured command
boundary rules, and emits PreToolUse deny JSON for the first matching rule.
Internal errors fail open by design.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SEPARATORS = {";", "&&", "||", "|", "(", ")"}
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*", re.S)


def _log(message: str) -> None:
    log_file = os.environ.get("COMMAND_BOUNDARY_LOG_FILE", "")
    if not log_file:
        return
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass


def _command_from_payload(payload: dict[str, Any]) -> str:
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    for key in ("command", "cmd"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    value = payload.get("command")
    return value if isinstance(value, str) else ""


def _cwd_from_payload(payload: dict[str, Any]) -> str:
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    env = payload.get("env") if isinstance(payload.get("env"), dict) else {}
    for value in (payload.get("cwd"), tool_input.get("cwd"), env.get("PWD")):
        if isinstance(value, str) and value:
            return value
    return ""


def shell_words(command: str) -> list[str]:
    # Treat unquoted newlines as command separators; shlex will preserve quoted text.
    out: list[str] = []
    quote: str | None = None
    escaped = False
    for ch in command:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in {"'", '"'}:
            out.append(ch)
            quote = ch
        elif ch == "\n":
            out.append(" ; ")
        else:
            out.append(ch)
    normalized = "".join(out)
    lexer = shlex.shlex(normalized, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def split_segments(words: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for word in words:
        if word in SEPARATORS or set(word) <= {";", "&", "|", "(", ")"}:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(word)
    if current:
        segments.append(current)
    return segments


def is_assignment(word: str) -> bool:
    return bool(ASSIGN_RE.match(word))


def assignment_matches_escape(word: str, env_name: str, values: set[str]) -> bool:
    if not env_name or not word.startswith(f"{env_name}="):
        return False
    value = word.split("=", 1)[1].strip().lower()
    return value in values


def strip_common_prefixes(
    segment: list[str], *, escape_env: str = "", escape_values: set[str] | None = None
) -> tuple[list[str], bool]:
    words = list(segment)
    escaped = False
    values = escape_values or set()

    while words and is_assignment(words[0]):
        if assignment_matches_escape(words[0], escape_env, values):
            escaped = True
        words.pop(0)

    while words and words[0] in {"command", "builtin"}:
        words.pop(0)
        while words and is_assignment(words[0]):
            if assignment_matches_escape(words[0], escape_env, values):
                escaped = True
            words.pop(0)

    if words and words[0] == "env":
        words.pop(0)
        while words:
            tok = words[0]
            if tok == "--":
                words.pop(0)
                break
            if tok.startswith("-"):
                words.pop(0)
                continue
            if is_assignment(tok):
                if assignment_matches_escape(tok, escape_env, values):
                    escaped = True
                words.pop(0)
                continue
            break

    if words and words[0] == "sudo":
        words.pop(0)
        while words and words[0].startswith("-"):
            opt = words.pop(0)
            if opt in {"-u", "-g", "-h", "-p"} and words:
                words.pop(0)
        while words and is_assignment(words[0]):
            if assignment_matches_escape(words[0], escape_env, values):
                escaped = True
            words.pop(0)

    return words, escaped


def command_position_match(command: str, matcher: dict[str, Any]) -> str | None:
    prefix = matcher.get("argv_prefix")
    if not isinstance(prefix, list) or not all(isinstance(x, str) for x in prefix) or not prefix:
        return None
    try:
        segments = split_segments(shell_words(command))
    except Exception:
        return None
    for raw in segments:
        seg, _ = strip_common_prefixes(raw)
        if len(seg) >= len(prefix) and seg[: len(prefix)] == prefix:
            return "command-position-" + "-".join(prefix)
    return None


def runtime_path_match(value: str, runtime_re: re.Pattern[str], *, current_runtime: bool = False) -> bool:
    if not value:
        return False
    v = value.strip().strip("\"'")
    low = v.lower()
    if low in {"$token_os", "${token_os}"} or low.startswith("$token_os/") or low.startswith("${token_os}/"):
        return True
    if runtime_re.search(v):
        return True
    if current_runtime and (v == "." or not v.startswith(("/", "~", "$"))):
        return True
    return False


def dangerous_chmod(words: list[str], current_runtime: bool, runtime_re: re.Pattern[str]) -> str | None:
    args = words[1:]
    i = 0
    while i < len(args) and args[i].startswith("-") and args[i] not in {"-w", "+w"}:
        i += 1
    if i >= len(args):
        return None
    mode = args[i]
    targets = args[i + 1 :]
    symbolic_add_write = bool(re.search(r"(^|,)[ugoa]*\+[^,]*w", mode)) or mode == "+w"
    octal_add_write = False
    if re.fullmatch(r"[0-7]{1,4}", mode):
        digits = mode[-4:] if len(mode) == 4 else mode
        owner = digits[1] if len(digits) == 4 else digits[0]
        octal_add_write = (int(owner, 8) & 0o2) != 0
    if not (symbolic_add_write or octal_add_write):
        return None
    if current_runtime or any(runtime_path_match(t, runtime_re, current_runtime=current_runtime) for t in targets):
        return "chmod-add-write" if symbolic_add_write else "chmod-octal-owner-write"
    return None


def dangerous_chflags(words: list[str], current_runtime: bool, runtime_re: re.Pattern[str]) -> str | None:
    args = words[1:]
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 1
    if i >= len(args):
        return None
    flags = args[i]
    targets = args[i + 1 :]
    if not re.search(r"(^|,)no(uchg|uchange|schg|schange)(,|$)", flags):
        return None
    if current_runtime or any(runtime_path_match(t, runtime_re, current_runtime=current_runtime) for t in targets):
        return "chflags-clear-immutable"
    return None


def helper_index(words: list[str], helper_names: set[str]) -> int | None:
    if not words:
        return None
    exe = words[0].split("/")[-1]
    if exe in helper_names:
        return 0
    if exe in {"bash", "sh", "zsh"}:
        i = 1
        while i < len(words) and words[i].startswith("-"):
            i += 1
        if i < len(words) and words[i].split("/")[-1] in helper_names:
            return i
    return None


def dangerous_helper(
    words: list[str], current_runtime: bool, runtime_re: re.Pattern[str], matcher: dict[str, Any]
) -> str | None:
    helper_names = {str(x) for x in matcher.get("helper_names", []) if isinstance(x, str)}
    if not helper_names:
        return None
    idx = helper_index(words, helper_names)
    action = matcher.get("helper_unlock_action", "unlock")
    if idx is None or idx + 1 >= len(words) or words[idx + 1] != action:
        return None
    raw_targets = words[idx + 2 :]
    allow_flags = {str(x) for x in matcher.get("helper_allow_flags", []) if isinstance(x, str)}
    if allow_flags and any(word in allow_flags for word in raw_targets):
        return None
    targets = [word for word in raw_targets if word != "--" and not word.startswith("--")]
    if not targets and bool(matcher.get("helper_default_targets_runtime", False)):
        return "runtime-write-protect-unlock"
    if current_runtime or any(runtime_path_match(t, runtime_re, current_runtime=current_runtime) for t in targets):
        return "runtime-write-protect-unlock"
    return None


def runtime_unlock_match(command: str, cwd: str, matcher: dict[str, Any]) -> str | None:
    escape_env = str(matcher.get("escape_env", ""))
    escape_values = {str(x).lower() for x in matcher.get("escape_values", []) if isinstance(x, str)}
    if escape_env and os.environ.get(escape_env, "").lower() in escape_values:
        _log(f"ALLOW runtime-unlock via env escape {escape_env}")
        return None

    try:
        runtime_re = re.compile(str(matcher.get("runtime_path_regex", r"(?i)runtimes/token-os/live")))
        segments = split_segments(shell_words(command))
    except Exception:
        return None

    commands = matcher.get("commands") if isinstance(matcher.get("commands"), dict) else {}
    current_runtime = runtime_path_match(cwd, runtime_re)
    for raw in segments:
        seg, escaped = strip_common_prefixes(raw, escape_env=escape_env, escape_values=escape_values)
        if escaped or not seg:
            if escaped:
                _log(f"ALLOW runtime-unlock via inline escape {escape_env}")
            continue
        name = seg[0].split("/")[-1]
        if name == "cd":
            current_runtime = runtime_path_match(seg[1], runtime_re, current_runtime=current_runtime) if len(seg) > 1 else False
            continue
        verdict = None
        if name == "chmod" and commands.get("chmod", True):
            verdict = dangerous_chmod(seg, current_runtime, runtime_re)
        elif name == "chflags" and commands.get("chflags", True):
            verdict = dangerous_chflags(seg, current_runtime, runtime_re)
        elif commands.get("runtime_write_protect_helper", False):
            verdict = dangerous_helper(seg, current_runtime, runtime_re, matcher)
        if verdict:
            return verdict
    return None



BROAD_NAS_ROOTS = {"/Volumes", "/Volumes/Imperium", "/Volumes/Civic", "/mnt/imperium", "$IMPERIUM", "${IMPERIUM}"}
NAS_SEARCH_PREFIXES = {"/Volumes/Imperium", "/Volumes/Civic", "/mnt/imperium"}
SEARCH_COMMANDS = {"find", "bfs", "rg", "ugrep", "grep"}
SEARCH_OPTIONS_WITH_VALUE = {
    "-A", "-B", "-C", "-D", "-M", "-e", "-f", "-g", "-m", "-t",
    "--after-context", "--before-context", "--color", "--colors", "--context",
    "--context-separator", "--encoding", "--engine", "--field-context-separator",
    "--field-match-separator", "--glob", "--iglob", "--ignore-file", "--json-path",
    "--max-columns", "--max-count", "--max-depth", "--max-filesize", "--path-separator",
    "--pre", "--pre-glob", "--regexp", "--replace", "--sort", "--sortr", "--threads",
    "--type", "--type-add", "--type-clear", "--type-not",
}


def normalize_search_path_token(token: str) -> str:
    token = (token or "").strip().rstrip("/")
    if token.startswith("$IMPERIUM/"):
        token = "/Volumes/Imperium/" + token[len("$IMPERIUM/") :]
    elif token.startswith("${IMPERIUM}/"):
        token = "/Volumes/Imperium/" + token[len("${IMPERIUM}/") :]
    return token.rstrip("/") or token


def resolve_search_path_token(token: str, cwd: str | None) -> str:
    import posixpath

    normalized = normalize_search_path_token(token)
    if normalized in {"", "."}:
        return cwd or normalized
    if normalized == "-":
        return normalized
    if normalized.startswith("./") and cwd:
        normalized = posixpath.join(cwd, normalized[2:])
    elif not normalized.startswith("/") and cwd:
        normalized = posixpath.join(cwd, normalized)
    return posixpath.normpath(normalized).rstrip("/") or normalized


def is_broad_nas_search_root(token: str, cwd: str | None = None) -> bool:
    normalized = resolve_search_path_token(token, cwd)
    if normalized in BROAD_NAS_ROOTS:
        return True
    return any(normalized.startswith(prefix + "/") for prefix in NAS_SEARCH_PREFIXES)


def grep_is_recursive(args: list[str]) -> bool:
    for arg in args:
        if arg == "--":
            break
        if arg in {"-R", "-r", "--recursive", "--dereference-recursive"}:
            return True
        if arg.startswith("-") and not arg.startswith("--") and ("R" in arg or "r" in arg):
            return True
    return False


def search_path_operands(args: list[str]) -> list[str]:
    paths: list[str] = []
    saw_pattern = False
    idx = 0
    while idx < len(args):
        arg = args[idx]
        if arg == "--":
            idx += 1
            if idx < len(args) and not saw_pattern:
                saw_pattern = True
                idx += 1
            paths.extend(args[idx:])
            break
        if arg.startswith("--") and "=" in arg:
            idx += 1
            continue
        if arg in SEARCH_OPTIONS_WITH_VALUE:
            if arg in {"-e", "--regexp"}:
                saw_pattern = True
            idx += 2
            continue
        if arg.startswith("-"):
            idx += 1
            continue
        if not saw_pattern:
            saw_pattern = True
        else:
            paths.append(arg)
        idx += 1
    return paths


def find_path_operands(args: list[str]) -> list[str]:
    idx = 0
    while idx < len(args) and args[idx] in {"-H", "-L", "-P", "-X", "-s"}:
        idx += 1
    if idx < len(args) and args[idx] == "--":
        idx += 1
    paths: list[str] = []
    for arg in args[idx:]:
        if arg.startswith("-") or arg in {"!", "(", ")", ","}:
            break
        paths.append(arg)
    return paths


def broad_nas_search_match(command: str, cwd: str, matcher: dict[str, Any]) -> str | None:
    try:
        segments = split_segments(shell_words(command))
    except Exception:
        return None
    current_cwd: str | None = normalize_search_path_token(cwd) if cwd else None
    commands = {str(x) for x in matcher.get("commands", SEARCH_COMMANDS) if isinstance(x, str)} or SEARCH_COMMANDS
    for raw in segments:
        seg, _ = strip_common_prefixes(raw)
        if not seg:
            continue
        exe = Path(seg[0]).name
        if exe == "cd":
            target = seg[1] if len(seg) > 1 else ""
            current_cwd = resolve_search_path_token(target, current_cwd) if target and target != "-" else None
            continue
        if exe not in commands:
            continue
        args = seg[1:]
        if exe == "grep" and not grep_is_recursive(args):
            continue
        if exe in {"find", "bfs"}:
            paths = find_path_operands(args) or ["."]
            for arg in paths:
                if is_broad_nas_search_root(arg, current_cwd):
                    return f"{exe}:{arg}"
            continue
        paths = search_path_operands(args)
        if not paths and current_cwd and is_broad_nas_search_root(current_cwd):
            return f"{exe}:{current_cwd}"
        for arg in paths:
            if is_broad_nas_search_root(arg, current_cwd):
                return f"{exe}:{arg}"
    return None


def command_subcommand_match(command: str, matcher: dict[str, Any]) -> str | None:
    command_names = {str(x) for x in matcher.get("commands", []) if isinstance(x, str)}
    subcommands = {str(x) for x in matcher.get("subcommands", []) if isinstance(x, str)}
    if not command_names or not subcommands:
        return None
    try:
        segments = split_segments(shell_words(command))
    except Exception:
        return None
    for raw in segments:
        seg, _ = strip_common_prefixes(raw)
        if len(seg) < 2:
            continue
        exe = Path(seg[0]).name
        if exe not in command_names:
            continue
        idx = 1
        while idx < len(seg) and seg[idx].startswith("-"):
            # Skip common tmux global option values.
            opt = seg[idx]
            idx += 1
            if opt in {"-L", "-S", "-f"} and idx < len(seg):
                idx += 1
        if idx < len(seg) and seg[idx] in subcommands:
            return f"{exe}:{seg[idx]}"
    return None

def rule_matches(command: str, cwd: str, rule: dict[str, Any]) -> str | None:
    matcher = rule.get("matcher") if isinstance(rule.get("matcher"), dict) else {}
    matcher_type = matcher.get("type")
    if matcher_type == "command_position":
        return command_position_match(command, matcher)
    if matcher_type == "runtime_unlock":
        return runtime_unlock_match(command, cwd, matcher)
    if matcher_type == "broad_nas_search":
        return broad_nas_search_match(command, cwd, matcher)
    if matcher_type == "command_subcommand":
        return command_subcommand_match(command, matcher)
    return None


def deny_output(rule: dict[str, Any], detail: str) -> dict[str, Any]:
    deny = rule.get("deny") if isinstance(rule.get("deny"), dict) else {}
    reason = str(deny.get("reason", f"Command denied by boundary rule {rule.get('id', 'unknown')}."))
    redirect = str(deny.get("redirect", "")).strip()
    full_reason = reason if not redirect else f"{reason}\n\nWhat to do instead: {redirect}"
    if detail:
        full_reason = full_reason.replace("${match}", detail)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": full_reason,
        }
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--rule", action="append", default=[])
    args = parser.parse_args(argv)

    raw = sys.stdin.read() or "{}"
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    except Exception as exc:
        _log(f"internal parse/config error; allowing fail-open: {exc}")
        return 0

    command = _command_from_payload(payload)
    if not command:
        return 0
    cwd = _cwd_from_payload(payload)
    selected = set(args.rule)

    try:
        rules = config.get("rules", []) if isinstance(config, dict) else []
        for rule in rules:
            if not isinstance(rule, dict) or not rule.get("enabled", True):
                continue
            rule_id = str(rule.get("id", ""))
            if selected and rule_id not in selected:
                continue
            terms = [str(t) for t in rule.get("fast_path_terms", []) if isinstance(t, str)]
            if terms and not any(term in command for term in terms):
                continue
            detail = rule_matches(command, cwd, rule)
            if detail:
                _log(f"DENY {rule_id} {detail}: {command}")
                print(json.dumps(deny_output(rule, detail), ensure_ascii=False))
                return 0
    except Exception as exc:
        _log(f"internal match error; allowing fail-open: {exc}")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
