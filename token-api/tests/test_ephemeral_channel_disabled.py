"""Behavioral pins for Token-API's decreed ephemeral-channel shutdown."""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

TOKEN_API_DIR = Path(__file__).resolve().parents[1]
if str(TOKEN_API_DIR) not in sys.path:
    sys.path.insert(0, str(TOKEN_API_DIR))

DISABLED_ERROR = "ephemeral channel disabled by decree"


def test_hook_subscription_refuses_ephemeral_before_resolution() -> None:
    hooks = importlib.import_module("routes.hooks")
    request = hooks.HookSubscribeRequest(
        target_pane="fake:target",
        subscriber_pane="fake:subscriber",
        delivery="ephemeral",
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(hooks.subscribe_hook(request))
    assert raised.value.status_code == 410
    assert raised.value.detail == DISABLED_ERROR


def test_existing_ephemeral_subscription_cannot_deliver_or_retry() -> None:
    hooks = importlib.import_module("routes.hooks")

    result = asyncio.run(
        hooks._enqueue_and_send_stop_delivery(
            None,
            subscription={"delivery": "ephemeral"},
            stop_event_key="fake-stop",
            payload="must not reach a pane",
        )
    )

    assert result == {"status": "failed", "error": DISABLED_ERROR}
