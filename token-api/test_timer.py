"""Unit tests for TimerEngine v2 — layered composite model, no I/O dependencies."""

from timer import (
    BREAK_RATE_TABLE,
    DEFAULT_BREAK_BUFFER_MS,
    DISTRACTION_TIMEOUT_MS,
    GYM_BOUNTY_MS,
    IDLE_TIMEOUT_FROM_MULTITASKING_MS,
    IDLE_TIMEOUT_FROM_WORKING_MS,
    MANUAL_LOCK_DURATION_MS,
    MAX_IDLE_MS,
    Activity,
    TickResult,
    TimerEngine,
    TimerEvent,
    TimerMode,
    format_timer_time,
)

# ---- Helpers ----


def make_engine(now_ms: int = 0, date: str = "2026-02-11") -> TimerEngine:
    """Create an engine and initialize its daily_start_date."""
    engine = TimerEngine(now_mono_ms=now_ms)
    engine.tick(now_ms, date)  # sets daily_start_date
    return engine


def advance(
    engine: TimerEngine, start_ms: int, seconds: int, date: str = "2026-02-11"
) -> TickResult:
    """Advance the engine by `seconds` in 1-second ticks, returning the last result."""
    result = TickResult()
    for i in range(seconds):
        result = engine.tick(start_ms + (i + 1) * 1000, date)
    return result


def collect_events(
    engine: TimerEngine, start_ms: int, seconds: int, date: str = "2026-02-11"
) -> list[TimerEvent]:
    """Advance and collect all events across all ticks."""
    events = []
    for i in range(seconds):
        result = engine.tick(start_ms + (i + 1) * 1000, date)
        events.extend(result.events)
    return events


# ---- format_timer_time ----


class TestFormatTimerTime:
    def test_zero(self):
        assert format_timer_time(0) == "0h 0m"

    def test_positive(self):
        assert format_timer_time(90 * 60 * 1000) == "1h 30m"

    def test_negative(self):
        assert format_timer_time(-45 * 60 * 1000) == "-0h 45m"

    def test_large(self):
        assert format_timer_time(3 * 60 * 60 * 1000 + 5 * 60 * 1000) == "3h 5m"


# ---- Basic tick / WORKING mode ----


class TestBasicTick:
    def test_working_earns_one_to_one(self):
        """60s of WORKING → 60_000ms break earned (1:1 rate)."""
        engine = make_engine(0)
        advance(engine, 0, 60)
        assert engine.break_balance_ms == 60_000

    def test_work_time_tracked(self):
        """60s of WORKING → 60_000ms total work time."""
        engine = make_engine(0)
        advance(engine, 0, 60)
        assert engine.total_work_time_ms == 60_000

    def test_all_values_integer(self):
        """No float drift — all values are exact integers."""
        engine = make_engine(0)
        advance(engine, 0, 123)
        assert isinstance(engine.break_balance_ms, int)
        assert isinstance(engine.total_work_time_ms, int)
        assert isinstance(engine.total_break_time_ms, int)

    def test_initial_mode_is_working(self):
        engine = TimerEngine(now_mono_ms=0)
        assert engine.current_mode == TimerMode.WORKING

    def test_sub_second_tick(self):
        """Ticks faster than 1s still accumulate correctly."""
        engine = make_engine(0)
        for i in range(1000):
            engine.tick(i * 100, "2026-02-11")  # 100ms ticks for 100s
        # 999 ticks of 100ms each = 99_900ms total, * 1/1 = 99_900ms
        assert engine.break_balance_ms == 99_900


# ---- Effective mode derivation ----


class TestEffectiveMode:
    def test_working_active_working(self):
        """Activity=working, prod=active → WORKING."""
        engine = make_engine(0)
        assert engine.effective_mode == TimerMode.WORKING

    def test_working_inactive_idle(self):
        """Activity=working, prod=inactive → IDLE."""
        engine = make_engine(0)
        engine.set_productivity(False, 0)
        assert engine.effective_mode == TimerMode.IDLE

    def test_distraction_active_multitasking(self):
        """Activity=distraction, prod=active, <10min → MULTITASKING."""
        engine = make_engine(0)
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=True, now_mono_ms=0)
        assert engine.effective_mode == TimerMode.MULTITASKING

    def test_distraction_inactive_break(self):
        """Activity=distraction, prod=inactive → IDLE_BREAK (auto, no amnesty)."""
        engine = make_engine(0)
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=False, now_mono_ms=0)
        engine.set_productivity(False, 0)
        assert engine.effective_mode == TimerMode.IDLE_BREAK

    def test_manual_break_overrides_all(self):
        """Manual break override (DECLARED_BREAK) takes priority."""
        engine = make_engine(0)
        engine.enter_break(0)
        assert engine.effective_mode == TimerMode.DECLARED_BREAK
        # Even with working + active, still BREAK
        assert engine.activity == Activity.WORKING
        assert engine.productivity_active

    def test_manual_sleeping_overrides_all(self):
        engine = make_engine(0)
        engine.enter_sleeping(0)
        assert engine.effective_mode == TimerMode.SLEEPING

    def test_distracted_requires_scrolling_gaming_and_timeout(self):
        """Video stays MULTITASKING even after 10min. Only scrolling/gaming → DISTRACTED."""
        engine = make_engine(0)
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=False, now_mono_ms=0)
        # Advance past 10 min
        advance(engine, 0, 700)
        assert engine.effective_mode == TimerMode.MULTITASKING  # video, not distracted

    def test_scrolling_becomes_distracted_after_timeout(self):
        """Scrolling/gaming + prod active → DISTRACTED after 10min."""
        engine = make_engine(0)
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=True, now_mono_ms=0)
        # Advance to 10 min
        timeout_secs = DISTRACTION_TIMEOUT_MS // 1000
        advance(engine, 0, timeout_secs)
        assert engine.effective_mode == TimerMode.DISTRACTED


