"""Day-start route placeholder for dirty worktree imports."""
from fastapi import APIRouter

router = APIRouter(prefix="/api/day-start", tags=["day-start"])


@router.get("/status")
async def day_start_status():
    return {"status": "unimplemented"}


@router.post("/fire")
async def fire_day_start_endpoint():
    return await fire_day_start_internal(source="api")


async def fire_day_start_internal(*, source="unknown", **kwargs):
    return {"status": "skipped", "source": source, "reason": "day_start placeholder"}


async def sync_day_start_schedule_from_daily_note():
    return None
