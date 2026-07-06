import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIEF = REPO_ROOT / "cli-tools" / "bin" / "brief"


def _load_brief_module():
    spec = importlib.util.spec_from_loader("brief_cli", SourceFileLoader("brief_cli", str(BRIEF)))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_brief_cli_prints_failed_reason_when_error_field_empty(monkeypatch, capsys):
    brief = _load_brief_module()

    refusal = "P0_LEDGER_SNIFF_INCONGRUENCY ledger_occupied=false sniff_live_agent=true"

    def fake_post(_path, _body, timeout=60.0):
        return {
            "status": "failed",
            "resolved": [
                {
                    "status": "failed",
                    "position_id": "mechanicus:fabricator-general",
                    "error": "",
                    "reason": refusal,
                }
            ],
            "unresolved": [],
        }

    monkeypatch.setattr(brief, "_post", fake_post)
    monkeypatch.setattr(brief, "_resolve_caller", lambda _caller: "%caller")

    rc = brief.main(["--pane", "mechanicus:fabricator-general", "hello"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "delivered=0/1" in captured.out
    assert refusal in captured.err