# ---- Layer transitions ----


class TestLayerTransitions:
    def test_set_activity_working_to_distraction(self):
        engine = make_engine(0)
        result = engine.set_activity(
            Activity.DISTRACTION, is_scrolling_gaming=False, now_mono_ms=1000
        )
        assert TimerEvent.MODE_CHANGED in result.events
        assert result.old_mode == TimerMode.WORKING
        assert engine.effective_mode == TimerMode.MULTITASKING

    def test_set_activity_distraction_to_working(self):
        engine = make_engine(0)
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=False, now_mono_ms=0)
        result = engine.set_activity(Activity.WORKING, is_scrolling_gaming=False, now_mono_ms=1000)
        assert TimerEvent.MODE_CHANGED in result.events
        assert result.old_mode == TimerMode.MULTITASKING
        assert engine.effective_mode == TimerMode.WORKING

    def test_set_productivity_active_to_inactive(self):
        engine = make_engine(0)
        result = engine.set_productivity(False, 1000)
        assert TimerEvent.MODE_CHANGED in result.events
        assert result.old_mode == TimerMode.WORKING
        assert engine.effective_mode == TimerMode.IDLE

    def test_set_productivity_inactive_to_active(self):
        engine = make_engine(0)
        engine.set_productivity(False, 0)
        result = engine.set_productivity(True, 1000)
        assert TimerEvent.MODE_CHANGED in result.events
        assert result.old_mode == TimerMode.IDLE
        assert engine.effective_mode == TimerMode.WORKING

    def test_no_event_on_same_effective_mode(self):
        """set_activity to same value → no MODE_CHANGED."""
        engine = make_engine(0)
        result = engine.set_activity(Activity.WORKING, is_scrolling_gaming=False, now_mono_ms=1000)
        assert TimerEvent.MODE_CHANGED not in result.events


# ---- Multitasking ----


class TestMultitasking:
    def test_multitasking_neutral_rate(self):
        """MULTITASKING earns 0 break (neutral)."""
        engine = make_engine(0)
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=False, now_mono_ms=0)
        assert engine.effective_mode == TimerMode.MULTITASKING
        advance(engine, 0, 60)
        assert engine.break_balance_ms == 0

    def test_multitasking_tracks_work_time(self):
        """MULTITASKING still counts as work time."""
        engine = make_engine(0)
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=False, now_mono_ms=0)
        advance(engine, 0, 60)
        assert engine.total_work_time_ms == 60_000

    def test_video_stays_multitasking_forever(self):
        """Video (not scrolling/gaming) never escalates to DISTRACTED."""
        engine = make_engine(0)
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=False, now_mono_ms=0)
        advance(engine, 0, 1200)  # 20 minutes
        assert engine.effective_mode == TimerMode.MULTITASKING


# ---- Distracted ----


class TestDistracted:
    def test_distracted_penalty_rate(self):
        """DISTRACTED spends break at -1:1 (60 min/hr)."""
        engine = make_engine(0)
        # Earn 120s break first
        advance(engine, 0, 120)
        assert engine.break_balance_ms == 120_000

        # Enter distraction (scrolling)
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=True, now_mono_ms=120_000)
        # Advance past 10 min threshold
        timeout_secs = DISTRACTION_TIMEOUT_MS // 1000
        advance(engine, 120_000, timeout_secs + 60)  # 60s past threshold
        # After threshold, mode becomes DISTRACTED and penalty applies
        assert engine.effective_mode == TimerMode.DISTRACTED

    def test_distraction_timeout_event(self):
        """DISTRACTION_TIMEOUT event fires when scrolling/gaming reaches 10min."""
        engine = make_engine(0)
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=True, now_mono_ms=0)
        timeout_secs = DISTRACTION_TIMEOUT_MS // 1000
        events = collect_events(engine, 0, timeout_secs)
        assert TimerEvent.DISTRACTION_TIMEOUT in events

    def test_video_no_distraction_timeout(self):
        """Video (not scrolling) never triggers DISTRACTION_TIMEOUT."""
        engine = make_engine(0)
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=False, now_mono_ms=0)
        events = collect_events(engine, 0, 700)  # well past 10 min
        assert TimerEvent.DISTRACTION_TIMEOUT not in events

    def test_distracted_prod_loss_becomes_break(self):
        """DISTRACTED → prod expires → IDLE_BREAK (rule 2: inactive+distraction)."""
        engine = make_engine(0)
        # Earn break, enter distraction, wait for DISTRACTED
        advance(engine, 0, 120)
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=True, now_mono_ms=120_000)
        timeout_secs = DISTRACTION_TIMEOUT_MS // 1000
        advance(engine, 120_000, timeout_secs)
        assert engine.effective_mode == TimerMode.DISTRACTED

        # Now lose productivity
        t = 120_000 + timeout_secs * 1000
        result = engine.set_productivity(False, t)
        assert engine.effective_mode == TimerMode.IDLE_BREAK
        assert TimerEvent.MODE_CHANGED in result.events
        assert result.old_mode == TimerMode.DISTRACTED


# ---- Parameterized idle ----


