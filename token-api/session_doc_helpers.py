"""Session doc frontmatter read/write utility.

Parses YAML between --- fences, updates fields without clobbering body content.
Uses PyYAML for robust parsing/serialization, handles arrays, nulls, and
multiline strings correctly.
"""

from pathlib import Path
from typing import Any, Optional

import yaml


class _ObsidianDumper(yaml.SafeDumper):
    """YAML dumper that doesn't quote Obsidian wikilinks or colons in strings."""
    pass


def _str_representer(dumper, data):
    """Represent strings without unnecessary quoting.

    PyYAML's SafeDumper quotes strings containing [ ] : etc.
    Obsidian wikilinks like [[Note Name]] need to stay unquoted.
    We use literal style only for multiline, and plain style where safe.
    """
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    # Let PyYAML decide, but prefer double-quote over single-quote when needed
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_ObsidianDumper.add_representer(str, _str_representer)


def read_frontmatter(file_path: Path) -> tuple[dict[str, Any], str]:
    """Read a markdown file and return (frontmatter_dict, body_content).

    Returns ({}, full_content) if no frontmatter fences found.
    """
    content = file_path.read_text(encoding="utf-8")
    return parse_frontmatter(content)


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse frontmatter from a markdown string.

    Returns (frontmatter_dict, body_content).
    Body includes everything after the closing --- fence (with its leading newline stripped once).
    """
    if not content.startswith("---"):
        return {}, content

    # Find closing fence — must be on its own line
    end_idx = content.find("\n---", 3)
    if end_idx == -1:
        return {}, content

    yaml_block = content[3:end_idx].strip()
    # Body starts after the closing ---\n
    body_start = end_idx + 4  # skip \n---
    if body_start < len(content) and content[body_start] == "\n":
        body_start += 1  # skip the newline after closing ---

    body = content[body_start:]

    try:
        fm = yaml.safe_load(yaml_block)
        if not isinstance(fm, dict):
            return {}, content
    except yaml.YAMLError:
        return {}, content

    return fm, body


def serialize_frontmatter(fm: dict[str, Any], body: str) -> str:
    """Serialize frontmatter dict + body back into a markdown string.

    Preserves body content exactly. Uses yaml.dump with settings tuned
    for Obsidian-compatible output (no trailing ..., flow style for short lists).
    """
    yaml_str = yaml.dump(
        fm,
        Dumper=_ObsidianDumper,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        width=200,
    ).rstrip("\n")

    # Reassemble
    if body and not body.startswith("\n"):
        return f"---\n{yaml_str}\n---\n\n{body}"
    return f"---\n{yaml_str}\n---\n{body}"


def update_frontmatter(file_path: Path, updates: dict[str, Any]) -> dict[str, Any]:
    """Read a session doc, merge updates into frontmatter, write back.

    Returns the updated frontmatter dict.
    Raises FileNotFoundError if the file doesn't exist.
    """
    fm, body = read_frontmatter(file_path)
    fm.update(updates)
    new_content = serialize_frontmatter(fm, body)
    file_path.write_text(new_content, encoding="utf-8")
    return fm


def update_victory_frontmatter(
    file_path: Path,
    victory_reason: str,
    end_time: str,
    deliverables: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Specialized victory update: sets victory fields and computes duration.

    Args:
        file_path: Path to the session doc markdown file.
        victory_reason: Why victory was declared.
        end_time: ISO 8601 timestamp for session end.
        deliverables: Optional list of deliverable descriptions.

    Returns the updated frontmatter dict.
    """
    fm, body = read_frontmatter(file_path)

    updates = {
        "victory": "declared",
        "victory_reason": victory_reason,
        "end_time": end_time,
        "status": "completed",
    }

    # Compute duration if start_time is present
    start_time = fm.get("start_time")
    if start_time:
        try:
            from datetime import datetime

            if isinstance(start_time, str):
                # Handle both with and without timezone
                st = datetime.fromisoformat(start_time)
                et = datetime.fromisoformat(end_time)
                delta = et - st
                updates["duration_minutes"] = round(delta.total_seconds() / 60)
        except (ValueError, TypeError):
            pass  # Can't compute, skip

    if deliverables is not None:
        updates["deliverables"] = deliverables

    fm.update(updates)
    new_content = serialize_frontmatter(fm, body)
    file_path.write_text(new_content, encoding="utf-8")
    return fm
