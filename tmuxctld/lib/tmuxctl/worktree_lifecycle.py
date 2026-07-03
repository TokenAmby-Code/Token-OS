"""Deferred worktree cleanup owned by the WrapperEnd lifecycle.

Worktree teardown is a shutdown request.  A merge command running from inside a
linked worktree must never remove its own current directory; doing so turns a
successful merge into a `getcwd(ENOENT)` crash for the still-running agent.

The only safe owner is WrapperEnd: by the time this module runs the wrapped
agent process is already gone.  Even then this code removes the *worktree only*
(`git worktree remove` + `prune`) and preserves whenever merge state or local
cleanliness is uncertain.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any


def cleanup_worktree_on_wrapper_end(
    worktree: str | os.PathLike[str] | None,
    *,
    instance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Remove a merged linked worktree after its wrapper has ended.

    Safety rules:
    - never delete a missing, non-directory, or primary checkout;
    - never delete dirty/untracked work;
    - delete only when the instance/PR state says the branch is merged, or when
      the branch tip is locally proven to be merged into the default branch;
    - on any uncertainty, preserve.
    """

    path = Path(str(worktree or "")).expanduser()
    result: dict[str, Any] = {"worktree": str(path), "status": "preserved"}
    if not str(worktree or "").strip():
        return {**result, "reason": "no_worktree"}
    if not path.exists():
        return {**result, "status": "already_missing", "reason": "missing"}
    if not path.is_dir():
        return {**result, "reason": "not_directory"}
    if not (path / ".git").is_file():
        return {**result, "reason": "not_linked_worktree"}

    branch = _git(path, "branch", "--show-current")
    if not branch:
        return {**result, "reason": "detached_head"}
    result["branch"] = branch

    status_proc = _run_git(path, "status", "--porcelain=v1")
    if status_proc.returncode != 0:
        return {
            **result,
            "reason": "status_failed",
            "stderr": status_proc.stderr.strip(),
        }
    dirty = _meaningful_dirty_entries(status_proc.stdout.strip())
    if dirty:
        return {**result, "reason": "dirty_worktree"}

    merged, merged_reason = _branch_is_merged(path, instance=instance)
    result["merge_reason"] = merged_reason
    if not merged:
        return {**result, "reason": "branch_not_merged"}

    common_dir = _git(path, "rev-parse", "--git-common-dir")
    if not common_dir:
        return {**result, "reason": "no_git_common_dir"}
    common = Path(common_dir)
    if not common.is_absolute():
        common = (path / common).resolve()

    remove = _run_git_gitdir(common, "worktree", "remove", "--force", str(path))
    if remove.returncode != 0:
        return {
            **result,
            "status": "preserved",
            "reason": "remove_failed",
            "stderr": remove.stderr.strip(),
        }
    prune = _run_git_gitdir(common, "worktree", "prune")
    # The worktree removal proved the branch merged (via `merged_reason`); prune
    # its now-merged branch ref too so refs don't accumulate in the bare after
    # teardown.  Gated on the same merged ground truth — only reachable here —
    # and never on the fail-safe preserve paths above.
    branch_prune = _prune_merged_branch_ref(common, branch)
    return {
        **result,
        "status": "removed",
        "reason": "merged",
        "prune_rc": prune.returncode,
        "prune_stderr": prune.stderr.strip(),
        **branch_prune,
    }


def _prune_merged_branch_ref(common: Path, branch: str) -> dict[str, Any]:
    """Delete a proven-merged worktree's branch ref from the bare.

    Best effort: a failed prune never turns a successful worktree removal into a
    failure.  Refuses to delete a default branch (main/master) defensively — a
    merged feature branch is never one of those, and the removal proof authorizes
    force-deletion (`-D`) regardless of local ancestry, which is exactly what a
    squash-merge needs.
    """

    if not branch or branch in {"main", "master"}:
        return {"branch_prune": "skipped"}
    proc = _run_git_gitdir(common, "branch", "-D", branch)
    if proc.returncode != 0:
        return {"branch_prune": "failed", "branch_prune_stderr": proc.stderr.strip()}
    return {"branch_prune": "pruned"}


def _branch_is_merged(path: Path, *, instance: dict[str, Any] | None) -> tuple[bool, str]:
    state = str((instance or {}).get("pr_state") or "").strip().lower()
    if state == "merged":
        return True, "instance_pr_state_merged"
    if state in {"open", "closed"}:
        return False, f"instance_pr_state_{state}"

    branch = _git(path, "branch", "--show-current")
    if not branch:
        return False, "detached_head"

    for base in _default_branch_candidates(path):
        if _run_git(path, "merge-base", "--is-ancestor", "HEAD", base).returncode == 0:
            return True, f"head_ancestor_of_{base}"
    return False, "unproven"


def _default_branch_candidates(path: Path) -> list[str]:
    candidates: list[str] = []
    remote_head = _git(path, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if remote_head:
        candidates.append(remote_head)
    for ref in ("origin/main", "main", "origin/master", "master"):
        if ref not in candidates:
            candidates.append(ref)
    return [ref for ref in candidates if _run_git(path, "rev-parse", "--verify", ref).returncode == 0]


def _meaningful_dirty_entries(status: str) -> list[str]:
    """Return dirty status lines that represent user work.

    ``worktree-setup`` creates ``.worktree.env`` as per-worktree runtime metadata;
    it is intentionally untracked and not user work.  Everything else — tracked
    modifications, staged changes, and unknown untracked files — is preserved.
    """

    meaningful: list[str] = []
    for line in status.splitlines():
        if line == "?? .worktree.env":
            continue
        meaningful.append(line)
    return meaningful


def _git(path: Path, *args: str) -> str:
    proc = _run_git(path, *args)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _run_git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _run_git_gitdir(git_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "--git-dir", str(git_dir), *args],
        capture_output=True,
        text=True,
        check=False,
    )