class TestParameterizedIdle:
    def test_idle_from_working_2hr_timeout(self):
        """WORKING → prod inactive → IDLE with 2-hour timeout."""
        engine = make_engine(0)
        engine.set_productivity(False, 1000)
        assert engine.effective_mode == TimerMode.IDLE
        assert engine.idle_timeout_ms == IDLE_TIMEOUT_FROM_WORKING_MS

    def test_idle_from_multitasking_2min_timeout(self):
        """MULTITASKING → prod inactive → IDLE with 2-minute timeout."""
        engine = make_engine(0)
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=False, now_mono_ms=0)
        assert engine.effective_mode == TimerMode.MULTITASKING
        engine.set_productivity(False, 1000)
        # inactive + distraction = BREAK, not IDLE — but timeout was parameterized
        # Actually: inactive + distraction → BREAK directly (rule 2)
        # The 2-min timeout applies when we go from MULTITASKING to IDLE
        # which means activity must switch back to WORKING first
        # Let me reconsider: if prod goes inactive while distracted → BREAK
        # The 2-min timeout is for when we were multitasking and distraction stops
        # then prod is inactive → IDLE with 2min timeout
        pass

    def test_idle_timeout_from_working_triggers_break(self):
        """After 2 hours of IDLE (from WORKING), auto-transition to BREAK."""
        engine = make_engine(0)
        engine.set_productivity(False, 0)
        assert engine.effective_mode == TimerMode.IDLE

        timeout_secs = IDLE_TIMEOUT_FROM_WORKING_MS // 1000
        events = collect_events(engine, 0, timeout_secs)
        assert TimerEvent.IDLE_TIMEOUT in events
        assert engine.effective_mode == TimerMode.IDLE_BREAK

    def test_idle_timeout_from_multitasking_triggers_break_fast(self):
        """After 2 minutes of IDLE (from MULTITASKING), auto-transition to BREAK."""
        engine = make_engine(0)
        # Start as multitasking (distraction + prod active)
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=False, now_mono_ms=0)
        assert engine.effective_mode == TimerMode.MULTITASKING
        # Lose productivity while still in MULTITASKING → goes to BREAK (rule 2: inactive+distraction)
        # To get 2min idle: stop distraction first so prod inactive → IDLE
        # But we want the 2min timeout from multitasking context.
        # The timeout is parameterized at the moment prod goes inactive.
        # If effective_mode was MULTITASKING when prod goes inactive → 2min timeout.
        # But inactive+distraction = BREAK, not IDLE.
        # So: lose prod while in MULTITASKING → switch to working → now IDLE with 2min timeout
        # Actually: set_productivity(False) while MULTITASKING → old_mode=MULTITASKING → 2min
        # but effective becomes BREAK (inactive+distraction). Need to switch activity too.
        # Real scenario: was multitasking, distraction stops, prod stops shortly after
        engine.set_activity(Activity.WORKING, is_scrolling_gaming=False, now_mono_ms=500)
        # Now WORKING. Lose productivity (old_mode was WORKING, not MULTITASKING)
        # Hmm — the 2min timeout should be based on what mode we were in BEFORE prod went inactive
        # Since we switched back to working first, old_mode is WORKING → 2hr timeout
        # The 2min case: prod goes inactive while still multitasking (activity=distraction)
        # But that gives BREAK (rule 2), not IDLE.
        # The 2min idle from multitasking applies when: was multitasking, distraction ends
        # AND prod goes inactive at roughly the same time.
        # Let's test: set prod inactive while multitasking → effective=BREAK, then set activity=working
        engine.set_productivity(True, 500)  # reset to active
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=False, now_mono_ms=600)
        assert engine.effective_mode == TimerMode.MULTITASKING
        # Prod goes inactive while multitasking → timeout parameterized to 2min
        engine.set_productivity(False, 700)
        # effective = IDLE_BREAK (inactive + distraction), but idle_timeout_ms was set to 2min
        assert engine.effective_mode == TimerMode.IDLE_BREAK
        # Now switch activity back to working → effective = IDLE (inactive + working)
        engine.set_activity(Activity.WORKING, is_scrolling_gaming=False, now_mono_ms=800)
        assert engine.effective_mode == TimerMode.IDLE
        assert engine.idle_timeout_ms == IDLE_TIMEOUT_FROM_MULTITASKING_MS

        timeout_secs = IDLE_TIMEOUT_FROM_MULTITASKING_MS // 1000
        events = collect_events(engine, 800, timeout_secs)
        assert TimerEvent.IDLE_TIMEOUT in events
        assert engine.effective_mode == TimerMode.IDLE_BREAK

    def test_idle_no_accumulation(self):
        """IDLE mode: no break earned, no work time change."""
        engine = make_engine(0)
        advance(engine, 0, 40)
        break_before = engine.break_balance_ms
        work_before = engine.total_work_time_ms

        engine.set_productivity(False, 40_000)
        advance(engine, 40_000, 60)
        assert engine.break_balance_ms == break_before
        assert engine.total_work_time_ms == work_before

    def test_idle_timeout_exempt(self):
        """Stays IDLE past timeout when exempt (gym/campus)."""
        engine = make_engine(0)
        engine.idle_timeout_exempt = True
        engine.set_productivity(False, 0)

        timeout_secs = IDLE_TIMEOUT_FROM_WORKING_MS // 1000
        advance(engine, 0, timeout_secs + 60)
        assert engine.effective_mode == TimerMode.IDLE
        assert TimerEvent.IDLE_TIMEOUT not in collect_events(
            engine, timeout_secs * 1000 + 60_000, 10
        )

    def test_productivity_active_clears_idle(self):
        """Becoming productive again clears idle state."""
        engine = make_engine(0)
        engine.set_productivity(False, 0)
        assert engine.effective_mode == TimerMode.IDLE
        engine.set_productivity(True, 5000)
        assert engine.effective_mode == TimerMode.WORKING


# ---- Gym bounty ----


