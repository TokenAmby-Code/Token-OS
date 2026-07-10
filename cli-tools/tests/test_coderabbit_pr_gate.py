from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github" / "scripts" / "coderabbit-pr-gate.sh"


def write_fake_gh(bin_dir: Path) -> None:
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
args="$*"
if [[ "$args" == *"commits/head-sha/status"* ]]; then
  # GitHub's combined status endpoint can be empty when CodeRabbit reports as a check run.
  exit 0
fi
if [[ "$args" == *"commits/head-sha/check-runs"* ]]; then
  conclusion="${FAKE_CR_CHECK_CONCLUSION:-success}"
  summary="${FAKE_CR_CHECK_SUMMARY:-No actionable comments were generated.}"
  printf '%s\t%s\t%s\t%s\t%s\n' 'CodeRabbit / Review' 'completed' "$conclusion" "$summary" 'https://app.coderabbit.ai/change-stack/test'
  exit 0
fi
if [[ "$args" == *"pulls/17/reviews"* ]]; then
  printf '%s\t%s\t%s\t%s\n' '123' "${FAKE_CR_REVIEW_STATE:-APPROVED}" '2026-07-10T02:23:44Z' 'https://github.test/review/123'
  exit 0
fi
printf 'unexpected gh args: %s\n' "$args" >&2
exit 2
"""
    )
    gh.chmod(0o755)


def test_coderabbit_gate_accepts_successful_check_run_without_commit_status(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_fake_gh(bin_dir)
    script = tmp_path / "coderabbit-pr-gate.sh"
    shutil.copy2(SCRIPT, script)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "GH_TOKEN": "token",
            "REPO": "owner/repo",
            "PR_NUMBER": "17",
            "SHA": "head-sha",
            "CODERABBIT_GATE_TIMEOUT_SECONDS": "1",
            "CODERABBIT_GATE_POLL_INTERVAL_SECONDS": "1",
        }
    )

    result = subprocess.run(
        ["bash", str(script)], env=env, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert (
        "CodeRabbit check run: name=CodeRabbit / Review status=completed conclusion=success"
        in result.stdout
    )
    assert "Latest CodeRabbit review for current head" in result.stdout


def test_coderabbit_gate_rejects_skipped_check_run(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_fake_gh(bin_dir)
    script = tmp_path / "coderabbit-pr-gate.sh"
    shutil.copy2(SCRIPT, script)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "GH_TOKEN": "token",
            "REPO": "owner/repo",
            "PR_NUMBER": "17",
            "SHA": "head-sha",
            "CODERABBIT_GATE_TIMEOUT_SECONDS": "1",
            "CODERABBIT_GATE_POLL_INTERVAL_SECONDS": "1",
            "FAKE_CR_CHECK_CONCLUSION": "skipped",
            "FAKE_CR_CHECK_SUMMARY": "Result: Skipped - disabled by policy",
        }
    )

    result = subprocess.run(
        ["bash", str(script)], env=env, text=True, capture_output=True, check=False
    )

    assert result.returncode != 0
    assert "CodeRabbit check run was skipped" in result.stdout


def test_coderabbit_gate_rejects_disabled_neutral_review(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_fake_gh(bin_dir)
    script = tmp_path / "coderabbit-pr-gate.sh"
    shutil.copy2(SCRIPT, script)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "GH_TOKEN": "token",
            "REPO": "owner/repo",
            "PR_NUMBER": "17",
            "SHA": "head-sha",
            "CODERABBIT_GATE_TIMEOUT_SECONDS": "1",
            "CODERABBIT_GATE_POLL_INTERVAL_SECONDS": "1",
            "FAKE_CR_CHECK_CONCLUSION": "neutral",
            "FAKE_CR_CHECK_SUMMARY": "Review skipped: free tier disabled",
        }
    )

    result = subprocess.run(
        ["bash", str(script)], env=env, text=True, capture_output=True, check=False
    )

    assert result.returncode != 0
    assert "CodeRabbit reported a skipped/disabled review" in result.stdout
