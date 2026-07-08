"""token-restart CD-sync watches the shared contracts package.

Terminus Stage 2 extracted the ops-cockpit TypeScript contracts into
``token-api/web/contracts/`` (`@token-os/contracts`, Zod schemas). The cockpit
imports from that package via a ``file:`` dep, so a contracts-only change must
rebuild the committed ``token-api/ui/ops`` bundle on deploy — the
``ops_refresh_needed_for_paths`` case arm in ``cli-tools/bin/token-restart``
must match ``token-api/web/contracts/*`` alongside the existing ops globs.

Graduated from the bounty lane with the contracts-extraction PR (Stage 2 PR B).
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOKEN_RESTART = ROOT / "bin" / "token-restart"


def _ops_refresh_case_arm() -> str:
    """The body of ops_refresh_needed_for_paths (read, never sourced)."""
    text = TOKEN_RESTART.read_text()
    match = re.search(r"ops_refresh_needed_for_paths\(\)\s*\{.*?\n\}", text, flags=re.DOTALL)
    assert match, "ops_refresh_needed_for_paths() not found in token-restart"
    return match.group(0)


def test_ops_refresh_glob_covers_contracts_package() -> None:
    body = _ops_refresh_case_arm()
    assert "token-api/web/contracts/*" in body, (
        "regression: ops bundle refresh must trigger on contracts changes "
        "(token-api/web/contracts/* missing from the case arm)"
    )