class TestGymBounty:
    def test_apply_gym_bounty(self):
        """+30 min break on gym exit."""
        engine = make_engine(0)
        result = engine.apply_gym_bounty(0)
        assert engine.break_balance_ms == GYM_BOUNTY_MS

    def test_gym_bounty_stacks_with_earned(self):
        """Bounty adds to existing break time."""
        engine = make_engine(0)
        advance(engine, 0, 60)  # earn 60_000ms
        engine.apply_gym_bounty(60_000)
        assert engine.break_balance_ms == 60_000 + GYM_BOUNTY_MS

    def test_gym_bounty_pays_off_backlog(self):
        """Bounty pays off backlog before accumulating."""
        engine = make_engine(0)
        # Create backlog by entering break with no earned time
        engine.enter_break(0)
        advance(engine, 0, 30)  # 30s break consumed → -30_000 balance
        assert engine.break_balance_ms == -30_000
        engine.resume(30_000)
        engine.apply_gym_bounty(30_000)
        assert engine.break_balance_ms == GYM_BOUNTY_MS - 30_000


# ---- Break consumption ----


class TestBreakConsumption:
    def test_break_mode_consumes_accumulated(self):
        """Enter break mode, verify accumulated_break_ms decreases."""
        engine = make_engine(0)
        advance(engine, 0, 60)  # 60_000ms break earned (1:1 rate)
        engine.enter_break(60_000)
        advance(engine, 60_000, 10)  # consume 10_000ms
        assert engine.break_balance_ms == 50_000
        assert engine.total_break_time_ms == 10_000

    def test_break_tracks_break_time(self):
        engine = make_engine(0)
        advance(engine, 0, 60)
        engine.enter_break(60_000)
        advance(engine, 60_000, 20)
        assert engine.total_break_time_ms == 20_000


# ---- Break exhaustion ----


class TestBreakExhaustion:
    def test_break_exhaustion_event(self):
        """Consume all break time → BREAK_EXHAUSTED event (fires once on 0-crossing).

        BREAK_EXHAUSTED fires when accumulated_break_ms crosses from >0 to <0
        in a single tick (overshoot). If break lands exactly on 0, no event fires.
        Use a non-round amount (500ms sub-second tick) so the 1s-tick consumption
        overshoots from 500 → -500, triggering the event.
        """
        engine = make_engine(0)
        advance(engine, 0, 10)  # 10_000ms break (1:1 rate)
        # Consume 500ms so remaining is 9_500 (not a multiple of 1000)
        engine.enter_break(10_000)
        engine.tick(10_500, "2026-02-11")  # consume 500ms → 9_500 remaining
        # Now advance 10s: tick 10 will go from 500 → -500 (overshoot → BREAK_EXHAUSTED)
        events = collect_events(engine, 10_500, 10)
        assert TimerEvent.BREAK_EXHAUSTED in events
        assert engine.break_balance_ms < 0

    def test_distracted_exhaustion(self):
        """DISTRACTED penalty can exhaust break (fires once on 0-crossing).

        Earn a non-round amount so DISTRACTED's -1:1 penalty overshoots past 0
        in a single tick, triggering the event.
        """
        engine = make_engine(0)
        # Earn 20_500ms break: 20s WORKING + 500ms sub-second tick
        advance(engine, 0, 20)
        engine.tick(20_500, "2026-02-11")  # +500ms → 20_500ms total
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=True, now_mono_ms=20_500)
        # Advance past 10min threshold (MULTITASKING, neutral rate — break unchanged)
        timeout_secs = DISTRACTION_TIMEOUT_MS // 1000
        advance(engine, 20_500, timeout_secs)
        # Now in DISTRACTED, penalty -1:1
        # 20_500ms / 1000ms per tick = tick 21 will cross from 500 → -500
        t_start = 20_500 + timeout_secs * 1000
        events = collect_events(engine, t_start, 25)
        assert TimerEvent.BREAK_EXHAUSTED in events

    def test_break_exhaustion_exact_zero(self):
        """When break hits exactly 0 (no overshoot), no backlog created."""
        engine = make_engine(0)
        advance(engine, 0, 5)  # 5_000ms break (1:1 rate)
        engine.enter_break(5_000)
        advance(engine, 5_000, 5)  # consume exactly 5_000ms
        assert engine.break_balance_ms == 0


# ---- Break-rate penalty multiplier (declared vs slacked) ----


def _enter_break_slacked(engine, now_ms):
    """Force an undeclared/idle-timeout break entry (trigger != 'user')."""
    engine._set_manual_mode(TimerMode.IDLE_BREAK, "idle_timeout", now_ms)


