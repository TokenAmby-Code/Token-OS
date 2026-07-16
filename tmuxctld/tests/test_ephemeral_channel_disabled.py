"""Behavioral pins for the decreed shutdown of the ephemeral side-channel.

These tests use fake pane identities only.  They must never contact live tmux.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest
from tmuxctl import daemon
from tmuxctl.skill_invoke import ethereal_invocation_text

ROOT = Path(__file__).resolve().parents[2]
DISABLED_ERROR = "ephemeral channel disabled by decree"


def _load_extensionless_module(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_brief_ephemeral_fails_loud_without_transport(monkeypatch, capsys):
    brief = _load_extensionless_module("brief_cli", ROOT / "cli-tools" / "bin" / "brief")

    def unexpected_post(*_args, **_kwargs):
        pytest.fail("disabled --ephemeral path attempted Token-API transport")

    monkeypatch.setattr(brief, "_post", unexpected_post)

    result = brief.main(["--ephemeral", "--pane", "fake:ephemeral", "status only"])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == f"brief: {DISABLED_ERROR}\n"


def test_hook_ephemeral_fails_loud_without_transport(monkeypatch, capsys):
    hook = _load_extensionless_module("hook_cli", ROOT / "cli-tools" / "bin" / "hook")

    def unexpected_request(*_args, **_kwargs):
        pytest.fail("disabled hook ephemeral path attempted Token-API transport")

    monkeypatch.setattr(hook, "_request", unexpected_request)

    result = hook.main(
        [
            "subscribe",
            "--pane",
            "fake:target",
            "--notify",
            "fake:subscriber",
            "--delivery",
            "ephemeral",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == f"hook: {DISABLED_ERROR}\n"


def test_tmuxctld_send_ethereal_fails_before_pane_resolution():
    class NoTmuxControl:
        @property
        def adapter(self):
            pytest.fail("disabled /send-ethereal path touched a tmux adapter")

        def resolve_instance(self, _instance_id):
            pytest.fail("disabled /send-ethereal path resolved an instance")

    with pytest.raises(ValueError, match=f"^{DISABLED_ERROR}$"):
        daemon._h_send_ethereal(
            NoTmuxControl(),
            {"pane": "fake:ephemeral", "message": "status only", "agent": "claude"},
        )

    assert daemon.ROUTES[("POST", "/send-ethereal")] is daemon._h_send_ethereal


def test_ethereal_renderer_cannot_bypass_disabled_route():
    with pytest.raises(ValueError, match=f"^{DISABLED_ERROR}$"):
        ethereal_invocation_text("claude", "status only")


def test_user_prompt_hook_and_keybinding_cannot_arm_btw_reprompt():
    settings = json.loads((ROOT / "claude-config" / "settings.template.json").read_text())
    prompt_hooks = settings["hooks"]["UserPromptSubmit"]
    commands = [hook["command"] for group in prompt_hooks for hook in group["hooks"]]
    assert all("btw-capture" not in command for command in commands)

    tmux_config = (ROOT / "cli-tools" / "tmux" / "tmux-base.conf").read_text()
    assert f'bind B display-message "{DISABLED_ERROR}"' in tmux_config
    assert "bind B run-shell" not in tmux_config


@pytest.mark.parametrize(
    "relative_path",
    ["cli-tools/bin/ethereal-prompt", "claude-config/hooks/btw-capture.sh"],
)
def test_legacy_reprompt_entrypoints_are_terminal_errors(relative_path):
    path = ROOT / relative_path
    result = subprocess.run(
        ["bash", str(path), "fake:ephemeral"],
        text=True,
        capture_output=True,
        check=False,
        timeout=2,
    )

    assert result.returncode != 0
    assert DISABLED_ERROR in result.stderr
    assert "tmux send-keys" not in path.read_text()
