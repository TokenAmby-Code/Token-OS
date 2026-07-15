"""Assemble the Obsidian-native build review note.

Prefers transclusion over copy: per-thread we emit ``![[doc#Key Files]]``, the
Sanguinius diagram companion sections, and core-change blocks — so the rich
content (HTML/SVG/Mermaid) renders from its source doc inside the vault. The
headings match ``Templates/Periodic/Build Note.md`` exactly.
"""

from __future__ import annotations

TOP_FILES_CAP = 30


def _fmt_date(iso: str | None) -> str:
    """Trim an ISO timestamp to YYYY-MM-DD for display."""
    if not iso:
        return "?"
    return iso[:10]


def _frontmatter(
    date: str,
    base_sha: str,
    head_sha: str,
    remote_main_sha: str,
    generated_at: str,
    n_threads: int,
    n_opted_out: int,
) -> str:
    base = base_sha or ""
    return (
        "---\n"
        f"title: {date}\n"
        f"date: {date}\n"
        "type: build\n"
        f'daily_note: "[[Daily/{date}]]"\n'
        f"covered_base_sha: {base}\n"
        f"head_sha: {head_sha}\n"
        "head_source: checkout\n"
        f"remote_main_sha: {remote_main_sha}\n"
        "status: active\n"
        f"generated_at: {generated_at}\n"
        f"thread_count: {n_threads}\n"
        f"opted_out_count: {n_opted_out}\n"
        "---\n"
    )


def _pr_line(pr: dict, branch: str) -> str:
    num = pr["number"]
    line = f"- **#{num}** {pr['title']}"
    if branch:
        line += f" (`{branch}`)"
    line += f" — merged {_fmt_date(pr.get('mergedAt'))}"
    if pr.get("url"):
        line += f" · [link]({pr['url']})"
    return line


def _bundle_section(
    threads: list[dict],
    commits: list[tuple],
    base_sha: str,
    base_date: str,
    head_sha: str,
    remote_main_sha: str,
    ref: str,
) -> str:
    lines = ["## Bundle", ""]
    short_base = base_sha[:9] if base_sha else "(bootstrap: build-date calendar day)"
    lines.append(
        f"*Base `{short_base}` (merged since {base_date or '?'}) → "
        f"checkout `{head_sha[:9] or '?'}` on `{ref}`.*"
    )
    if remote_main_sha and remote_main_sha != head_sha:
        lines.append("")
        lines.append(
            f"> ⚠️ Checkout `{ref}` (`{head_sha[:9]}`) ≠ remote `{ref}` "
            f"(`{remote_main_sha[:9]}`) — this note covers the checkout, not deployed truth."
        )
    lines.append("")

    by_pr: dict[int, list[tuple]] = {}
    unattributed: list[tuple] = []
    for sha, subject, pr_num in commits:
        if pr_num is None:
            unattributed.append((sha, subject))
        else:
            by_pr.setdefault(pr_num, []).append((sha, subject))

    if not threads:
        lines.append("- *No merged PRs resolved for this window.*")
    covered_nums: set[int] = set()
    for thread in threads:
        pr = thread["pr"]
        num = pr["number"]
        covered_nums.add(num)
        lines.append(_pr_line(pr, thread["branch"]))
        for sha, subject in by_pr.get(num, []):
            lines.append(f"  - `{sha}` {subject}")

    # Belt and braces: a PR-numbered commit is never silently dropped, even if
    # it somehow escaped the attribution union above.
    leftovers = {num: pairs for num, pairs in by_pr.items() if num not in covered_nums}
    if leftovers:
        lines.append("")
        lines.append("**PR-attributed commits without a thread entry:**")
        for num in sorted(leftovers, reverse=True):
            for sha, subject in leftovers[num]:
                lines.append(f"- `#{num}` `{sha}` {subject}")

    if unattributed:
        lines.append("")
        lines.append("**Unattributed commits** *(no PR number in subject):*")
        for sha, subject in unattributed:
            lines.append(f"- `{sha}` {subject}")

    return "\n".join(lines)