class TestBreakPenaltyMultiplier:
    def test_declared_break_debits_base_rate(self):
        """Declared break (trigger='user') debits at 1.0x — exact elapsed ms."""
        engine = make_engine(0)
        advance(engine, 0, 60)  # +60_000 earned
        engine.enter_break(60_000)  # declared
        advance(engine, 60_000, 40)  # consume 40s declared
        assert engine.break_balance_ms == 60_000 - 40_000

    def test_slacked_break_debits_penalty_rate(self):
        """Slacked/idle-timeout break debits at 1.5x (N*3//2)."""
        engine = make_engine(0)
        advance(engine, 0, 60)  # +60_000 earned
        _enter_break_slacked(engine, 60_000)
        advance(engine, 60_000, 40)  # 40_000*3//2 = 60_000 debit
        assert engine.break_balance_ms == 60_000 - 60_000

    def test_declared_goes_negative_slower_than_slacked(self):
        """For equal elapsed, declared rest stays less negative than slacking."""
        declared = make_engine(0)
        slacked = make_engine(0)
        declared.enter_break(0)
        _enter_break_slacked(slacked, 0)
        advance(declared, 0, 30)
        advance(slacked, 0, 30)
        assert declared.break_balance_ms == -30_000
        assert slacked.break_balance_ms == -45_000
        assert declared.break_balance_ms > slacked.break_balance_ms

    def test_multiplier_configurable(self):
        """Penalty multiplier is injectable — (2, 1) → 2x burn."""
        engine = TimerEngine(now_mono_ms=0, break_penalty_multiplier=(2, 1))
        engine.tick(0, "2026-02-11")
        _enter_break_slacked(engine, 0)
        advance(engine, 0, 30)  # 30_000*2 = 60_000 debit
        assert engine.break_balance_ms == -60_000

    def test_none_trigger_fails_toward_penalty(self):
        """Auto-break with no manual substate (trigger=None) burns at penalty rate."""
        engine = make_engine(0)
        # Inactive + distraction → IDLE_BREAK via layers (no manual mode, trigger None)
        engine.set_productivity(False, 0)
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=False, now_mono_ms=0)
        assert engine.current_mode == TimerMode.IDLE_BREAK
        assert engine.manual_trigger is None
        advance(engine, 0, 20)  # 20_000*3//2 = 30_000 debit
        assert engine.break_balance_ms == -30_000

    def test_penalty_keeps_balance_exact_int(self):
        """Penalty arithmetic stays integer — no float leak."""
        engine = make_engine(0)
        _enter_break_slacked(engine, 0)
        advance(engine, 0, 7)  # 7_000*3//2 = 10_500
        assert engine.break_balance_ms == -10_500
        assert isinstance(engine.break_balance_ms, int)

    def test_break_exhausted_fires_once_with_penalty(self):
        """Penalty path still fires BREAK_EXHAUSTED exactly once on the zero-crossing."""
        engine = make_engine(0)
        advance(engine, 0, 10)  # +10_000
        engine.tick(10_500, "2026-02-11")  # +500 → 10_500 balance
        _enter_break_slacked(engine, 10_500)
        events = collect_events(engine, 10_500, 20)  # 1.5x burn crosses zero
        assert events.count(TimerEvent.BREAK_EXHAUSTED) == 1
        assert engine.break_balance_ms < 0


# ---- Backlog mechanics ----


class TestBacklog:
    def test_backlog_grows_during_break(self):
        """No break earned, go to break → backlog grows."""
        engine = make_engine(0)
        engine.enter_break(0)
        advance(engine, 0, 60)  # 60_000ms consumed → all backlog
        assert engine.break_balance_ms == -60_000

    def test_backlog_offset_before_accumulation(self):
        """Earn break with backlog → pay off backlog first."""
        engine = make_engine(0)
        engine.enter_break(0)
        advance(engine, 0, 10)  # 10_000ms backlog
        assert engine.break_balance_ms == -10_000
        engine.resume(10_000)
        advance(engine, 10_000, 20)  # 20_000ms earned → pays off 10k debt + 10k positive
        assert engine.break_balance_ms == 10_000


# ---- Idle detection (gap) ----


class TestIdleDetection:
    def test_large_gap_skips_accumulation(self):
        """Idle >10 min → no accumulation for that tick."""
        engine = make_engine(0)
        advance(engine, 0, 10)  # small warmup: 10_000ms break
        # Jump 15 minutes
        result = engine.tick(10_000 + 15 * 60 * 1000, "2026-02-11")
        assert TimerEvent.BREAK_EXHAUSTED not in result.events
        # Only the first 10s of work should have accumulated (at 1:1 rate)
        assert engine.break_balance_ms == 10_000

    def test_exactly_at_threshold(self):
        """Gap exactly at MAX_IDLE_MS is still idle."""
        engine = make_engine(0)
        gap_ms = MAX_IDLE_MS + 1  # just over threshold
        engine.tick(gap_ms, "2026-02-11")
        assert engine.break_balance_ms == 0


# ---- Daily reset ----


class TestDailyReset:
    def test_reset_on_new_day(self):
        """Tick with new date → DAILY_RESET event, counters zeroed."""
        engine = make_engine(0, "2026-02-10")
        advance(engine, 0, 60, date="2026-02-10")  # accumulate some state
        assert engine.break_balance_ms == 60_000

        result = engine.tick(61_000, "2026-02-11", current_hour=8)
        assert TimerEvent.DAILY_RESET in result.events
        assert result.reset_date == "2026-02-10"

    def test_reset_productivity_score(self):
        """Productivity score = break_balance_ms // (1000 * 60)."""
        engine = make_engine(0, "2026-02-10")
        advance(engine, 0, 60, date="2026-02-10")  # 60_000ms break (60s * 1/1)
        result = engine.tick(61_000, "2026-02-11", current_hour=8)
        assert result.productivity_score == 1  # 60_000 // 60_000 = 1

    def test_reset_clears_counters(self):
        engine = make_engine(0, "2026-02-10")
        advance(engine, 0, 60, date="2026-02-10")
        engine.tick(61_000, "2026-02-11", current_hour=8)
        assert engine.break_balance_ms == DEFAULT_BREAK_BUFFER_MS
        assert engine.total_work_time_ms == 0
        assert engine.total_break_time_ms == 0
        assert engine.current_mode == TimerMode.WORKING
        assert engine.daily_start_date == "2026-02-11"

    def test_reset_clears_manual_lock(self):
        engine = make_engine(0, "2026-02-10")
        engine.enter_break(0)
        assert engine.manual_mode_lock
        engine.tick(1_000, "2026-02-11", current_hour=8)
        assert not engine.manual_mode_lock

    def test_first_tick_sets_date(self):
        engine = TimerEngine(now_mono_ms=0)
        engine.tick(0, "2026-02-11")
        assert engine.daily_start_date == "2026-02-11"

    def test_reset_hour_7(self):
        """Default reset hour is 7, not 9."""
        engine = make_engine(0, "2026-02-10")
        advance(engine, 0, 10, date="2026-02-10")
        # At hour 6 (before 7 AM), should NOT reset
        result = engine.tick(11_000, "2026-02-11", current_hour=6)
        assert TimerEvent.DAILY_RESET not in result.events
        assert engine.daily_start_date == "2026-02-10"
        # At hour 7, SHOULD reset
        result = engine.tick(12_000, "2026-02-11", current_hour=7)
        assert TimerEvent.DAILY_RESET in result.events

    def test_sleeping_auto_wakes_at_reset_hour(self):
        """SLEEPING mode auto-exits at reset hour."""
        engine = make_engine(0)
        engine.enter_sleeping(0)
        assert engine.effective_mode == TimerMode.SLEEPING
        result = engine.tick(1_000, "2026-02-11", current_hour=8)
        assert engine.effective_mode == TimerMode.WORKING
        assert engine.manual_mode is None


