"""Bounties: Discord notification fabric (Terminus Stage 2, PR D).

Intent recorded 2026-07-08 (Terminus Decree addendum, daemon-consolidation
gradient): `dispatch_notify` grows a `discord` transport leg so notifications
can ride the Discord daemon as a device-agnostic channel — placed AFTER the
quiet-hours early-return and try/except-isolated so a dead daemon never masks
the TTS/tactile legs. A dedicated sender (`send_discord_notification`) owns the
daemon HTTP hop.

These stay open until the fabric ships; the conftest auto-applies
``bounty`` + ``xfail(strict=False)``. XPASS = graduate into the regression
suite (see tests/test_comms_router.py).
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
from pathlib import Path

TOKEN_API_DIR = Path(__file__).resolve().parents[2]


def _load(mod: str):
    if str(TOKEN_API_DIR) not in sys.path:
        sys.path.insert(0, str(TOKEN_API_DIR))
    return importlib.import_module(mod)


def test_dispatch_notify_has_discord_transport_kwarg() -> None:
    """The router front door accepts `discord=` alongside tts/vibe/beep/banner."""
    tts = _load("routes.tts")
    assert "discord" in inspect.signature(tts.dispatch_notify).parameters, (
        "open bounty: dispatch_notify has no discord transport kwarg yet"
    )


def test_dedicated_discord_sender_exists() -> None:
    """The daemon HTTP hop lives in one named sender, not inline in the router."""
    tts = _load("routes.tts")
    sender = tts.send_discord_notification  # AttributeError today == open bounty
    assert callable(sender)


def test_discord_leg_respects_quiet_hours(monkeypatch) -> None:
    """Quiet hours suppress the Discord leg like every other transport.

    The discord send must sit AFTER the quiet-hours early-return in
    dispatch_notify: suppressed notifications never reach the daemon.
    """
    tts = _load("routes.tts")
    monkeypatch.setattr(tts, "_is_quiet_hours", lambda *a, **k: True)

    async def _identity(value):
        return value or ""

    monkeypatch.setattr(tts, "_sanitize_public_text_async", _identity)

    sent: list[str] = []

    # raising=False: the sender does not exist yet — install the recorder over
    # the future attribute so this graduates without edits when it ships.
    monkeypatch.setattr(
        tts,
        "send_discord_notification",
        lambda *a, **k: sent.append(a[0] if a else k.get("message", "")),
        raising=False,
    )

    # Dies with TypeError (unknown kwarg) today == open bounty.
    result = asyncio.run(tts.dispatch_notify("x", discord=True))

    assert result.get("reason") == "quiet_hours"
    assert result.get("suppressed") is True
    assert sent == [], "quiet-hours-suppressed notify must never reach the daemon"