def _thread_block(thread: dict) -> str:
    pr = thread["pr"]
    num = pr["number"]
    meta_parts = []
    if thread["branch"]:
        meta_parts.append(f"`{thread['branch']}`")
    meta_parts.append(f"merged {_fmt_date(pr.get('mergedAt'))}")
    if pr.get("url"):
        meta_parts.append(f"[PR #{num}]({pr['url']})")
    lines = [f"### #{num} · {pr['title']}", " · ".join(meta_parts)]
    stem = thread.get("stem")
    if not stem:
        lines.append("")
        branch = thread["branch"] or "(branch unknown — attributed from git commit)"
        lines.append(f"*No session doc resolved for branch `{branch}`.*")
        return "\n".join(lines)

    title = thread.get("title") or stem
    lines.append(f"Session doc: [[{stem}|{title}]]")
    lines.append("")

    emitted = False
    if thread.get("key_files_heading"):
        lines.append(f"![[{stem}#{thread['key_files_heading']}]]")
        emitted = True
    for diag_stem, sections in thread.get("diagrams", []):
        for section in sections:
            lines.append(f"![[{diag_stem}#{section}]]")
            emitted = True
    for heading in thread.get("core_change_headings", []):
        lines.append(f"![[{stem}#{heading}]]")
        emitted = True
    if not emitted:
        lines.append(
            f"*No transcludable sections found in [[{stem}]] "
            "(no Key Files / diagram / Changes Made headings).*"
        )
    return "\n".join(lines)


def _threads_section(threads: list[dict]) -> str:
    lines = ["## Threads", ""]
    if not threads:
        lines.append("- *No included threads.*")
        return "\n".join(lines)
    blocks = [_thread_block(thread) for thread in threads]
    return "## Threads\n\n" + "\n\n".join(blocks)


def _top_files_section(top_files: list[tuple[str, str]]) -> str:
    lines = ["## Top files to read", ""]
    if not top_files:
        lines.append("- *No files resolved (no Key Files sections, no diff churn).*")
        return "\n".join(lines)
    shown = top_files[:TOP_FILES_CAP]
    for idx, (path, reason) in enumerate(shown, start=1):
        lines.append(f"{idx}. `{path}` — {reason}")
    if len(top_files) > TOP_FILES_CAP:
        lines.append("")
        lines.append(
            f"*+{len(top_files) - TOP_FILES_CAP} more files not shown (capped at {TOP_FILES_CAP}).*"
        )
    return "\n".join(lines)


def _open_pr_section(open_prs: list[dict]) -> str:
    lines = ["## Open-PR roll-call", ""]
    if not open_prs:
        lines.append("- *No open PRs against `main`. Nothing rolling over.*")
        return "\n".join(lines)
    lines.append("*Each must merge or opt out before it rolls over.*")
    lines.append("")
    for pr in open_prs:
        lines.append(
            f"- [ ] **#{pr['number']}** {pr['title']} (`{pr['headRefName']}`) — "
            f"[link]({pr['url']}) · updated {_fmt_date(pr.get('updatedAt'))}"
        )
    return "\n".join(lines)


def _opted_out_section(opted_out: list[dict]) -> str:
    lines = ["## Rolled over (opted out)", ""]
    if not opted_out:
        lines.append("- *Nothing opted out this cycle.*")
        return "\n".join(lines)
    lines.append(
        "*`daily_build_skip: true` on the session doc — excluded from "
        "the review, listed so nothing disappears silently.*"
    )
    lines.append("")
    for thread in opted_out:
        pr = thread["pr"]
        stem = thread.get("stem")
        label = f"[[{stem}|{thread.get('title') or stem}]]" if stem else "*(no session doc)*"
        lines.append(f"- {label} — #{pr['number']} {pr['title']} (`{thread['branch']}`)")
    return "\n".join(lines)


def generate(
    *,
    date: str,
    base_sha: str,
    base_date: str,
    head_sha: str,
    remote_main_sha: str,
    ref: str,
    threads: list[dict],
    opted_out: list[dict],
    commits: list[tuple],
    top_files: list[tuple[str, str]],
    open_prs: list[dict],
    generated_at: str,
) -> str:
    """Build the full note markdown (frontmatter + the five stable sections)."""
    header = (
        f"# Build — {date}\n\n"
        f"Daily human-review backstop for the day's `{ref}` activity. "
        f"Satellite of [[Daily/{date}]]. Generated by `daily-build` — "
        "re-running the same day regenerates this note in place."
    )
    parts = [
        _frontmatter(
            date, base_sha, head_sha, remote_main_sha, generated_at, len(threads), len(opted_out)
        ),
        header,
        _bundle_section(threads, commits, base_sha, base_date, head_sha, remote_main_sha, ref),
        _threads_section(threads),
        _top_files_section(top_files),
        _open_pr_section(open_prs),
        _opted_out_section(opted_out),
    ]
    return parts[0] + "\n" + "\n\n---\n\n".join(parts[1:]) + "\n"