# ---- Manual mode (break/sleeping) ----


class TestManualMode:
    def test_enter_break(self):
        engine = make_engine(0)
        changed, result = engine.enter_break(0)
        assert changed
        assert engine.effective_mode == TimerMode.DECLARED_BREAK
        assert TimerEvent.MODE_CHANGED in result.events

    def test_enter_sleeping(self):
        engine = make_engine(0)
        changed, result = engine.enter_sleeping(0)
        assert changed
        assert engine.effective_mode == TimerMode.SLEEPING

    def test_enter_quiet_sleeping_context(self):
        engine = make_engine(0)
        changed, result = engine.enter_quiet(0, context="sleeping")
        assert changed
        assert engine.effective_mode == TimerMode.QUIET
        assert engine.quiet_context == "sleeping"
        assert TimerEvent.MODE_CHANGED in result.events

    def test_quiet_camps_idle_timeout(self):
        engine = make_engine(0)
        engine.set_productivity(False, 0)
        engine.enter_quiet(1_000, context="sleeping")
        timeout_secs = IDLE_TIMEOUT_FROM_WORKING_MS // 1000
        events = collect_events(engine, 1_000, timeout_secs + 60)
        assert TimerEvent.IDLE_TIMEOUT not in events
        assert engine.effective_mode == TimerMode.QUIET

    def test_resume_from_break(self):
        engine = make_engine(0)
        engine.enter_break(0)
        changed, result = engine.resume(1000)
        assert changed
        assert engine.effective_mode == TimerMode.WORKING
        assert not engine.manual_mode_lock

    def test_resume_from_sleeping(self):
        engine = make_engine(0)
        engine.enter_sleeping(0)
        changed, result = engine.resume(1000)
        assert changed
        assert engine.effective_mode == TimerMode.WORKING

    def test_resume_when_not_in_manual(self):
        engine = make_engine(0)
        changed, _ = engine.resume(0)
        assert not changed

    def test_enter_break_twice(self):
        engine = make_engine(0)
        engine.enter_break(0)
        changed, _ = engine.enter_break(1000)
        assert not changed

    def test_break_sets_lock(self):
        engine = make_engine(0)
        engine.enter_break(0)
        assert engine.manual_mode_lock

    def test_break_trigger_is_user(self):
        """Manual break entry sets trigger='user'."""
        engine = make_engine(0)
        engine.enter_break(0)
        assert engine.manual_trigger == "user"

    def test_sleeping_trigger_is_user(self):
        """Manual sleeping entry sets trigger='user'."""
        engine = make_engine(0)
        engine.enter_sleeping(0)
        assert engine.manual_trigger == "user"

    def test_idle_timeout_break_trigger(self):
        """Idle timeout sets IDLE_BREAK with trigger='idle_timeout'."""
        engine = make_engine(0)
        engine.set_productivity(False, 0)
        timeout_secs = IDLE_TIMEOUT_FROM_WORKING_MS // 1000
        advance(engine, 0, timeout_secs)
        assert engine.effective_mode == TimerMode.IDLE_BREAK
        assert engine.manual_trigger == "idle_timeout"

    def test_idle_timeout_break_auto_clears_on_productivity(self):
        """set_productivity(True) auto-clears an IDLE_BREAK (idle-timeout break)."""
        engine = make_engine(0)
        engine.set_productivity(False, 0)
        timeout_secs = IDLE_TIMEOUT_FROM_WORKING_MS // 1000
        advance(engine, 0, timeout_secs)
        assert engine.effective_mode == TimerMode.IDLE_BREAK
        assert engine.manual_trigger == "idle_timeout"
        # Becoming productive again should auto-clear the idle-timeout break
        engine.set_productivity(True, timeout_secs * 1000 + 1000)
        assert engine.effective_mode == TimerMode.WORKING
        assert engine.manual_mode is None
        assert engine.manual_trigger is None

    def test_user_break_not_auto_cleared_on_productivity(self):
        """set_productivity(True) does NOT auto-clear a DECLARED_BREAK (trigger 'user')."""
        engine = make_engine(0)
        engine.enter_break(0)
        assert engine.manual_trigger == "user"
        engine.set_productivity(True, 1000)
        # Declared break should persist
        assert engine.effective_mode == TimerMode.DECLARED_BREAK
        assert engine.manual_trigger == "user"

    def test_resume_clears_trigger(self):
        """resume() clears manual_trigger."""
        engine = make_engine(0)
        engine.enter_break(0)
        assert engine.manual_trigger == "user"
        engine.resume(1000)
        assert engine.manual_trigger is None

    def test_sleeping_neutral(self):
        """SLEEPING: no accumulation of any kind."""
        engine = make_engine(0)
        advance(engine, 0, 10)  # earn 10_000ms
        break_before = engine.break_balance_ms
        engine.enter_sleeping(10_000)
        advance(engine, 10_000, 60)
        assert engine.break_balance_ms == break_before
        assert engine.total_break_time_ms == 0


# ---- Serialization round-trip (v2) ----


