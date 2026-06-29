#!/usr/bin/env python3
"""Classify runtime-unlock intent from a PreToolUse Bash command.

Reads command/cwd from RUNTIME_UNLOCK_GUARD_CMD and
RUNTIME_UNLOCK_GUARD_CWD. Prints an unlock reason token, or nothing when the
command is allowed/irrelevant. Best-effort parser: fail-open by printing
nothing on syntax it cannot classify.
"""

from __future__ import annotations

import os
import re
import shlex
import sys

cmd = os.environ.get("RUNTIME_UNLOCK_GUARD_CMD", "")
cwd = os.environ.get("RUNTIME_UNLOCK_GUARD_CWD", "")

ADMIN_FORCE_FLAGS = {"--force", "--admin-force"}


def is_runtime_path(value: str, *, current_runtime: bool = False) -> bool:
    if not value:
        return False
    v = value.strip().strip("\"'")
    low = v.lower()
    if (
        low in {"$token_os", "${token_os}"}
        or low.startswith("$token_os/")
        or low.startswith("${token_os}/")
    ):
        return True
    if "runtimes/token-os/live" in low:
        return True
    if current_runtime and (v == "." or not v.startswith(("/", "~", "$"))):
        return True
    return False


def shell_words(command: str) -> list[str]:
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def split_segments(words: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    cur: list[str] = []
    for word in words:
        if word in {";", "&&", "||", "|", "(", ")"}:
            if cur:
                segments.append(cur)
                cur = []
            continue
        cur.append(word)
    if cur:
        segments.append(cur)
    return segments


def is_assignment(word: str) -> bool:
    return re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word) is not None


def strip_wrappers(seg: list[str]) -> tuple[list[str], bool]:
    words = list(seg)
    allow = False
    while words and is_assignment(words[0]):
        if re.match(r"^IMPERIUM_ALLOW_RUNTIME_WRITE=(1|true)($|\b)", words[0], re.I):
            allow = True
        words.pop(0)
    while words and words[0] in {"command", "builtin"}:
        words.pop(0)
        while words and is_assignment(words[0]):
            if re.match(r"^IMPERIUM_ALLOW_RUNTIME_WRITE=(1|true)($|\b)", words[0], re.I):
                allow = True
            words.pop(0)
    if words and words[0] == "env":
        words.pop(0)
        while words and (is_assignment(words[0]) or words[0].startswith("-")):
            if re.match(r"^IMPERIUM_ALLOW_RUNTIME_WRITE=(1|true)($|\b)", words[0], re.I):
                allow = True
            words.pop(0)
    if words and words[0] == "sudo":
        words.pop(0)
        while words and words[0].startswith("-"):
            # Best-effort skip sudo option + one common option argument.
            opt = words.pop(0)
            if opt in {"-u", "-g", "-h", "-p"} and words:
                words.pop(0)
        while words and is_assignment(words[0]):
            words.pop(0)
    return words, allow


def dangerous_chmod(words: list[str], current_runtime: bool) -> str | None:
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
    if current_runtime or any(is_runtime_path(t, current_runtime=current_runtime) for t in targets):
        return "chmod-add-write" if symbolic_add_write else "chmod-octal-owner-write"
    return None


def dangerous_chflags(words: list[str], current_runtime: bool) -> str | None:
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
    if current_runtime or any(is_runtime_path(t, current_runtime=current_runtime) for t in targets):
        return "chflags-clear-immutable"
    return None


def helper_index(words: list[str]) -> int | None:
    if not words:
        return None
    exe = words[0].split("/")[-1]
    if exe == "runtime-write-protect.sh":
        return 0
    if exe in {"bash", "sh", "zsh"}:
        i = 1
        while i < len(words) and words[i].startswith("-"):
            i += 1
        if i < len(words) and words[i].split("/")[-1] == "runtime-write-protect.sh":
            return i
    return None


def dangerous_helper(words: list[str], current_runtime: bool) -> str | None:
    idx = helper_index(words)
    if idx is None or idx + 1 >= len(words):
        return None
    action = words[idx + 1]
    if action != "unlock":
        return None
    raw_targets = words[idx + 2 :]
    if any(word in ADMIN_FORCE_FLAGS for word in raw_targets):
        return None
    targets = [word for word in raw_targets if word not in {"--"} and not word.startswith("--")]
    # No explicit root means the helper unlocks its built-in runtime roots.
    if not targets:
        return "runtime-write-protect-unlock"
    if current_runtime or any(is_runtime_path(t, current_runtime=current_runtime) for t in targets):
        return "runtime-write-protect-unlock"
    return None


def main() -> int:
    try:
        words = shell_words(cmd)
    except Exception:
        return 0

    current_runtime = is_runtime_path(cwd)
    for raw_seg in split_segments(words):
        seg, allow = strip_wrappers(raw_seg)
        if allow or not seg:
            continue
        name = seg[0].split("/")[-1]
        if name == "cd":
            # `cd` with no target goes home (not runtime for this guard).
            current_runtime = (
                is_runtime_path(seg[1], current_runtime=current_runtime) if len(seg) > 1 else False
            )
            continue
        verdict = None
        if name == "chmod":
            verdict = dangerous_chmod(seg, current_runtime)
        elif name == "chflags":
            verdict = dangerous_chflags(seg, current_runtime)
        else:
            verdict = dangerous_helper(seg, current_runtime)
        if verdict:
            print(verdict)
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
