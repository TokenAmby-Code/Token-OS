from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_dispatch_no_longer_delivers_instance_name_prefix() -> None:
    combined = "\n".join(
        _text(rel)
        for rel in (
            "cli-tools/bin/dispatch",
            "cli-tools/lib/agent-wrapper-common.sh",
        )
    )
    assert "TOKEN_API_INSTANCE_NAME_PREFIX" not in combined
    assert "Instance name prefix" not in combined
    assert "On startup, name this instance" not in combined


def test_session_doc_rename_does_not_auto_derive_instance_names() -> None:
    main = _text("token-api/main.py")
    assert "session-doc-rename" not in main
    assert "SessionStart:session-doc-instance-name" not in _text("token-api/routes/hooks.py")
    assert "_apply_session_doc_instance_name" not in _text("token-api/routes/hooks.py")


def test_only_sanctioned_actors_can_write_non_placeholder_name() -> None:
    mutation = _text("token-api/instance_mutation.py")
    assert "OFFICIAL_INSTANCE_NAME_ACTORS" in mutation
    assert "instance-name-cli" in mutation
    assert "naming-interview" in mutation
    assert "session-doc-name" in mutation
    assert "_assert_name_update_authorized" in mutation
