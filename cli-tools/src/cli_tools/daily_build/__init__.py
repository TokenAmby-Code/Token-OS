"""daily-build — assemble the day's Obsidian-hosted build review note.

Reads the day's git/GitHub activity (merged PRs on `main` since the last build),
resolves each thread to its session doc, and writes an Obsidian-native review note
at ``Terra/Journal/Builds/<date>.md`` — the satellite of that day's daily note.

Pure-internal (v0): reads git + session docs, writes one vault note. No GitHub
branch surgery (that is v1, deferred).

The CLI entrypoint is ``cli_tools.daily_build.cli:main`` (see pyproject scripts).
"""
