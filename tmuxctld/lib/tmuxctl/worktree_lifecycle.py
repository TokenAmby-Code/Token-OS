"""Worktree teardown owned by tmuxctld — the single sanctioned executor.

Worktree teardown is a shutdown request.  A merge command running from inside a
linked worktree must never remove its own current directory; doing so turns a
successful merge into a `getcwd(ENOENT)` crash for the still-running agent.

The safe owner is tmuxctld.  On the WrapperEnd path the wrapped agent process is
already gone, which is *structural* getcwd immunity; every other entrypoint runs
the same universal sanitization gate here plus an explicit cwd-equality guard, so
no teardown can purge the directory the calling process still stands in.

:func:`teardown_worktree` is the one entrypoint.  It preserves whenever merge
state or local cleanliness is uncertain, and only after the merge is *proven*
does it delete the worktree, prune the local branch ref, and — gated on
no-open-PR — delete the remote branch ref too.  ``token-api`` stays the
merge-proof authority (``pr_state``); tmuxctld consults it and executes.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def teardown_worktree(
    worktree: str | os.PathLike[str] | None,
    *,
    instance: dict[str, Any] | None = None,
    delete_remote: bool = True,
) -> dict[str, Any]:
    """Tear down a merged linked worktree — the single teardown entrypoint.

    Callable by the WrapperEnd handler and the on-demand daemon route alike.

    Universal sanitization gate (never destroys unmerged or uncommitted work):
    - never delete a missing, non-directory, or primary checkout;
    - never delete the calling process's own cwd (or an ancestor of it);
    - never delete dirty/untracked work;
    - delete only when the instance/PR state says the branch is merged, or when
      the branch tip is locally proven merged into the default branch;
    - on any uncertainty, preserve.

    Once the merge is proven the local worktree and its branch ref are removed;
    the remote branch ref is additionally deleted only when ``delete_remote`` is
    set AND no open PR is possible (``pr_state == "merged"`` — a merged PR is not
    open).  Deleting the head branch of an open PR would orphan/close that PR, so
    an unproven no-open-PR state preserves the remote ref.
    """

    gate = _sanitize(worktree, instance=instance)
    if not gate.proceed:
        return gate.result

    path, branch, common = gate.path, gate.branch, gate.common
    result = gate.result

    remove = _run_git_gitdir(common, "worktree", "remove", "--force", str(path))
    if remove.returncode != 0:
        return {
            **result,
            "status": "preserved",
            "reason": "remove_failed",
            "stderr": remove.stderr.strip(),
        }
    prune = _run_git_gitdir(common, "worktree", "prune")
    # The worktree removal proved the branch merged (via `merge_reason`); prune
    # its now-merged branch ref too so refs don't accumulate in the bare after
    # teardown.  Gated on the same merged ground truth — only reachable here —
    # and never on the fail-safe preserve paths above.
    branch_prune = _prune_merged_branch_ref(common, branch)
    remote_prune: dict[str, Any] = {"remote_prune": "disabled"}
    if delete_remote:
        remote_prune = _delete_remote_ref_if_safe(common, branch, instance=instance)
    return {
        **result,
        "status": "removed",
        "reason": "merged",
        "prune_rc": prune.returncode,
        "prune_stderr": prune.stderr.strip(),
        **branch_prune,
        **remote_prune,
    }


def cleanup_worktree_on_wrapper_end(
    worktree: str | os.PathLike[str] | None,
    *,
    instance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """WrapperEnd-facing alias for :func:`teardown_worktree`.

    Kept as the lifecycle handler's stable name; the WrapperEnd path is
    post-process-death, so it clears the full local+remote teardown.
    """

    return teardown_worktree(worktree, instance=instance, delete_remote=True)


@dataclass
class _GateOutcome:
    """Result of the universal sanitization gate.

    ``proceed`` is True only when teardown is authorized; ``path``/``branch``/
    ``common`` are then populated.  Otherwise ``result`` carries the preserve
    envelope the caller returns verbatim.
    """

    proceed: bool
    result: dict[str, Any]
    path: Path | None = None
    branch: str = ""
    common: Path | None = None


def _sanitize(
    worktree: str | os.PathLike[str] | None,
    *,
    instance: dict[str, Any] | None,
) -> _GateOutcome:
    """Run the universal preserve-if-uncertain gate for every teardown path."""

    path = Path(str(worktree or "")).expanduser()
    result: dict[str, Any] = {"worktree": str(path), "status": "preserved"}
    if not str(worktree or "").strip():
        return _GateOutcome(False, {**result, "reason": "no_worktree"})
    if not path.exists():
        return _GateOutcome(
            False, {**result, "status": "already_missing", "reason": "missing"}
        )
    if not path.is_dir():
        return _GateOutcome(False, {**result, "reason": "not_directory"})
    if not (path / ".git").is_file():
        return _GateOutcome(False, {**result, "reason": "not_linked_worktree"})
    # getcwd immunity for every non-WrapperEnd caller: never remove the directory
    # the running process still stands in (or an ancestor of it).  On WrapperEnd
    # the wrapped process is already gone, so this never trips there.
    if _would_purge_cwd(path):
        return _GateOutcome(False, {**result, "reason": "would_remove_cwd"})

    branch = _git(path, "branch", "--show-current")
    if not branch:
        return _GateOutcome(False, {**result, "reason": "detached_head"})
    result["branch"] = branch

    status_proc = _run_git(path, "status", "--porcelain=v1")
    if status_proc.returncode != 0:
        return _GateOutcome(
            False,
            {**result, "reason": "status_failed", "stderr": status_proc.stderr.strip()},
        )
    if _meaningful_dirty_entries(status_proc.stdout.strip()):
        return _GateOutcome(False, {**result, "reason": "dirty_worktree"})

    merged, merged_reason = _branch_is_merged(path, instance=instance)
    result["merge_reason"] = merged_reason
    if not merged:
        return _GateOutcome(False, {**result, "reason": "branch_not_merged"})

    common_dir = _git(path, "rev-parse", "--git-common-dir")
    if not common_dir:
        return _GateOutcome(False, {**result, "reason": "no_git_common_dir"})
    common = Path(common_dir)
    if not common.is_absolute():
        common = (path / common).resolve()

    return _GateOutcome(True, result, path=path, branch=branch, common=common)


def _would_purge_cwd(path: Path) -> bool:
    """True if removing ``path`` would pull the process's own cwd out from under it."""

    try:
        cwd = Path(os.getcwd()).resolve()
    except OSError:
        # getcwd already gone (the exact crash we guard against elsewhere) — the
        # safest reading is that we may be standing inside a doomed dir; preserve.
        return True
    target = path.resolve()
    return cwd == target or target in cwd.parents


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


