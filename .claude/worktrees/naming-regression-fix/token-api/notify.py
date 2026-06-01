"""Notification request contract + compatibility shim.

The single routing brain lives in `routes.tts.dispatch_notify`
(geofence-first: Discord VC → WSL/phone-by-geofence → Mac, plus tactile/banner
fanout and quiet-hours gating). This module no longer contains a second device
router — the old WSL>Mac>phone `DEFAULT_DEVICE_ORDER` path, `force_device`, and
`distraction_source` were retired so there is exactly one routing decision that
can never disagree with the geofence router.

`NotifyRequest` + `dispatch_notification` are kept as the typed entry that
`enforce.py` and the APScheduler sync wrapper already import; both delegate to
the unified core.
"""

from __future__ import annotations

from pydantic import BaseModel


class NotifyRequest(BaseModel):
    message: str
    type: str = "tts"  # retained for back-compat; routing is unified regardless
    voice: str | None = None
    instance_id: str | None = None


async def dispatch_notification(request: NotifyRequest) -> dict:
    """Delegate to the single comms router (`routes.tts.dispatch_notify`)."""
    from routes.tts import dispatch_notify

    return await dispatch_notify(
        request.message,
        tts=True,
        voice=request.voice,
        instance_id=request.instance_id,
    )