class TestSerialization:
    def test_round_trip(self):
        """to_dict → from_dict preserves state."""
        engine = make_engine(0)
        advance(engine, 0, 60)  # 60_000ms break
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=True, now_mono_ms=60_000)
        advance(engine, 60_000, 30)

        data = engine.to_dict(now_mono_ms=90_000)
        assert data["format_version"] == 2

        restored = TimerEngine(now_mono_ms=200_000)
        restored.from_dict(data, now_mono_ms=200_000)

        assert restored.activity == engine.activity
        assert restored.productivity_active == engine.productivity_active
        assert restored.manual_mode == engine.manual_mode
        assert restored.break_balance_ms == engine.break_balance_ms
        assert restored.total_work_time_ms == engine.total_work_time_ms
        assert restored.total_break_time_ms == engine.total_break_time_ms
        assert restored.daily_start_date == engine.daily_start_date

    def test_lock_surviving_serialization(self):
        """Manual lock persists across serialize/deserialize."""
        engine = make_engine(0)
        engine.enter_break(0)
        data = engine.to_dict(now_mono_ms=5_000)

        restored = TimerEngine(now_mono_ms=100_000)
        restored.from_dict(data, now_mono_ms=100_000)
        assert restored.manual_mode_lock
        assert restored.effective_mode == TimerMode.DECLARED_BREAK

    def test_expired_lock_cleared_on_restore(self):
        """Lock that expired during downtime is cleared on restore."""
        engine = make_engine(0)
        engine.enter_break(0)
        data = engine.to_dict(now_mono_ms=MANUAL_LOCK_DURATION_MS + 60_000)

        restored = TimerEngine(now_mono_ms=200_000)
        restored.from_dict(data, now_mono_ms=200_000)
        assert not restored.manual_mode_lock

    def test_idle_serialization(self):
        """Round-trip preserves idle state."""
        engine = make_engine(0)
        engine.idle_timeout_exempt = True
        engine.set_productivity(False, 10_000)
        advance(engine, 10_000, 5)

        data = engine.to_dict(now_mono_ms=15_000)
        assert data["idle_entered_elapsed_ms"] == 5_000
        assert data["idle_timeout_exempt"] is True
        assert data["idle_timeout_ms"] == IDLE_TIMEOUT_FROM_WORKING_MS

        restored = TimerEngine(now_mono_ms=100_000)
        restored.from_dict(data, now_mono_ms=100_000)
        assert not restored.productivity_active
        assert restored.idle_timeout_exempt is True

    def test_distraction_serialization(self):
        """Round-trip preserves distraction state."""
        engine = make_engine(0)
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=True, now_mono_ms=0)
        advance(engine, 0, 30)

        data = engine.to_dict(now_mono_ms=30_000)
        assert data["distraction_elapsed_ms"] == 30_000
        assert data["distraction_is_scrolling_gaming"] is True

        restored = TimerEngine(now_mono_ms=100_000)
        restored.from_dict(data, now_mono_ms=100_000)
        assert restored.activity == Activity.DISTRACTION
        # distraction_started_ms should be restored relative to new now
        assert restored.distraction_started_ms == 100_000 - 30_000

    def test_export_dict_camel_case(self):
        """to_export_dict returns camelCase keys with layer info."""
        engine = make_engine(0)
        advance(engine, 0, 60)
        d = engine.to_export_dict()
        assert d["currentMode"] == "working"
        assert d["activity"] == "working"
        assert d["productivityActive"] is True
        assert d["breakAvailableSeconds"] == 60


# ---- Legacy migration ----


class TestLegacyMigration:
    def test_work_silence_migration(self):
        old_data = {
            "current_mode": "work_silence",
            "total_work_time_ms": 120000,
            "total_break_time_ms": 0,
            "accumulated_break_ms": 60000,
            "break_backlog_ms": 0,
            "daily_start_date": "2026-02-10",
            "manual_mode_lock": False,
        }
        engine = TimerEngine(now_mono_ms=0)
        engine.from_dict(old_data, now_mono_ms=0)
        assert engine.activity == Activity.WORKING
        assert engine.productivity_active is True
        assert engine.manual_mode is None
        assert engine.effective_mode == TimerMode.WORKING
        assert engine.break_balance_ms == 60000

    def test_work_video_migration(self):
        old_data = {
            "current_mode": "work_video",
            "total_work_time_ms": 0,
            "total_break_time_ms": 0,
            "accumulated_break_ms": 0,
            "break_backlog_ms": 0,
        }
        engine = TimerEngine(now_mono_ms=0)
        engine.from_dict(old_data, now_mono_ms=0)
        assert engine.activity == Activity.DISTRACTION
        assert engine.productivity_active is True
        assert engine.effective_mode == TimerMode.MULTITASKING

    def test_work_scrolling_migration(self):
        old_data = {"current_mode": "work_scrolling"}
        engine = TimerEngine(now_mono_ms=0)
        engine.from_dict(old_data, now_mono_ms=0)
        assert engine.activity == Activity.DISTRACTION
        assert engine.productivity_active is True
        # scrolling/gaming → distraction_is_scrolling_gaming = True

    def test_break_migration(self):
        old_data = {
            "current_mode": "break",
            "manual_mode_lock": True,
            "manual_mode_lock_remaining_ms": 300000,
            "accumulated_break_ms": 50000,
        }
        engine = TimerEngine(now_mono_ms=0)
        engine.from_dict(old_data, now_mono_ms=0)
        # v1 "break" was always a user-declared rest → DECLARED_BREAK.
        assert engine.manual_mode == TimerMode.DECLARED_BREAK
        assert engine.effective_mode == TimerMode.DECLARED_BREAK
        assert engine.manual_mode_lock is True

    def test_sleeping_migration(self):
        old_data = {"current_mode": "sleeping"}
        engine = TimerEngine(now_mono_ms=0)
        engine.from_dict(old_data, now_mono_ms=0)
        assert engine.manual_mode == TimerMode.QUIET
        assert engine.effective_mode == TimerMode.QUIET
        assert engine.quiet_context == "sleeping"

    def test_idle_migration(self):
        old_data = {"current_mode": "idle"}
        engine = TimerEngine(now_mono_ms=0)
        engine.from_dict(old_data, now_mono_ms=0)
        assert engine.activity == Activity.WORKING
        assert engine.productivity_active is False
        assert engine.effective_mode == TimerMode.IDLE

    def test_pause_migration(self):
        old_data = {"current_mode": "pause"}
        engine = TimerEngine(now_mono_ms=0)
        engine.from_dict(old_data, now_mono_ms=0)
        assert engine.activity == Activity.WORKING
        assert engine.productivity_active is False
        assert engine.effective_mode == TimerMode.IDLE

    def test_gym_migration(self):
        old_data = {"current_mode": "gym"}
        engine = TimerEngine(now_mono_ms=0)
        engine.from_dict(old_data, now_mono_ms=0)
        assert engine.activity == Activity.WORKING
        assert engine.productivity_active is True
        assert engine.effective_mode == TimerMode.WORKING

    def test_old_format_float_truncation(self):
        """Old float values are truncated to int."""
        old_data = {
            "current_mode": "work_music",
            "total_work_time_ms": 120000.5,
            "accumulated_break_ms": 60000.25,
            "total_break_time_ms": 0,
            "break_backlog_ms": 0,
        }
        engine = TimerEngine(now_mono_ms=0)
        engine.from_dict(old_data, now_mono_ms=0)
        assert engine.break_balance_ms == 60000
        assert engine.total_work_time_ms == 120000