def _delete_remote_ref_if_safe(
    common: Path, branch: str, *, instance: dict[str, Any] | None
) -> dict[str, Any]:
    """Delete a proven-merged branch's ref on the remote — gated on no-open-PR.

    Load-bearing gate: deleting the head branch of an *open* PR orphans/closes
    that PR on GitHub.  Merge is already proven by the caller; here we require
    additional proof that no PR is open on this head.  ``token-api`` is that
    authority: ``pr_state == "merged"`` means the PR closed on merge (no open
    PR), so the remote ref is safe to reap.  A merge proven only by local
    ancestry (no authoritative ``pr_state``) cannot rule out an open PR, so the
    remote ref is preserved.  Best effort: a failed push never fails teardown.
    """

    if not branch or branch in {"main", "master"}:
        return {"remote_prune": "skipped", "remote_prune_reason": "default_branch"}
    ok, reason = _no_open_pr(instance)
    if not ok:
        return {"remote_prune": "preserved", "remote_prune_reason": reason}
    remote = _remote_name(common)
    if not remote:
        return {"remote_prune": "skipped", "remote_prune_reason": "no_remote"}
    proc = _run_git_gitdir(common, "push", remote, "--delete", branch)
    if proc.returncode != 0:
        return {
            "remote_prune": "failed",
            "remote_prune_reason": reason,
            "remote": remote,
            "remote_prune_stderr": proc.stderr.strip(),
        }
    return {"remote_prune": "deleted", "remote": remote, "remote_prune_reason": reason}


def _no_open_pr(instance: dict[str, Any] | None) -> tuple[bool, str]:
    """Whether it is proven that no open PR points at this head branch."""

    state = str((instance or {}).get("pr_state") or "").strip().lower()
    if state == "merged":
        return True, "pr_state_merged"
    if state == "open":
        return False, "pr_state_open"
    # Merge proven only by local ancestry / no authoritative PR state: an open PR
    # cannot be ruled out, so preserve the remote ref.
    return False, "no_open_pr_unconfirmed"


def _remote_name(common: Path) -> str:
    """Pick the bare's push remote — ``origin`` if present, else the first."""

    proc = _run_git_gitdir(common, "remote")
    remotes = proc.stdout.split() if proc.returncode == 0 else []
    if "origin" in remotes:
        return "origin"
    return remotes[0] if remotes else ""


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


# Teardown runs unattended (WrapperEnd, daemon route, cron cascade): git must
# never block on a credential/SSH prompt, and a hung remote `push` must not wedge
# the caller.  Force non-interactive auth and bound every invocation.
_GIT_ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -oBatchMode=yes"),
}
_GIT_TIMEOUT = 60.0


def _git(path: Path, *args: str) -> str:
    proc = _run_git(path, *args)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _run_git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-C", str(path), *args])


def _run_git_gitdir(git_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(["git", "--git-dir", str(git_dir), *args])


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a git command non-interactively and time-bounded.

    A timeout surfaces as a non-zero ``CompletedProcess`` (rc 124) so every
    existing ``returncode != 0`` check treats a wedged network op as a preserve /
    best-effort failure rather than hanging teardown.
    """

    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=_GIT_ENV,
            timeout=_GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            cmd,
            124,
            exc.stdout or "",
            (exc.stderr or "") + f"\ngit timed out after {_GIT_TIMEOUT:g}s",
        )
