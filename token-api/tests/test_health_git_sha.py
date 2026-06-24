"""/health self-reports the git SHA the running process launched from.

token-restart's deploy reconciliation compares this against the live checkout
HEAD to detect a stale process (checkout advanced without a paired restart).
The field must always be present in the payload (value may be None if the SHA
could not be captured at import — token-restart treats empty/absent as stale).
"""

from __future__ import annotations


def test_health_payload_includes_git_sha(app_env) -> None:
    from fastapi.testclient import TestClient

    client = TestClient(app_env.main.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    # Key MUST be present so token-restart's sed extraction has a field to read.
    assert "git_sha" in body
    # It reflects the SHA captured once at import (the code THIS process loaded).
    assert body["git_sha"] == app_env.main.LAUNCHED_GIT_SHA