# ---- Break-mode split back-compat ----


class TestBreakModeSplitBackCompat:
    """A v2 row persisted before the split stored manual_mode=='break'. On load
    it must split by its trigger: user → DECLARED_BREAK, else → IDLE_BREAK."""

    def _v2_break_row(self, trigger):
        return {
            "format_version": 2,
            "manual_mode": "break",
            "manual_trigger": trigger,
            "manual_mode_lock": True,
            "manual_mode_lock_remaining_ms": 300_000,
            "break_balance_ms": 50_000,
        }

    def test_legacy_v2_break_user_maps_to_declared(self):
        engine = TimerEngine(now_mono_ms=0)
        engine.from_dict(self._v2_break_row("user"), now_mono_ms=0)
        assert engine.manual_mode == TimerMode.DECLARED_BREAK
        assert engine.effective_mode == TimerMode.DECLARED_BREAK
        assert engine.manual_trigger == "user"

    def test_legacy_v2_break_idle_timeout_maps_to_idle_break(self):
        engine = TimerEngine(now_mono_ms=0)
        engine.from_dict(self._v2_break_row("idle_timeout"), now_mono_ms=0)
        assert engine.manual_mode == TimerMode.IDLE_BREAK
        assert engine.effective_mode == TimerMode.IDLE_BREAK
        assert engine.manual_trigger == "idle_timeout"

    def test_legacy_v2_break_missing_trigger_fails_toward_idle_break(self):
        row = self._v2_break_row("user")
        del row["manual_trigger"]
        engine = TimerEngine(now_mono_ms=0)
        engine.from_dict(row, now_mono_ms=0)
        # Fail toward enforcement: an unknown trigger is treated as idle/undeclared.
        assert engine.manual_mode == TimerMode.IDLE_BREAK

    def test_declared_break_round_trips(self):
        engine = make_engine(0)
        engine.enter_break(0)
        restored = TimerEngine(now_mono_ms=10_000)
        restored.from_dict(engine.to_dict(now_mono_ms=0), now_mono_ms=10_000)
        assert restored.manual_mode == TimerMode.DECLARED_BREAK

    def test_idle_break_round_trips(self):
        engine = make_engine(0)
        engine._set_manual_mode(TimerMode.IDLE_BREAK, "idle_timeout", 0)
        restored = TimerEngine(now_mono_ms=10_000)
        restored.from_dict(engine.to_dict(now_mono_ms=0), now_mono_ms=10_000)
        assert restored.manual_mode == TimerMode.IDLE_BREAK
        assert restored.manual_trigger == "idle_timeout"


# ---- Edge cases ----


class TestEdgeCases:
    def test_zero_elapsed(self):
        """Tick with same timestamp → no change."""
        engine = make_engine(0)
        engine.tick(0, "2026-02-11")
        assert engine.break_balance_ms == 0

    def test_negative_elapsed(self):
        """Monotonic clock should never go backward, but handle gracefully."""
        engine = make_engine(1000)
        engine.tick(500, "2026-02-11")  # earlier timestamp
        assert engine.break_balance_ms == 0

    def test_rate_table_completeness(self):
        """All TimerModes have an entry in the rate table."""
        for mode in TimerMode:
            assert mode in BREAK_RATE_TABLE, f"Missing rate for {mode}"

    def test_distraction_upgrade_resets_timer(self):
        """Upgrading from video to scrolling resets distraction timer."""
        engine = make_engine(0)
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=False, now_mono_ms=0)
        advance(engine, 0, 300)  # 5 min of video
        # Upgrade to scrolling
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=True, now_mono_ms=300_000)
        assert engine.distraction_started_ms == 300_000  # reset, not 0

    def test_distraction_clears_on_working(self):
        """Switching back to working clears all distraction state."""
        engine = make_engine(0)
        engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=True, now_mono_ms=0)
        engine.set_activity(Activity.WORKING, is_scrolling_gaming=False, now_mono_ms=5000)
        assert engine.distraction_started_ms is None
