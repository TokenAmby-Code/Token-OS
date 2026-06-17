import importlib.machinery
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "cli-tools" / "bin" / "codex-hooks-install"

loader = importlib.machinery.SourceFileLoader("codex_hooks_install", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)


def test_merge_adds_pretooluse_runtime_guard_without_clobbering_existing_hooks():
    data = {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo stop", "timeout": 5}]}]}
    }

    changed = mod.merge_codex_hooks(data)

    assert changed is True
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo stop"
    pre = data["hooks"]["PreToolUse"]
    assert pre[0]["hooks"][0]["command"] == mod.RUNTIME_WRITE_GUARD_HOOK["command"]
    assert pre[0]["hooks"][0]["timeout"] == 5


def test_merge_is_idempotent():
    data = {"hooks": {}}

    assert mod.merge_codex_hooks(data) is True
    first = json.loads(json.dumps(data))
    assert mod.merge_codex_hooks(data) is False

    assert data == first


def test_cli_creates_missing_hooks_file(tmp_path):
    hooks_path = tmp_path / ".codex" / "hooks.json"

    rc = mod.main(["--hooks", str(hooks_path), "--no-backup"])

    assert rc == 0
    data = json.loads(hooks_path.read_text())
    assert (
        data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        == mod.RUNTIME_WRITE_GUARD_HOOK["command"]
    )


def test_check_fails_when_required_hook_missing(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text('{"hooks": {}}\n')

    assert mod.main(["--hooks", str(hooks_path), "--check"]) == 1


def test_check_passes_when_required_hook_present(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"hooks": [mod.RUNTIME_WRITE_GUARD_HOOK]}]}})
    )

    assert mod.main(["--hooks", str(hooks_path), "--check"]) == 0
