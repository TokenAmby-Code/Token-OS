"""
Slaanesh Scheduling Module — Calendly Integration

Polls the Calendly API for upcoming events booked via the public scheduling link.
Calendly handles the booking surface, availability rules, and calendar sync.
Token-API reads events and surfaces them to Slaanesh/Custodes.

Booking link: https://calendly.com/colbymlanier/date

Polling schedule:
  - 8:00 AM (morning cron window)
  - 3:00 PM (afternoon check)
  - On-demand via GET /api/schedule/refresh
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional, List

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger("token-api.schedule")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CALENDLY_API_TOKEN = os.environ.get("CALENDLY_API_TOKEN")
CALENDLY_BASE_URL = "https://api.calendly.com"
CALENDLY_BOOKING_URL = "https://calendly.com/colbymlanier/date"

# Resolved lazily on first API call
_calendly_user_uri: Optional[str] = None

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cached_events: List[dict] = []
_last_polled: Optional[str] = None  # ISO timestamp


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class CalendlyEvent(BaseModel):
    name: str                          # Invitee name
    email: Optional[str] = None        # Invitee email
    start_time: str                    # ISO 8601
    end_time: str                      # ISO 8601
    status: str                        # "active" or "canceled"
    event_type: Optional[str] = None   # Event type name from Calendly
    location: Optional[str] = None
    calendly_uri: str                  # Calendly event URI (unique ID)


class ScheduleResponse(BaseModel):
    events: List[CalendlyEvent]
    last_polled: Optional[str] = None
    booking_url: str = CALENDLY_BOOKING_URL
    mode: str = "calendly"


# ---------------------------------------------------------------------------
# Calendly API helpers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    if not CALENDLY_API_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="CALENDLY_API_TOKEN not configured. Add it to .env"
        )
    return {
        "Authorization": f"Bearer {CALENDLY_API_TOKEN}",
        "Content-Type": "application/json",
    }


async def _resolve_user_uri() -> str:
    """Get the authenticated user's URI (needed for event queries)."""
    global _calendly_user_uri
    if _calendly_user_uri:
        return _calendly_user_uri

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{CALENDLY_BASE_URL}/users/me",
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        _calendly_user_uri = resp.json()["resource"]["uri"]
        return _calendly_user_uri


async def poll_calendly() -> List[CalendlyEvent]:
    """Fetch upcoming events from Calendly API and update cache."""
    global _cached_events, _last_polled

    user_uri = await _resolve_user_uri()
    now = datetime.now(timezone.utc).isoformat()

    events = []
    next_page = None

    async with httpx.AsyncClient() as client:
        # Fetch scheduled events
        params = {
            "user": user_uri,
            "min_start_time": now,
            "status": "active",
            "sort": "start_time:asc",
            "count": 50,
        }

        resp = await client.get(
            f"{CALENDLY_BASE_URL}/scheduled_events",
            headers=_headers(),
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        for event in data.get("collection", []):
            event_uri = event["uri"]
            event_uuid = event_uri.rsplit("/", 1)[-1]

            # Fetch invitee details for this event
            inv_resp = await client.get(
                f"{CALENDLY_BASE_URL}/scheduled_events/{event_uuid}/invitees",
                headers=_headers(),
                timeout=10,
            )

            invitee_name = "Unknown"
            invitee_email = None
            if inv_resp.status_code == 200:
                invitees = inv_resp.json().get("collection", [])
                if invitees:
                    invitee_name = invitees[0].get("name", "Unknown")
                    invitee_email = invitees[0].get("email")

            location_info = None
            if event.get("location", {}).get("location"):
                location_info = event["location"]["location"]

            events.append(CalendlyEvent(
                name=invitee_name,
                email=invitee_email,
                start_time=event["start_time"],
                end_time=event["end_time"],
                status=event["status"],
                event_type=event.get("name"),
                location=location_info,
                calendly_uri=event_uri,
            ))

    _cached_events = [e.model_dump() for e in events]
    _last_polled = datetime.now(timezone.utc).isoformat()
    logger.info(f"Calendly poll: {len(events)} upcoming events")

    return events


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter()


@router.get("/api/schedule", response_model=ScheduleResponse)
async def get_schedule(refresh: bool = Query(False)):
    """Return cached Calendly events. Pass ?refresh=true to force re-poll."""
    if not CALENDLY_API_TOKEN:
        return ScheduleResponse(
            events=[],
            last_polled=None,
            mode="unconfigured",
        )

    if refresh or not _last_polled:
        try:
            await poll_calendly()
        except Exception as e:
            logger.error(f"Calendly poll failed: {e}")
            if not _cached_events:
                raise HTTPException(status_code=502, detail=f"Calendly API error: {e}")

    return ScheduleResponse(
        events=[CalendlyEvent(**e) for e in _cached_events],
        last_polled=_last_polled,
    )


@router.get("/api/schedule/refresh", response_model=ScheduleResponse)
async def refresh_schedule():
    """Force poll Calendly API and return fresh results."""
    events = await poll_calendly()
    return ScheduleResponse(
        events=events,
        last_polled=_last_polled,
    )


@router.get("/api/schedule/upcoming")
async def upcoming_events():
    """Convenience: future events only, sorted by start time."""
    if not _cached_events and CALENDLY_API_TOKEN:
        try:
            await poll_calendly()
        except Exception as e:
            logger.error(f"Calendly poll failed: {e}")

    now = datetime.now(timezone.utc).isoformat()
    upcoming = [
        e for e in _cached_events
        if e["start_time"] > now and e["status"] == "active"
    ]
    upcoming.sort(key=lambda e: e["start_time"])

    return {
        "events": upcoming,
        "count": len(upcoming),
        "booking_url": CALENDLY_BOOKING_URL,
        "last_polled": _last_polled,
    }
