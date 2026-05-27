"""Timer engine v2 — layered composite model, pure logic, no I/O.

All time values are integer milliseconds. Time source is injected via
now_mono_ms parameters for deterministic testing.

State model: three mode layers compose into 6 effective modes, plus focus overlay.
  Activity:     working | distraction   (from AHK/phone detection)
  Productivity: active | inactive       (from Claude instances / work actions)
  Manual:       None | BREAK | QUIET | SLEEPING (user-initiated overrides)
  Focus:        on | off                (user toggle, auto-off on distraction)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Activity(str, Enum):
    WORKING = "working"
    DISTRACTION = "distraction"


class TimerMode(str, Enum):
    WORKING = "working"
    MULTITASKING = "multitasking"
    DISTRACTED = "distracted"
    IDLE = "idle"
    BREAK = "break"
    QUIET = "quiet"
    SLEEPING = "sleeping"


class TimerEvent(Enum):
    BREAK_EXHAUSTED = "break_exhausted"
    IDLE_TIMEOUT = "idle_timeout"
    DISTRACTION_TIMEOUT = "distraction_timeout"
    DAILY_RESET = "daily_reset"
    MODE_CHANGED = "mode_changed"


@dataclass
class TickResult:
    events: list[TimerEvent] = field(default_factory=list)
    old_mode: TimerMode | None = None
    productivity_score: int | None = None
    reset_date: str | None = None


# Break rates as (numerator, denominator) — integer rational arithmetic.
# break_delta_ms = elapsed_ms * numerator // denominator
BREAK_RATE_TABLE: dict[TimerMode, tuple[int, int]] = {
    TimerMode.WORKING: (1, 1),  # +60 min/hr
    TimerMode.MULTITASKING: (0, 1),  # neutral
    TimerMode.IDLE: (0, 1),  # neutral
    TimerMode.DISTRACTED: (-1, 1),  # -60 min/hr (penalty)
    TimerMode.BREAK: (-1, 1),  # -60 min/hr (consuming break)
    TimerMode.QUIET: (0, 1),  # neutral
    TimerMode.SLEEPING: (0, 1),  # neutral
}

# Timeouts
IDLE_TIMEOUT_FROM_WORKING_MS = 420_000  # 7 minutes after productivity goes inactive
IDLE_TIMEOUT_FROM_MULTITASKING_MS = 420_000  # 7 minutes after productivity goes inactive
DISTRACTION_TIMEOUT_MS = 600_000  # 10 minutes (scrolling/gaming only)
GYM_BOUNTY_MS = 1_800_000  # 30 minutes

# Focus layer — indexes backward from cutoff hour at 1:1
FOCUS_CUTOFF_BASE_HOUR = 21  # 9 PM
FOCUS_CUTOFF_FLOOR_HOUR = 18  # 6 PM (minimum, prevents gaming)

MAX_IDLE_MS = 10 * 60 * 1000  # 10 min gap detection
MANUAL_LOCK_DURATION_MS = 20 * 60 * 1000  # 20 minutes
DEFAULT_BREAK_BUFFER_MS = 5 * 60 * 1000  # 5 min starting break on reset

# Legacy compat — old code may import these
IDLE_TO_BREAK_TIMEOUT_MS = IDLE_TIMEOUT_FROM_WORKING_MS


def format_timer_time(ms: int) -> str:
    """Format milliseconds as 'Xh Ym' string."""
    is_negative = ms < 0
    abs_ms = abs(ms)
    hours = abs_ms // (1000 * 60 * 60)
    minutes = (abs_ms % (1000 * 60 * 60)) // (1000 * 60)
    sign = "-" if is_negative else ""
    return f"{sign}{hours}h {minutes}m"


class TimerEngine:
    """Encapsulates all timer state and logic.

    Pure computation — no I/O, no globals, deterministically testable.
    State is three independent layers that compose into an effective mode.
    """

    def __init__(self, now_mono_ms: int, reset_hour: int = 7):
        # Layer state
        self._activity: Activity = Activity.WORKING
        self._productivity_active: bool = True
        self._manual_mode: TimerMode | None = None  # BREAK or SLEEPING

        # Layer substates — grouped per-layer, reset when layer value changes.
        # Manual: {trigger: str, lock_until_ms: int|None}
        self._manual_substate: dict | None = None
        # Activity: {distraction_started_ms: int|None, is_scrolling_gaming: bool}
        self._activity_substate: dict = {
            "distraction_started_ms": None,
            "is_scrolling_gaming": False,
        }
        # Productivity: {idle_entered_ms: int|None, idle_timeout_ms: int, idle_timeout_exempt: bool}
        self._productivity_substate: dict = {
            "idle_entered_ms": None,
            "idle_timeout_ms": IDLE_TIMEOUT_FROM_WORKING_MS,
            "idle_timeout_exempt": False,
        }

        # Focus layer (independent, parallel to other layers)
        self._focus_active: bool = False
        self._total_focus_time_ms: int = 0

        # Counters
        self._total_work_time_ms: int = 0
        self._total_break_time_ms: int = 0
        self._break_balance_ms: int = 0  # positive = available, negative = backlog/debt

        # Timing
        self._daily_start_date: str | None = None
        self._last_tick_ms: int = now_mono_ms
        self._reset_hour: int = reset_hour

    # ---- Read-only properties ----

    @property
    def effective_mode(self) -> TimerMode:
        """Derive effective mode from layers (priority order, top wins)."""
        # 1. Manual override
        if self._manual_mode is not None:
            return self._manual_mode

        # 2. Inactive + distraction → BREAK
        if not self._productivity_active and self._activity == Activity.DISTRACTION:
            return TimerMode.BREAK

        # 3-4. Active + distraction
        if self._productivity_active and self._activity == Activity.DISTRACTION:
            asub = self._activity_substate
            if (
                asub["is_scrolling_gaming"]
                and asub["distraction_started_ms"] is not None
                and self._last_tick_ms - asub["distraction_started_ms"] >= DISTRACTION_TIMEOUT_MS
            ):
                return TimerMode.DISTRACTED  # 3. scrolling/gaming ≥10min
            return TimerMode.MULTITASKING  # 4. distraction <10min or video

        # 5. Inactive + working → IDLE
        if not self._productivity_active and self._activity == Activity.WORKING:
            return TimerMode.IDLE

        # 6. Active + working → WORKING
        return TimerMode.WORKING

    @property
    def current_mode(self) -> TimerMode:
        """Alias for effective_mode (backward compat)."""
        return self.effective_mode

    @property
    def break_balance_ms(self) -> int:
        """Signed break balance: positive = available, negative = backlog/debt."""
        return self._break_balance_ms

    @property
    def is_in_backlog(self) -> bool:
        return self._break_balance_ms < 0

    # Backward compat properties (derived from single signed value)
    @property
    def accumulated_break_ms(self) -> int:
        return max(0, self._break_balance_ms)

    @property
    def break_backlog_ms(self) -> int:
        return abs(min(0, self._break_balance_ms))

    @property
    def manual_mode_lock(self) -> bool:
        if self._manual_substate is None:
            return False
        return self._manual_substate.get("lock_until_ms") is not None

    @property
    def manual_trigger(self) -> str | None:
        if self._manual_substate is None:
            return None
        return self._manual_substate.get("trigger")

    @property
    def quiet_context(self) -> str | None:
        if self._manual_mode != TimerMode.QUIET or self._manual_substate is None:
            return None
        return self._manual_substate.get("trigger")

    @property
    def total_work_time_ms(self) -> int:
        return self._total_work_time_ms

    @property
    def total_break_time_ms(self) -> int:
        return self._total_break_time_ms

    @property
    def daily_start_date(self) -> str | None:
        return self._daily_start_date

    @property
    def idle_timeout_exempt(self) -> bool:
        return self._productivity_substate["idle_timeout_exempt"]

    @idle_timeout_exempt.setter
    def idle_timeout_exempt(self, value: bool) -> None:
        self._productivity_substate["idle_timeout_exempt"] = value

    @property
    def activity(self) -> Activity:
        return self._activity

    @property
    def productivity_active(self) -> bool:
        return self._productivity_active

    @property
    def focus_active(self) -> bool:
        return self._focus_active

    @property
    def total_focus_time_ms(self) -> int:
        return self._total_focus_time_ms

    @property
    def focus_cutoff_hour(self) -> float:
        """Derive end-of-day cutoff: base (21:00) minus focus hours, floored at 18:00."""
        focus_hours = self._total_focus_time_ms / (1000 * 60 * 60)
        cutoff = FOCUS_CUTOFF_BASE_HOUR - focus_hours
        return max(FOCUS_CUTOFF_FLOOR_HOUR, cutoff)

    @property
    def focus_cutoff_time(self) -> str:
        """Human-readable cutoff time string like '20:30' or '9:00 PM'."""
        h = self.focus_cutoff_hour
        hour = int(h)
        minute = int((h - hour) * 60)
        return f"{hour}:{minute:02d}"

    @property
    def manual_mode(self) -> TimerMode | None:
        return self._manual_mode

    @property
    def idle_timeout_ms(self) -> int:
        return self._productivity_substate["idle_timeout_ms"]

    @property
    def idle_entered_ms(self) -> int | None:
        return self._productivity_substate["idle_entered_ms"]

    def idle_remaining_ms(self, now_mono_ms: int) -> int | None:
        idle_entered = self.idle_entered_ms
        if idle_entered is None or self.idle_timeout_exempt:
            return None
        return max(0, self.idle_timeout_ms - (now_mono_ms - idle_entered))

    @property
    def distraction_started_ms(self) -> int | None:
        return self._activity_substate["distraction_started_ms"]

    # ---- Manual mode helpers ----

    def _set_manual_mode(
        self,
        mode: TimerMode,
        trigger: str,
        now_mono_ms: int,
        lock_duration_ms: int = MANUAL_LOCK_DURATION_MS,
    ) -> None:
        """Set manual mode with substate. All manual mode entry goes through here."""
        self._manual_mode = mode
        self._manual_substate = {
            "trigger": trigger,
            "lock_until_ms": now_mono_ms + lock_duration_ms,
        }

    def _clear_manual_mode(self) -> None:
        """Clear manual mode and its substate. All manual mode exit goes through here."""
        self._manual_mode = None
        self._manual_substate = None

    # ---- Layer mutation methods ----

    def set_activity(
        self, activity: Activity, is_scrolling_gaming: bool, now_mono_ms: int
    ) -> TickResult:
        """Update the activity layer. Called by AHK/phone detection."""
        old_mode = self.effective_mode
        result = self._advance(now_mono_ms)

        # Auto-exit focus on any distraction
        if activity == Activity.DISTRACTION and self._focus_active:
            self._focus_active = False

        sub = self._activity_substate
        if activity == Activity.DISTRACTION:
            if self._activity != Activity.DISTRACTION:
                # Entering distraction — start timer
                sub["distraction_started_ms"] = now_mono_ms
                sub["is_scrolling_gaming"] = is_scrolling_gaming
            elif is_scrolling_gaming and not sub["is_scrolling_gaming"]:
                # Upgrading from video to scrolling/gaming — reset timer
                sub["distraction_started_ms"] = now_mono_ms
                sub["is_scrolling_gaming"] = True
            elif not is_scrolling_gaming and sub["is_scrolling_gaming"]:
                # Downgrading from scrolling/gaming to video — clear scrolling flag
                sub["is_scrolling_gaming"] = False
        else:
            # Back to working — reset activity substate
            sub["distraction_started_ms"] = None
            sub["is_scrolling_gaming"] = False

        self._activity = activity

        new_mode = self.effective_mode
        if new_mode != old_mode:
            result.events.append(TimerEvent.MODE_CHANGED)
            result.old_mode = old_mode
        return result

    def set_productivity(self, active: bool, now_mono_ms: int) -> TickResult:
        """Update the productivity layer. Called by Claude activity / work actions."""
        old_mode = self.effective_mode
        result = self._advance(now_mono_ms)

        was_active = self._productivity_active
        sub = self._productivity_substate

        if active and not was_active:
            # Becoming active — clear idle state
            self._productivity_active = active
            sub["idle_entered_ms"] = None
            # Auto-clear break if it was set by idle timeout (user is back)
            if (
                self._manual_mode == TimerMode.BREAK
                and self._manual_substate
                and self._manual_substate.get("trigger") == "idle_timeout"
            ):
                self._clear_manual_mode()
        elif not active and was_active:
            # Becoming inactive — parameterize idle timeout based on CURRENT mode
            # (before changing productivity, so effective_mode still reflects active state)
            if old_mode in (TimerMode.MULTITASKING, TimerMode.DISTRACTED):
                sub["idle_timeout_ms"] = IDLE_TIMEOUT_FROM_MULTITASKING_MS
            else:
                sub["idle_timeout_ms"] = IDLE_TIMEOUT_FROM_WORKING_MS
            self._productivity_active = active
            sub["idle_entered_ms"] = now_mono_ms
        else:
            self._productivity_active = active

        new_mode = self.effective_mode
        if new_mode != old_mode:
            result.events.append(TimerEvent.MODE_CHANGED)
            result.old_mode = old_mode
        return result

    def enter_break(self, now_mono_ms: int) -> tuple[bool, TickResult]:
        """Manual break entry. Returns (changed, result)."""
        if self._manual_mode == TimerMode.BREAK:
            return False, TickResult()

        old_mode = self.effective_mode
        result = self._advance(now_mono_ms)

        self._set_manual_mode(TimerMode.BREAK, "user", now_mono_ms)

        new_mode = self.effective_mode
        if new_mode != old_mode:
            result.events.append(TimerEvent.MODE_CHANGED)
            result.old_mode = old_mode
        return True, result

    def enter_sleeping(self, now_mono_ms: int) -> tuple[bool, TickResult]:
        """Manual sleeping entry. Returns (changed, result)."""
        if self._manual_mode == TimerMode.SLEEPING:
            return False, TickResult()

        old_mode = self.effective_mode
        result = self._advance(now_mono_ms)

        self._set_manual_mode(TimerMode.SLEEPING, "user", now_mono_ms)

        new_mode = self.effective_mode
        if new_mode != old_mode:
            result.events.append(TimerEvent.MODE_CHANGED)
            result.old_mode = old_mode
        return True, result

    def enter_quiet(self, now_mono_ms: int, context: str = "sleeping") -> tuple[bool, TickResult]:
        """Enter quiet mode. Context distinguishes sleeping from do-not-disturb."""
        if self._manual_mode == TimerMode.QUIET and self.quiet_context == context:
            return False, TickResult()

        old_mode = self.effective_mode
        result = self._advance(now_mono_ms)

        self._set_manual_mode(TimerMode.QUIET, context, now_mono_ms, lock_duration_ms=0)
        if self._manual_substate:
            self._manual_substate["lock_until_ms"] = None

        new_mode = self.effective_mode
        if new_mode != old_mode:
            result.events.append(TimerEvent.MODE_CHANGED)
            result.old_mode = old_mode
        return True, result

    def resume(self, now_mono_ms: int) -> tuple[bool, TickResult]:
        """Exit manual mode (break/sleeping). Returns (changed, result)."""
        if self._manual_mode is None:
            return False, TickResult()

        old_mode = self.effective_mode
        result = self._advance(now_mono_ms)

        self._clear_manual_mode()

        new_mode = self.effective_mode
        if new_mode != old_mode:
            result.events.append(TimerEvent.MODE_CHANGED)
            result.old_mode = old_mode
        return True, result

    def apply_gym_bounty(self, now_mono_ms: int) -> TickResult:
        """Grant +30 min break on gym exit."""
        result = self._advance(now_mono_ms)
        self._apply_break_delta(GYM_BOUNTY_MS, result)
        return result

    # ---- Focus layer ----

    def enter_focus(self, now_mono_ms: int) -> tuple[bool, TickResult]:
        """Toggle focus ON. Returns (changed, result)."""
        if self._focus_active:
            return False, TickResult()
        result = self._advance(now_mono_ms)
        self._focus_active = True
        return True, result

    def exit_focus(self, now_mono_ms: int) -> tuple[bool, TickResult]:
        """Toggle focus OFF. Returns (changed, result)."""
        if not self._focus_active:
            return False, TickResult()
        result = self._advance(now_mono_ms)
        self._focus_active = False
        return True, result

    # ---- Tick ----

    def tick(
        self,
        now_mono_ms: int,
        today_date: str,
        current_hour: int | None = None,
        suppress_idle_timeout: bool = False,
    ) -> TickResult:
        """Main tick: check daily reset, then advance counters."""
        reset_result = self._check_daily_reset(now_mono_ms, today_date, current_hour)
        if reset_result is not None:
            return reset_result

        # Auto-switch from legacy sleeping to working at reset hour.
        # QUIET is controlled by explicit sleep/wake endpoints and schedules.
        if (
            current_hour is not None
            and current_hour >= self._reset_hour
            and self._manual_mode == TimerMode.SLEEPING
        ):
            old_mode = self.effective_mode
            result = self._advance(now_mono_ms, suppress_idle_timeout=suppress_idle_timeout)
            self._clear_manual_mode()
            new_mode = self.effective_mode
            if new_mode != old_mode:
                result.events.append(TimerEvent.MODE_CHANGED)
                result.old_mode = old_mode
            return result

        return self._advance(now_mono_ms, suppress_idle_timeout=suppress_idle_timeout)

    # ---- Serialization ----

    def to_dict(self, now_mono_ms: int) -> dict:
        """Serialize state for DB persistence (snake_case keys)."""
        # Manual substate
        lock_remaining_ms = 0
        manual_trigger = None
        if self._manual_substate:
            manual_trigger = self._manual_substate.get("trigger")
            lock_until = self._manual_substate.get("lock_until_ms")
            if lock_until is not None:
                lock_remaining_ms = max(0, lock_until - now_mono_ms)

        # Productivity substate
        psub = self._productivity_substate
        idle_entered_elapsed_ms = 0
        if psub["idle_entered_ms"] is not None:
            idle_entered_elapsed_ms = max(0, now_mono_ms - psub["idle_entered_ms"])

        # Activity substate
        asub = self._activity_substate
        distraction_elapsed_ms = 0
        if asub["distraction_started_ms"] is not None:
            distraction_elapsed_ms = max(0, now_mono_ms - asub["distraction_started_ms"])

        return {
            "format_version": 2,
            # Layers
            "activity": self._activity.value,
            "productivity_active": self._productivity_active,
            "manual_mode": self._manual_mode.value if self._manual_mode else None,
            "quiet_context": self.quiet_context,
            # Focus layer
            "focus_active": self._focus_active,
            "total_focus_time_ms": self._total_focus_time_ms,
            # Manual substate
            "manual_trigger": manual_trigger,
            "manual_mode_lock": self.manual_mode_lock,
            "manual_mode_lock_remaining_ms": lock_remaining_ms,
            # Counters
            "total_work_time_ms": self._total_work_time_ms,
            "total_break_time_ms": self._total_break_time_ms,
            "break_balance_ms": self._break_balance_ms,
            # Timing
            "daily_start_date": self._daily_start_date,
            # Productivity substate
            "idle_entered_elapsed_ms": idle_entered_elapsed_ms,
            "idle_timeout_ms": psub["idle_timeout_ms"],
            "idle_timeout_exempt": psub["idle_timeout_exempt"],
            # Activity substate
            "distraction_elapsed_ms": distraction_elapsed_ms,
            "distraction_is_scrolling_gaming": asub["is_scrolling_gaming"],
        }

    def to_export_dict(self) -> dict:
        """CamelCase dict for JSON file and API export."""
        return {
            "currentMode": self.effective_mode.value,
            "activity": self._activity.value,
            "productivityActive": self._productivity_active,
            "manualMode": self._manual_mode.value if self._manual_mode else None,
            "quietContext": self.quiet_context,
            "breakAvailableSeconds": round(max(0, self._break_balance_ms) / 1000),
            "breakBalanceSeconds": round(self._break_balance_ms / 1000),
            "isInBacklog": self._break_balance_ms < 0,
            "backlogSeconds": round(abs(min(0, self._break_balance_ms)) / 1000),
            "workTimeSeconds": round(self._total_work_time_ms / 1000),
            "breakUsedSeconds": round(self._total_break_time_ms / 1000),
            "focusActive": self._focus_active,
            "focusTimeSeconds": round(self._total_focus_time_ms / 1000),
            "focusCutoffHour": self.focus_cutoff_hour,
            "focusCutoffTime": self.focus_cutoff_time,
        }

    def from_dict(self, data: dict, now_mono_ms: int) -> None:
        """Restore state from DB. Handles both v2 and legacy formats."""
        version = data.get("format_version", 1)

        if version >= 2:
            self._load_v2(data, now_mono_ms)
        else:
            self._load_legacy(data, now_mono_ms)

    def _load_v2(self, data: dict, now_mono_ms: int) -> None:
        """Load v2 format (layered model)."""
        self._activity = Activity(data.get("activity", "working"))
        self._productivity_active = data.get("productivity_active", True)
        manual = data.get("manual_mode")
        self._manual_mode = TimerMode(manual) if manual else None

        # Focus layer
        self._focus_active = data.get("focus_active", False)
        self._total_focus_time_ms = int(data.get("total_focus_time_ms", 0))

        self._total_work_time_ms = int(data.get("total_work_time_ms", 0))
        self._total_break_time_ms = int(data.get("total_break_time_ms", 0))
        # New signed key, fallback to old two-counter format
        if "break_balance_ms" in data:
            self._break_balance_ms = int(data["break_balance_ms"])
        else:
            acc = int(data.get("accumulated_break_ms", 0))
            bl = int(data.get("break_backlog_ms", 0))
            self._break_balance_ms = acc - bl
        self._daily_start_date = data.get("daily_start_date")

        # Manual substate
        if self._manual_mode is not None:
            has_lock = data.get("manual_mode_lock", False)
            remaining = int(data.get("manual_mode_lock_remaining_ms", 0))
            trigger = data.get("quiet_context") or data.get("manual_trigger", "user")
            self._manual_substate = {
                "trigger": trigger,
                "lock_until_ms": now_mono_ms + remaining if has_lock and remaining > 0 else None,
            }
            if self._manual_mode == TimerMode.QUIET:
                self._manual_substate["lock_until_ms"] = None
        else:
            self._manual_substate = None

        # Productivity substate
        psub = self._productivity_substate
        idle_elapsed = int(data.get("idle_entered_elapsed_ms", 0))
        if idle_elapsed > 0 and not self._productivity_active:
            psub["idle_entered_ms"] = now_mono_ms - idle_elapsed
        else:
            psub["idle_entered_ms"] = None
        psub["idle_timeout_ms"] = int(data.get("idle_timeout_ms", IDLE_TIMEOUT_FROM_WORKING_MS))
        psub["idle_timeout_exempt"] = data.get("idle_timeout_exempt", False)

        # Activity substate
        asub = self._activity_substate
        distraction_elapsed = int(data.get("distraction_elapsed_ms", 0))
        if distraction_elapsed > 0 and self._activity == Activity.DISTRACTION:
            asub["distraction_started_ms"] = now_mono_ms - distraction_elapsed
        else:
            asub["distraction_started_ms"] = None
        asub["is_scrolling_gaming"] = data.get("distraction_is_scrolling_gaming", False)

        self._last_tick_ms = now_mono_ms

    def _load_legacy(self, data: dict, now_mono_ms: int) -> None:
        """Migrate v1 (flat mode) format to v2 layers."""
        old_mode = data.get("current_mode", "work_silence")
        asub = self._activity_substate

        # Map old mode → new layers
        if old_mode in ("work_silence", "work_music"):
            self._activity = Activity.WORKING
            self._productivity_active = True
            self._clear_manual_mode()
        elif old_mode == "work_video":
            self._activity = Activity.DISTRACTION
            self._productivity_active = True
            asub["is_scrolling_gaming"] = False
            asub["distraction_started_ms"] = now_mono_ms
            self._clear_manual_mode()
        elif old_mode in ("work_scrolling", "work_gaming"):
            self._activity = Activity.DISTRACTION
            self._productivity_active = True
            asub["is_scrolling_gaming"] = True
            asub["distraction_started_ms"] = now_mono_ms
            self._clear_manual_mode()
        elif old_mode == "idle":
            self._activity = Activity.WORKING
            self._productivity_active = False
            self._clear_manual_mode()
        elif old_mode == "break":
            self._activity = Activity.WORKING
            self._productivity_active = True
            self._set_manual_mode(TimerMode.BREAK, "user", now_mono_ms)
        elif old_mode == "pause":
            self._activity = Activity.WORKING
            self._productivity_active = False
            self._clear_manual_mode()
        elif old_mode in ("gym", "work_gym"):
            self._activity = Activity.WORKING
            self._productivity_active = True
            self._clear_manual_mode()
        elif old_mode == "quiet":
            self._activity = Activity.WORKING
            self._productivity_active = True
            self._set_manual_mode(
                TimerMode.QUIET, data.get("quiet_context", "sleeping"), now_mono_ms
            )
            if self._manual_substate:
                self._manual_substate["lock_until_ms"] = None
        elif old_mode == "sleeping":
            self._activity = Activity.WORKING
            self._productivity_active = True
            self._set_manual_mode(TimerMode.QUIET, "sleeping", now_mono_ms)
            if self._manual_substate:
                self._manual_substate["lock_until_ms"] = None
        else:
            # Unknown mode — default to working
            self._activity = Activity.WORKING
            self._productivity_active = True
            self._clear_manual_mode()

        # Restore counters
        self._total_work_time_ms = int(data.get("total_work_time_ms", 0))
        self._total_break_time_ms = int(data.get("total_break_time_ms", 0))
        acc = int(data.get("accumulated_break_ms", 0))
        bl = int(data.get("break_backlog_ms", 0))
        self._break_balance_ms = acc - bl
        self._daily_start_date = data.get("daily_start_date")

        # Restore manual substate from legacy lock fields
        if self._manual_mode is not None:
            has_lock = data.get("manual_mode_lock", False)
            remaining = int(data.get("manual_mode_lock_remaining_ms", 0))
            lock_until = None
            if has_lock and remaining > 0:
                lock_until = now_mono_ms + remaining
            elif has_lock and data.get("manual_mode_lock_until"):
                import time as _time

                remaining_s = float(data["manual_mode_lock_until"]) - _time.time()
                if remaining_s > 0:
                    lock_until = now_mono_ms + int(remaining_s * 1000)
            # Merge lock_until into existing substate (set by _set_manual_mode above)
            if self._manual_substate:
                self._manual_substate["lock_until_ms"] = lock_until

        # Productivity substate
        psub = self._productivity_substate
        idle_elapsed = int(data.get("idle_entered_elapsed_ms", 0))
        if idle_elapsed > 0 and not self._productivity_active:
            psub["idle_entered_ms"] = now_mono_ms - idle_elapsed
        else:
            psub["idle_entered_ms"] = None
        psub["idle_timeout_ms"] = IDLE_TIMEOUT_FROM_WORKING_MS
        psub["idle_timeout_exempt"] = data.get("idle_timeout_exempt", False)

        self._last_tick_ms = now_mono_ms

    def force_daily_reset(self, now_mono_ms: int, today_date: str) -> TickResult:
        """Force a daily reset regardless of date. Used for scheduled reset."""
        productivity_score = max(0, self._break_balance_ms // (1000 * 60))

        result = TickResult()
        result.events.append(TimerEvent.DAILY_RESET)
        result.productivity_score = productivity_score
        result.reset_date = self._daily_start_date or today_date

        self._reset_state(now_mono_ms, today_date, with_buffer=False)
        return result

    # ---- Internal ----

    def _advance(self, now_mono_ms: int, suppress_idle_timeout: bool = False) -> TickResult:
        """Advance timer counters by elapsed time since last tick."""
        result = TickResult()
        elapsed_ms = now_mono_ms - self._last_tick_ms

        # Idle detection or no time elapsed
        if elapsed_ms > MAX_IDLE_MS or elapsed_ms <= 0:
            self._last_tick_ms = now_mono_ms
            return result

        mode = self.effective_mode

        if mode == TimerMode.WORKING:
            self._total_work_time_ms += elapsed_ms
            rate = BREAK_RATE_TABLE[mode]
            num, den = rate
            break_delta_ms = elapsed_ms * num // den
            self._apply_break_delta(break_delta_ms, result)

        elif mode == TimerMode.MULTITASKING:
            self._total_work_time_ms += elapsed_ms
            # 0:0 neutral — no break delta
            # Check if this tick crosses the distraction timeout (scrolling/gaming only)
            asub = self._activity_substate
            if asub["is_scrolling_gaming"] and asub["distraction_started_ms"] is not None:
                was_before = (
                    self._last_tick_ms - elapsed_ms - asub["distraction_started_ms"]
                ) < DISTRACTION_TIMEOUT_MS
                is_after = (now_mono_ms - asub["distraction_started_ms"]) >= DISTRACTION_TIMEOUT_MS
                if was_before and is_after:
                    result.events.append(TimerEvent.DISTRACTION_TIMEOUT)
                    result.events.append(TimerEvent.MODE_CHANGED)
                    result.old_mode = mode

        elif mode == TimerMode.DISTRACTED:
            self._total_work_time_ms += elapsed_ms
            rate = BREAK_RATE_TABLE[mode]
            num, den = rate
            break_delta_ms = elapsed_ms * num // den
            self._apply_break_delta(break_delta_ms, result)

        elif mode == TimerMode.IDLE:
            # No accumulation. Check idle timeout → auto-break.
            psub = self._productivity_substate
            idle_timed_out = (
                psub["idle_entered_ms"] is not None
                and not psub["idle_timeout_exempt"]
                and now_mono_ms - psub["idle_entered_ms"] >= psub["idle_timeout_ms"]
            )
            if idle_timed_out and suppress_idle_timeout:
                psub["idle_entered_ms"] = now_mono_ms
            elif idle_timed_out:
                old_mode = self.effective_mode
                self._set_manual_mode(TimerMode.BREAK, "idle_timeout", now_mono_ms)
                psub["idle_entered_ms"] = None
                result.events.append(TimerEvent.IDLE_TIMEOUT)
                result.events.append(TimerEvent.MODE_CHANGED)
                result.old_mode = old_mode

        elif mode == TimerMode.BREAK:
            self._total_break_time_ms += elapsed_ms
            self._apply_break_delta(-elapsed_ms, result)

        # QUIET/SLEEPING: no accumulation

        # Focus layer: accumulate independently when active
        if self._focus_active:
            self._total_focus_time_ms += elapsed_ms

        self._last_tick_ms = now_mono_ms
        return result

    def _check_daily_reset(
        self, now_mono_ms: int, today_date: str, current_hour: int | None = None
    ) -> TickResult | None:
        """Check and perform daily reset. Returns TickResult if reset happened."""
        if self._daily_start_date is None:
            self._daily_start_date = today_date
            return None

        if self._daily_start_date == today_date:
            return None

        if current_hour is not None and current_hour < self._reset_hour:
            return None

        # Day changed (and past reset hour) — calculate productivity score and reset
        productivity_score = max(0, self._break_balance_ms // (1000 * 60))

        result = TickResult()
        result.events.append(TimerEvent.DAILY_RESET)
        result.productivity_score = productivity_score
        result.reset_date = self._daily_start_date

        self._reset_state(now_mono_ms, today_date, with_buffer=True)
        return result

    def _reset_state(self, now_mono_ms: int, today_date: str, with_buffer: bool) -> None:
        """Reset all state for a new day."""
        self._activity = Activity.WORKING
        self._productivity_active = True
        self._clear_manual_mode()
        self._activity_substate = {"distraction_started_ms": None, "is_scrolling_gaming": False}
        self._productivity_substate = {
            "idle_entered_ms": None,
            "idle_timeout_ms": IDLE_TIMEOUT_FROM_WORKING_MS,
            "idle_timeout_exempt": self._productivity_substate.get("idle_timeout_exempt", False),
        }
        self._focus_active = False
        self._total_focus_time_ms = 0
        self._total_work_time_ms = 0
        self._total_break_time_ms = 0
        self._break_balance_ms = DEFAULT_BREAK_BUFFER_MS if with_buffer else 0
        self._daily_start_date = today_date
        self._last_tick_ms = now_mono_ms

    def _apply_break_delta(self, break_delta_ms: int, result: TickResult) -> None:
        """Apply break time change. Fires BREAK_EXHAUSTED on zero-crossing."""
        was_positive = self._break_balance_ms > 0
        self._break_balance_ms += break_delta_ms
        if was_positive and self._break_balance_ms <= 0:
            result.events.append(TimerEvent.BREAK_EXHAUSTED)
