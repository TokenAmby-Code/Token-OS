from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from cli_tools.db_query import cli, query_runner


def test_missing_deploy_dir_defaults_to_pax_sql(monkeypatch, tmp_path):
    monkeypatch.setattr(query_runner, "DEPLOY_DIR", tmp_path / "missing" / "deploy")
    monkeypatch.delenv("DB_HOST", raising=False)

    environments = query_runner._load_environments()

    assert environments["development"]["instance"] == "pax-dev-469018:us-central1:pax-sql"
    assert environments["development"]["database"] == "pax-sql"
    assert environments["production"]["instance"] == "pax-prod-467920:us-central1:pax-sql"
    assert environments["production"]["database"] == "pax-sql"
    assert environments["production"]["read_only"] is True


def test_db_and_instance_overrides_are_applied(monkeypatch):
    monkeypatch.setattr(
        cli,
        "get_env_config",
        lambda env: {
            "project_id": "pax-prod-467920",
            "instance": "pax-prod-467920:us-central1:pax-sql",
            "database": "pax-sql",
            "user": "postgres",
            "host": "localhost",
            "public_ip": None,
            "port": 5432,
            "read_only": True,
        },
    )
    args = argparse.Namespace(
        env="prod",
        direct=False,
        port=None,
        db="postgres",
        instance="override-project:us-central1:override-instance",
        host=None,
    )

    config = cli._get_config_with_overrides(args)

    assert config["database"] == "postgres"
    assert config["instance"] == "override-project:us-central1:override-instance"
    assert config["project_id"] == "override-project"


def test_password_resolves_from_secret_manager(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    monkeypatch.setattr(query_runner, "DEPLOY_DIR", tmp_path / "missing" / "deploy")
    fake_gcloud = tmp_path / "gcloud"
    fake_gcloud.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    fake_gcloud.chmod(0o755)
    monkeypatch.setattr(query_runner.shutil, "which", lambda name: str(fake_gcloud))

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="sekret\n", stderr="")

    monkeypatch.setattr(query_runner.subprocess, "run", fake_run)

    result = query_runner.resolve_password(
        {"instance": "pax-dev-469018:us-central1:pax-sql"},
    )

    assert result.password == "sekret"
    assert result.source == "Secret Manager pax-dev-469018/db-password"
    assert calls == [
        [
            str(fake_gcloud),
            "secrets",
            "versions",
            "access",
            "latest",
            "--secret",
            "db-password",
            "--project",
            "pax-dev-469018",
        ]
    ]


def test_password_failure_is_loud_when_secret_manager_unavailable(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    monkeypatch.setattr(query_runner, "DEPLOY_DIR", tmp_path / "missing" / "deploy")
    monkeypatch.setattr(query_runner.shutil, "which", lambda name: None)

    result = query_runner.resolve_password(
        {"instance": "pax-dev-469018:us-central1:pax-sql"},
    )

    assert result.password is None
    assert result.error is not None
    assert "Secret Manager lookup failed for pax-dev-469018/db-password" in result.error
    assert "gcloud not found" in result.error


def test_db_query_wrapper_uses_no_sync():
    wrapper = Path(__file__).resolve().parents[1] / "bin" / "db-query"
    assert "uv run --no-sync --directory" in wrapper.read_text(encoding="utf-8")
