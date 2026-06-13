"""Guard: no runtime path reads the DROPPED `claude_instances` table.

`claude_instances` was extracted to archive.db and dropped mid-session. The only
legitimate remaining references live in db_schema.py's extract/archive/restore
machinery (and one-time legacy rebuild). Any OTHER runtime module touching it is
a defect — a GT/keepalive reader bound to a table that no longer exists would
silently fail closed or resurrect a ghost. This test pins canonical reads at the
`instances` table.
"""

import re
from pathlib import Path

import pytest

TOKEN_API = Path(__file__).resolve().parent.parent

# Runtime modules that drive GT dispatch / keepalive / instance resolution. None
# of these may read the dropped legacy table — they must target `instances`.
RUNTIME_MODULES = [
    "main.py",
    "routes/hooks.py",
    "instance_registry.py",
    "instance_mutation.py",
    "personas.py",
    "morning_session.py",
    "shared.py",
]


@pytest.mark.parametrize("rel_path", RUNTIME_MODULES)
def test_runtime_module_does_not_read_claude_instances(rel_path):
    src = (TOKEN_API / rel_path).read_text(encoding="utf-8")
    # `claude_instances` (the dropped table) must not appear. `legacy_instances`
    # (the test-only compatibility view / unrelated identifiers) is fine, so match
    # the exact token with a word boundary that does not swallow a leading word char.
    offenders = [
        line for line in src.splitlines() if re.search(r"(?<![\w])claude_instances\b", line)
    ]
    assert offenders == [], (
        f"{rel_path} references the dropped claude_instances table:\n" + "\n".join(offenders)
    )
