"""
Cron Engine: APScheduler-based job scheduler with quiet hours, budgets, and audit trail.

Replaces OpenClaw's cron system with a local, controllable engine that stores
job definitions and run history in agents.db.
"""

import asyncio
import json
import os
import re
import signal
import sqlite3
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from nas_mount import ensure_command_mounts
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# Timezone handling
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo


VICTORY_RE = re.compile(r'##IMPERIUM_VICTORIOUS:\s*(.+?)##', re.DOTALL)

# Parse model and prompt_path from legacy monolithic command strings
_CMD_PARSE_RE = re.compile(
    r'claude\s+--model\s+(\S+)\s+-p\s+"\$\(cat\s+([^)]+)\)"\s+--dangerously-skip-permissions'
)

# Default prompt directory (migrated from ~/.openclaw/workspace/memory/prompts/)
_PROMPTS_DIR = str(Path.home() / ".claude" / "prompts")

# Ensure critical paths are available to subprocess shells.
# LaunchAgent environments have a minimal PATH; this guarantees
# claude, openclaw, and homebrew tools are reachable.
_HOME = str(Path.home())
_EXTRA_PATHS = [
    f"{_HOME}/Scripts/cli-tools/bin",
    f"{_HOME}/.local/bin",
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
]


def _subprocess_env(**extras) -> dict:
    """Build environment dict for subprocess shells with full PATH."""
    env = dict(os.environ)
    current_path = env.get("PATH", "")
    for p in reversed(_EXTRA_PATHS):
        if p not in current_path:
            current_path = f"{p}:{current_path}"
    env["PATH"] = current_path
    env["HOME"] = _HOME
    env.update(extras)
    return env


def _now_iso() -> str:
    return datetime.now().isoformat()


def _parse_interval(value: str) -> dict:
    """Parse interval string like '15m', '2h', '30s' into trigger kwargs."""
    unit = value[-1].lower()
    amount = int(value[:-1])
    if unit == "s":
        return {"seconds": amount}
    elif unit == "m":
        return {"minutes": amount}
    elif unit == "h":
        return {"hours": amount}
    elif unit == "d":
        return {"days": amount}
    raise ValueError(f"Unknown interval unit: {value}")


class CronEngine:
    """Manages cron jobs via APScheduler with DB-backed state and run history."""

    def __init__(self, scheduler: AsyncIOScheduler, db_path: Path):
        self.scheduler = scheduler
        self.db_path = db_path
        self._running_jobs: dict[str, asyncio.subprocess.Process] = {}

    # ── DB Schema ──────────────────────────────────────────────

    @staticmethod
    async def init_tables(db: aiosqlite.Connection):
        """Create cron tables. Called from main init_db()."""
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cron_jobs (
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                description TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1,
                schedule_type TEXT NOT NULL,
                schedule_value TEXT NOT NULL,
                timezone TEXT DEFAULT 'America/Phoenix',
                command TEXT NOT NULL,
                timeout_seconds INTEGER DEFAULT 120,
                quiet_hours_start INTEGER,
                quiet_hours_end INTEGER,
                max_runs_per_window INTEGER,
                run_window_hours INTEGER DEFAULT 5,
                session_type TEXT DEFAULT 'isolated',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cron_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES cron_jobs(id),
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                skip_reason TEXT,
                duration_seconds REAL,
                exit_code INTEGER,
                output_summary TEXT,
                error_summary TEXT,
                victory_reason TEXT DEFAULT NULL,
                created_at TEXT NOT NULL
            )
        """)
        # Index for common queries
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_cron_runs_job_id
            ON cron_runs(job_id, started_at DESC)
        """)

        # Migrations: add new columns to existing tables if absent
        cursor = await db.execute("PRAGMA table_info(cron_jobs)")
        cron_jobs_cols = {row[1] for row in await cursor.fetchall()}
        if "guards_count" not in cron_jobs_cols:
            await db.execute("ALTER TABLE cron_jobs ADD COLUMN guards_count INTEGER DEFAULT 0")
        if "followup_delay_seconds" not in cron_jobs_cols:
            await db.execute("ALTER TABLE cron_jobs ADD COLUMN followup_delay_seconds INTEGER DEFAULT NULL")
        if "notify_discord" not in cron_jobs_cols:
            await db.execute("ALTER TABLE cron_jobs ADD COLUMN notify_discord INTEGER DEFAULT 0")
        if "commander" not in cron_jobs_cols:
            await db.execute("ALTER TABLE cron_jobs ADD COLUMN commander TEXT DEFAULT 'mechanicus'")
        if "model" not in cron_jobs_cols:
            await db.execute("ALTER TABLE cron_jobs ADD COLUMN model TEXT DEFAULT NULL")
        if "prompt_path" not in cron_jobs_cols:
            await db.execute("ALTER TABLE cron_jobs ADD COLUMN prompt_path TEXT DEFAULT NULL")
        if "active_session_id" not in cron_jobs_cols:
            await db.execute("ALTER TABLE cron_jobs ADD COLUMN active_session_id TEXT DEFAULT NULL")
        if "session_started_date" not in cron_jobs_cols:
            await db.execute("ALTER TABLE cron_jobs ADD COLUMN session_started_date TEXT DEFAULT NULL")
        if "victory_conditions" not in cron_jobs_cols:
            await db.execute("ALTER TABLE cron_jobs ADD COLUMN victory_conditions TEXT DEFAULT NULL")
        if "legion" not in cron_jobs_cols:
            await db.execute("ALTER TABLE cron_jobs ADD COLUMN legion TEXT DEFAULT 'mechanicus'")

        cursor = await db.execute("PRAGMA table_info(cron_runs)")
        cron_runs_cols = {row[1] for row in await cursor.fetchall()}
        if "victory_reason" not in cron_runs_cols:
            await db.execute("ALTER TABLE cron_runs ADD COLUMN victory_reason TEXT DEFAULT NULL")

        await db.commit()

        # One-time data migration: extract model/prompt_path from command strings
        cursor = await db.execute(
            "SELECT id, command FROM cron_jobs WHERE model IS NULL AND command LIKE 'claude %'"
        )
        rows = await cursor.fetchall()
        for row_id, cmd in rows:
            m = _CMD_PARSE_RE.match(cmd)
            if m:
                model = m.group(1)
                prompt = m.group(2).strip()
                # Rewrite legacy openclaw path to canonical ~/.claude/prompts/
                prompt = prompt.replace(
                    "~/.openclaw/workspace/memory/prompts/",
                    "~/.claude/prompts/"
                )
                await db.execute(
                    "UPDATE cron_jobs SET model = ?, prompt_path = ? WHERE id = ?",
                    (model, prompt, row_id),
                )
        if rows:
            await db.commit()
            print(f"CronEngine: Migrated {len(rows)} jobs to model/prompt_path columns")

    # ── Startup Cleanup ────────────────────────────────────────

    async def recover_orphaned_runs(self):
        """Mark any 'running' records as 'orphaned' on startup.

        These are runs whose process was lost when the server restarted.
        Without this, stuck records accumulate and the FG detects false positives.
        """
        now = _now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM cron_runs WHERE status = 'running'"
            )
            count = (await cursor.fetchone())[0]
            if count:
                await db.execute("""
                    UPDATE cron_runs
                    SET status = 'orphaned',
                        finished_at = ?,
                        error_summary = 'Process lost on server restart — run never completed'
                    WHERE status = 'running'
                """, (now,))
                await db.commit()
                print(f"CronEngine: Orphaned {count} stale 'running' record(s) from previous session")

    # ── Load / Sync ────────────────────────────────────────────

    # The deep reserve. Alpharius is the ONLY seeded job — everything else lives
    # exclusively in the DB, managed by the API. If FG, Custodes, or any other job
    # is deleted, Alpharius detects it and alerts. The Emperor rebuilds from there.
    _PERMANENT_JOBS = [
        {
            "id": "a1pha-r1us-0000-0000-hydra-dominatus",
            "name": "alpharius-heartbeat",
            "commander": "alpharius",
            "description": "Deep reserve watchdog. Monitors fleet health, alerts on catastrophic failure. I am Alpharius.",
            "enabled": True,
            "schedule": {"type": "cron", "value": "*/30 * * * *", "tz": "America/Phoenix"},
            "command": "cd /mnt/imperium/Scripts/token-api && python3 alpharius_heartbeat.py",
            "timeout_seconds": 60,
        },
    ]

    async def ensure_permanent_jobs(self):
        """Seed permanent jobs into DB on first boot only (INSERT OR IGNORE).

        The DB is authoritative. This only fires for jobs not yet present —
        it never overwrites live state. Edit _PERMANENT_JOBS when a command
        or schedule truly changes; the next fresh-DB boot will pick it up.
        """
        now = _now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            for job_def in self._PERMANENT_JOBS:
                schedule = job_def["schedule"]
                quiet = job_def.get("quiet_hours")
                await db.execute("""
                    INSERT OR IGNORE INTO cron_jobs (
                        id, name, description, enabled,
                        schedule_type, schedule_value, timezone,
                        command, timeout_seconds,
                        quiet_hours_start, quiet_hours_end,
                        max_runs_per_window, run_window_hours,
                        session_type, notify_discord, commander,
                        model, prompt_path,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    job_def["id"], job_def["name"],
                    job_def.get("description", ""),
                    1 if job_def.get("enabled", True) else 0,
                    schedule["type"], schedule["value"],
                    schedule.get("tz", "America/Phoenix"),
                    job_def.get("command", ""),
                    job_def.get("timeout_seconds", 120),
                    quiet[0] if quiet else None,
                    quiet[1] if quiet else None,
                    job_def.get("max_runs_per_window"),
                    job_def.get("run_window_hours", 5),
                    job_def.get("session_type", "isolated"),
                    1 if job_def.get("notify_discord") else 0,
                    job_def.get("commander", "mechanicus"),
                    job_def.get("model"),
                    job_def.get("prompt_path"),
                    now, now,
                ))
            await db.commit()

        await self._register_all()

    async def _register_all(self):
        """Register all enabled jobs with APScheduler."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM cron_jobs WHERE enabled = 1"
            )
            jobs = await cursor.fetchall()

        for job in jobs:
            self._register_job(dict(job))

    def _register_job(self, job: dict):
        """Register a single job with APScheduler."""
        job_id = job["id"]
        try:
            trigger = self._build_trigger(job)
            self.scheduler.add_job(
                self._run_wrapper,
                trigger=trigger,
                args=[job_id],
                id=f"cron_{job_id}",
                replace_existing=True,
                name=job["name"],
            )
            print(f"CronEngine: Registered '{job['name']}' ({job['schedule_type']}: {job['schedule_value']})")
        except Exception as e:
            print(f"CronEngine: Failed to register '{job['name']}': {e}")

    def _build_trigger(self, job: dict):
        """Build APScheduler trigger from job definition."""
        tz = ZoneInfo(job.get("timezone", "America/Phoenix"))
        if job["schedule_type"] == "interval":
            kwargs = _parse_interval(job["schedule_value"])
            return IntervalTrigger(**kwargs, timezone=tz)
        elif job["schedule_type"] == "cron":
            parts = job["schedule_value"].split()
            if len(parts) != 5:
                raise ValueError(f"Invalid cron expression: {job['schedule_value']}")
            return CronTrigger(
                minute=parts[0], hour=parts[1],
                day=parts[2], month=parts[3],
                day_of_week=parts[4], timezone=tz,
            )
        raise ValueError(f"Unknown schedule type: {job['schedule_type']}")

    # ── Execution ──────────────────────────────────────────────

    async def _run_wrapper(self, job_id: str, bypass_enabled: bool = False):
        """Entry point called by APScheduler. Checks guards, then executes.
        If bypass_enabled=True, skip the disabled check (used by manual trigger)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM cron_jobs WHERE id = ?", (job_id,))
            job = await cursor.fetchone()
            if not job:
                print(f"CronEngine: Job {job_id} not found in DB")
                return
            job = dict(job)

        # Guard: disabled (skipped for manual triggers)
        if not bypass_enabled and not job.get("enabled"):
            await self._log_skip(job_id, "disabled")
            return

        # Guard: already running
        if job_id in self._running_jobs:
            await self._log_skip(job_id, "already_running")
            return

        # Guard: instance mutex — previous claude instance for this job still live
        if not await self._check_instance_mutex(job):
            await self._log_skip(job_id, "instance_mutex")
            return

        # Guard: quiet hours
        if not self._check_quiet_hours(job):
            await self._log_skip(job_id, "quiet_hours")
            return

        # Guard: run quota
        if not await self._check_quota(job):
            await self._log_skip(job_id, "quota_exceeded")
            return

        await self._execute(job)

    def _check_quiet_hours(self, job: dict) -> bool:
        """Return True if job is allowed to run now (not in quiet hours)."""
        start = job.get("quiet_hours_start")
        end = job.get("quiet_hours_end")
        if start is None or end is None:
            return True

        tz = ZoneInfo(job.get("timezone", "America/Phoenix"))
        now_hour = datetime.now(tz).hour

        # Handle wrap-around (e.g., 22-8 means quiet from 10pm to 8am)
        if start > end:
            return not (now_hour >= start or now_hour < end)
        else:
            return not (start <= now_hour < end)

    async def _check_quota(self, job: dict) -> bool:
        """Return True if job hasn't exceeded its run quota for the current window."""
        max_runs = job.get("max_runs_per_window")
        if not max_runs:
            return True

        window_hours = job.get("run_window_hours", 5)
        cutoff = (datetime.now() - timedelta(hours=window_hours)).isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                SELECT COUNT(*) FROM cron_runs
                WHERE job_id = ? AND started_at > ? AND status IN ('ok', 'error', 'timeout', 'orphaned')
            """, (job["id"], cutoff))
            count = (await cursor.fetchone())[0]

        return count < max_runs

    async def _build_claude_command(self, job: dict) -> str:
        """Build claude CLI command, handling session persistence.

        session_type values:
          'isolated'   — fresh instance every run (default, original behavior)
          'persistent' — resume same session indefinitely across runs
          'daily'      — persistent within a day, fresh session each morning
        """
        model = job["model"]
        prompt_path = job["prompt_path"]
        session_type = job.get("session_type", "isolated")
        active_session_id = job.get("active_session_id")
        session_started_date = job.get("session_started_date")
        today = datetime.now().strftime("%Y-%m-%d")

        base = f'claude --model {model} -p "$(cat {prompt_path})" --dangerously-skip-permissions'

        if session_type == "isolated":
            return base

        # For persistent/daily: determine if we resume or start fresh
        needs_new_session = False

        if not active_session_id:
            needs_new_session = True
        elif session_type == "daily" and session_started_date != today:
            needs_new_session = True

        if needs_new_session:
            new_session_id = str(uuid.uuid4())
            # Store the session ID and date in the DB before spawning
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE cron_jobs SET active_session_id = ?, session_started_date = ?, updated_at = ? WHERE id = ?",
                    (new_session_id, today, _now_iso(), job["id"]),
                )
                await db.commit()
            print(f"CronEngine: '{job['name']}' new {session_type} session: {new_session_id[:8]}...")
            return f'{base} --session-id {new_session_id}'
        else:
            # Resume existing session — prompt is injected as the -p message
            print(f"CronEngine: '{job['name']}' resuming session: {active_session_id[:8]}...")
            return f'claude -p "$(cat {prompt_path})" --resume {active_session_id} --dangerously-skip-permissions'

    async def _execute(self, job: dict):
        """Run the job command as a subprocess with timeout."""
        job_id = job["id"]
        started_at = _now_iso()
        run_id = None

        # Insert running record
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                INSERT INTO cron_runs (job_id, started_at, status, created_at)
                VALUES (?, ?, 'running', ?)
            """, (job_id, started_at, started_at))
            run_id = cursor.lastrowid
            await db.commit()

        status = "ok"
        exit_code = None
        output_summary = ""
        error_summary = ""
        import time as _time
        start_time = _time.monotonic()

        # Discord trigger notification (log on start)
        if job.get("notify_discord"):
            try:
                subprocess.run(
                    ["discord", "send", "fleet", f"🔄 **{job['name']}**: started"],
                    timeout=8, env=_subprocess_env(),
                )
            except Exception as e:
                print(f"CronEngine: Discord trigger notify failed for '{job['name']}': {e}")

        # Build command from structured fields if available
        if job.get("prompt_path") and job.get("model"):
            command = await self._build_claude_command(job)
        else:
            command = job["command"]

        # NAS availability check — attempt remount before giving up
        nas_ok, nas_err = await asyncio.get_event_loop().run_in_executor(
            None, ensure_command_mounts, command
        )
        if not nas_ok:
            status = "nas_unavailable"
            error_summary = nas_err
            print(f"CronEngine: '{job['name']}' skipped — {nas_err}")
            # Alert fleet channel once so the issue is visible
            try:
                subprocess.run(
                    ["discord", "send", "fleet",
                     f"⚠️ **{job['name']}** skipped: {nas_err}"],
                    timeout=8, env=_subprocess_env(),
                )
            except Exception:
                pass
            # Jump straight to DB update in finally block
            raise RuntimeError(nas_err)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_subprocess_env(
                    CRON_JOB_NAME=job["name"],
                    CRON_JOB_ID=job_id,
                    TOKEN_API_SUBAGENT=f"cron:{job['name']}",
                ),
                start_new_session=True,
            )
            self._running_jobs[job_id] = proc

            # Start independent read tasks so data is accumulated regardless of
            # whether proc.wait() times out. Unlike communicate(), these tasks
            # are not cancelled when wait_for(proc.wait()) times out.
            read_stdout = asyncio.create_task(proc.stdout.read())
            read_stderr = asyncio.create_task(proc.stderr.read())

            try:
                await asyncio.wait_for(
                    proc.wait(),
                    timeout=job.get("timeout_seconds", 120),
                )
                exit_code = proc.returncode
                stdout = await read_stdout
                stderr = await read_stderr
                output_summary = (stdout or b"").decode("utf-8", errors="replace")[-4000:]
                error_summary = (stderr or b"").decode("utf-8", errors="replace")[-4000:]
                status = "ok" if exit_code == 0 else "error"
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
                # Pipes close after killpg; read tasks should complete promptly.
                try:
                    stdout = await asyncio.wait_for(read_stdout, timeout=5)
                    stderr = await asyncio.wait_for(read_stderr, timeout=5)
                except asyncio.TimeoutError:
                    read_stdout.cancel()
                    read_stderr.cancel()
                    stdout, stderr = b"", b""
                output_summary = (stdout or b"").decode("utf-8", errors="replace")[-4000:]
                error_summary = (stderr or b"").decode("utf-8", errors="replace")[-2000:]
                status = "timeout"
                error_summary = f"Killed after {job.get('timeout_seconds', 120)}s timeout\n" + error_summary

        except Exception as e:
            if status != "nas_unavailable":  # don't overwrite NAS-specific status
                status = "error"
                error_summary = str(e)[:4000]

        finally:
            duration = _time.monotonic() - start_time
            finished_at = _now_iso()

            # Detect victory signal
            victory_match = VICTORY_RE.search(output_summary)
            victory_reason = victory_match.group(1).strip() if victory_match else None

            try:
                async with aiosqlite.connect(self.db_path) as db:
                    await db.execute("""
                        UPDATE cron_runs SET
                            finished_at = ?, status = ?, duration_seconds = ?,
                            exit_code = ?, output_summary = ?, error_summary = ?,
                            victory_reason = ?
                        WHERE id = ?
                    """, (finished_at, status, round(duration, 2),
                          exit_code, output_summary, error_summary,
                          victory_reason, run_id))
                    await db.commit()
            except Exception as db_err:
                print(f"CronEngine: DB update failed for '{job['name']}': {db_err}")
            finally:
                # Pop AFTER DB write so is_running stays true until DB is consistent
                self._running_jobs.pop(job_id, None)

            print(f"CronEngine: '{job['name']}' finished: {status} ({duration:.1f}s)")

            # Discord completion notification (with substance)
            if job.get("notify_discord"):
                emoji = "✅" if status == "ok" else ("⏱️" if status == "timeout" else "❌")
                msg = f"{emoji} **{job['name']}**: {status} ({duration:.0f}s)"
                if victory_reason:
                    msg += f"\n> {victory_reason}"
                try:
                    subprocess.run(
                        ["discord", "send", "fleet", msg],
                        timeout=8, env=_subprocess_env(),
                    )
                except Exception as e:
                    print(f"CronEngine: Discord notify failed for '{job['name']}': {e}")

            # Post-run: victory handling or follow-up scheduling
            if status == "ok":
                if victory_reason:
                    asyncio.create_task(self._handle_victory(job, run_id, victory_reason))
                elif job.get("followup_delay_seconds"):
                    delay = job["followup_delay_seconds"]
                    async def _delayed_followup(jid=job_id, d=delay):
                        await asyncio.sleep(d)
                        await self._run_wrapper(jid)
                    asyncio.create_task(_delayed_followup())

            # Post-run graph (guards + victory chain via LangGraph)
            guards_count = job.get("guards_count", 0)
            followup_delay = job.get("followup_delay_seconds")
            if guards_count or followup_delay:
                try:
                    from post_run_graph import post_run_graph
                    asyncio.create_task(post_run_graph.ainvoke({
                        "job_id": job_id,
                        "job_name": job["name"],
                        "cron_run_id": run_id,
                        "full_output": output_summary,
                        "guards_count": guards_count or 0,
                        "followup_delay_seconds": followup_delay,
                        "victory_reason": victory_reason,
                        "guard_results": [],
                        "followup_scheduled": False,
                    }))
                except ImportError:
                    pass  # post_run_graph not yet installed

    async def handle_victory(self, job: dict, run_id: int, reason: str):
        """Fire Discord victory notification when an agent raises the victory banner.
        Called internally on VICTORY_RE detection or externally via POST /api/cron/jobs/{id}/victory."""
        msg = f"⚔️ **IMPERIUM VICTORIOUS** — {job['name']}\n> {reason}"
        try:
            subprocess.run(
                ["discord", "send", "fleet", msg],
                timeout=10,
                env=_subprocess_env(),
            )
        except Exception as e:
            print(f"CronEngine: Victory Discord notify failed: {e}")
        print(f"CronEngine: '{job['name']}' declared victory: {reason}")

    # Keep private alias for backwards compatibility
    _handle_victory = handle_victory

    async def _check_instance_mutex(self, job: dict) -> bool:
        """Return True if no live claude instance exists for this job.

        Cron workers tag their instance with spawner='cron:<job_name>' via
        TOKEN_API_SUBAGENT. If a previous run's instance is still alive
        (status != 'stopped'), skip this run to avoid pileup.
        """
        spawner_prefix = f"cron:{job['name']}"
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM claude_instances WHERE spawner LIKE ? AND status != 'stopped'",
                (f"{spawner_prefix}%",),
            )
            count = (await cursor.fetchone())[0]
        if count > 0:
            print(
                f"CronEngine: '{job['name']}' SKIPPED: mutex — "
                f"previous instance still running ({count} live)"
            )
        return count == 0

    async def _log_skip(self, job_id: str, reason: str):
        """Record a skipped run."""
        now = _now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO cron_runs (job_id, started_at, finished_at, status, skip_reason, duration_seconds, created_at)
                VALUES (?, ?, ?, 'skipped', ?, 0, ?)
            """, (job_id, now, now, reason, now))
            await db.commit()
        print(f"CronEngine: Job {job_id} skipped ({reason})")

    # ── CRUD ───────────────────────────────────────────────────

    async def get_jobs(self) -> list[dict]:
        """Get all cron jobs with next run time."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM cron_jobs ORDER BY name")
            jobs = [dict(row) for row in await cursor.fetchall()]

        # Enrich with scheduler info
        for job in jobs:
            sched_job = self.scheduler.get_job(f"cron_{job['id']}")
            if sched_job and sched_job.next_run_time:
                job["next_run_at"] = sched_job.next_run_time.isoformat()
            else:
                job["next_run_at"] = None
            job["is_running"] = job["id"] in self._running_jobs
            # Deserialize victory_conditions JSON
            vc = job.get("victory_conditions")
            if vc and isinstance(vc, str):
                try:
                    job["victory_conditions"] = json.loads(vc)
                except json.JSONDecodeError:
                    pass

        return jobs

    async def get_job(self, job_id: str) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM cron_jobs WHERE id = ?", (job_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            job = dict(row)

        sched_job = self.scheduler.get_job(f"cron_{job_id}")
        if sched_job and sched_job.next_run_time:
            job["next_run_at"] = sched_job.next_run_time.isoformat()
        else:
            job["next_run_at"] = None
        job["is_running"] = job_id in self._running_jobs
        # Deserialize victory_conditions JSON
        vc = job.get("victory_conditions")
        if vc and isinstance(vc, str):
            try:
                job["victory_conditions"] = json.loads(vc)
            except json.JSONDecodeError:
                pass
        return job

    async def load_from_config(self, config_path: Path) -> list[dict]:
        """Load (or reload) cron jobs from a JSON config file.

        Each entry in the JSON array is upserted by name: created if new,
        updated if a job with that name already exists.  Missing file is a
        no-op (returns empty list).
        """
        if not Path(config_path).exists():
            return []

        try:
            entries = json.loads(Path(config_path).read_text())
        except Exception as e:
            print(f"CronEngine: load_from_config failed to parse {config_path}: {e}")
            return []

        results = []
        for entry in entries:
            name = entry.get("name")
            if not name:
                continue
            # Find existing job by name
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT id FROM cron_jobs WHERE name = ?", (name,))
                row = await cursor.fetchone()

            if row:
                job = await self.update_job(row["id"], entry)
            else:
                job = await self.create_job(entry)
            results.append(job)

        return results

    VALID_COMMANDERS = {"mechanicus", "custodes", "alpharius", "dorn", "emperor"}

    async def create_job(self, data: dict) -> dict:
        """Create a new cron job."""
        job_id = str(uuid.uuid4())
        now = _now_iso()
        schedule = data["schedule"]
        quiet = data.get("quiet_hours")
        commander = data.get("commander", "mechanicus")
        if commander not in self.VALID_COMMANDERS:
            raise ValueError(f"Invalid commander '{commander}'. Must be one of: {sorted(self.VALID_COMMANDERS)}")

        model = data.get("model")
        prompt_path = data.get("prompt_path")
        command = data.get("command", "")

        # If structured fields provided without a raw command, store empty command
        if not command and not (model and prompt_path):
            raise ValueError("Either 'command' or both 'model' and 'prompt_path' are required")

        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT INTO cron_jobs (
                        id, name, description, enabled,
                        schedule_type, schedule_value, timezone,
                        command, timeout_seconds,
                        quiet_hours_start, quiet_hours_end,
                        max_runs_per_window, run_window_hours,
                        session_type, commander,
                        model, prompt_path,
                        victory_conditions,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    job_id, data["name"],
                    data.get("description", ""),
                    1 if data.get("enabled", True) else 0,
                    schedule["type"], schedule["value"],
                    schedule.get("tz", "America/Phoenix"),
                    command,
                    data.get("timeout_seconds", 120),
                    quiet[0] if quiet else None,
                    quiet[1] if quiet else None,
                    data.get("max_runs_per_window"),
                    data.get("run_window_hours", 5),
                    data.get("session_type", "isolated"),
                    commander,
                    model, prompt_path,
                    json.dumps(data["victory_conditions"]) if data.get("victory_conditions") else None,
                    now, now,
                ))
                await db.commit()
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed: cron_jobs.name" in str(e):
                raise ValueError(f"A job named '{data['name']}' already exists")
            raise

        job = await self.get_job(job_id)
        if data.get("enabled", True):
            self._register_job(job)
        return job

    async def update_job(self, job_id: str, updates: dict) -> Optional[dict]:
        """Update a cron job. Re-registers with scheduler if schedule changed."""
        if "commander" in updates and updates["commander"] not in self.VALID_COMMANDERS:
            raise ValueError(f"Invalid commander '{updates['commander']}'. Must be one of: {sorted(self.VALID_COMMANDERS)}")

        job = await self.get_job(job_id)
        if not job:
            return None

        set_clauses = []
        params = []

        field_map = {
            "name": "name", "description": "description",
            "enabled": "enabled", "command": "command",
            "timeout_seconds": "timeout_seconds",
            "quiet_hours_start": "quiet_hours_start",
            "quiet_hours_end": "quiet_hours_end",
            "max_runs_per_window": "max_runs_per_window",
            "run_window_hours": "run_window_hours",
            "session_type": "session_type",
            "commander": "commander",
            "model": "model",
            "prompt_path": "prompt_path",
            "active_session_id": "active_session_id",
            "session_started_date": "session_started_date",
            "victory_conditions": "victory_conditions",
        }

        for key, col in field_map.items():
            if key in updates:
                set_clauses.append(f"{col} = ?")
                val = updates[key]
                # JSON-serialize victory_conditions
                if key == "victory_conditions" and val is not None and not isinstance(val, str):
                    val = json.dumps(val)
                params.append(val)

        # Handle schedule sub-object
        if "schedule" in updates:
            sched = updates["schedule"]
            if "type" in sched:
                set_clauses.append("schedule_type = ?")
                params.append(sched["type"])
            if "value" in sched:
                set_clauses.append("schedule_value = ?")
                params.append(sched["value"])
            if "tz" in sched:
                set_clauses.append("timezone = ?")
                params.append(sched["tz"])

        if not set_clauses:
            return job

        set_clauses.append("updated_at = ?")
        params.append(_now_iso())
        params.append(job_id)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE cron_jobs SET {', '.join(set_clauses)} WHERE id = ?",
                params,
            )
            await db.commit()

        updated = await self.get_job(job_id)

        # Re-register or remove from scheduler
        sched_id = f"cron_{job_id}"
        if updated["enabled"]:
            self._register_job(updated)
        else:
            try:
                self.scheduler.remove_job(sched_id)
            except Exception:
                pass

        return updated

    async def delete_job(self, job_id: str) -> bool:
        """Delete a cron job and its run history."""
        try:
            self.scheduler.remove_job(f"cron_{job_id}")
        except Exception:
            pass

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM cron_runs WHERE job_id = ?", (job_id,))
            cursor = await db.execute("DELETE FROM cron_jobs WHERE id = ?", (job_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def trigger_job(self, job_id: str, dry_run: bool = False, delay_seconds: int = 0) -> dict:
        """Manually trigger a job, bypassing schedule (but respecting quiet hours and quota).
        If dry_run=True, check all guards and log but don't execute the command.
        If delay_seconds > 0, schedule execution in the future instead of running immediately."""
        job = await self.get_job(job_id)
        if not job:
            return {"error": "Job not found"}

        if dry_run:
            return await self._dry_run(job)

        if delay_seconds > 0:
            async def _delayed_run():
                await asyncio.sleep(delay_seconds)
                await self._run_wrapper(job_id, bypass_enabled=True)
            asyncio.create_task(_delayed_run())
            return {"triggered": True, "job": job["name"], "delay_seconds": delay_seconds}

        asyncio.create_task(self._run_wrapper(job_id, bypass_enabled=True))
        return {"triggered": True, "job": job["name"]}

    async def _dry_run(self, job: dict) -> dict:
        """Simulate a job run: check all guards, log result, don't execute."""
        job_id = job["id"]
        now = _now_iso()
        tz = ZoneInfo(job.get("timezone", "America/Phoenix"))
        current_hour = datetime.now(tz).hour

        checks = {
            "quiet_hours": self._check_quiet_hours(job),
            "quota": await self._check_quota(job),
            "enabled": bool(job.get("enabled")),
            "not_running": job_id not in self._running_jobs,
        }
        would_run = all(checks.values())

        # Build descriptive output
        details = []
        details.append(f"Job: {job['name']} ({job_id})")
        if job.get("prompt_path") and job.get("model"):
            details.append(f"Model: {job['model']}")
            details.append(f"Prompt: {job['prompt_path']}")
        else:
            details.append(f"Command: {job['command']}")
        details.append(f"Current hour ({tz}): {current_hour}")
        quiet_s, quiet_e = job.get("quiet_hours_start"), job.get("quiet_hours_end")
        if quiet_s is not None:
            details.append(f"Quiet hours: {quiet_s}-{quiet_e} → {'BLOCKED' if not checks['quiet_hours'] else 'clear'}")
        max_runs = job.get("max_runs_per_window")
        if max_runs:
            details.append(f"Quota: {max_runs}/{job.get('run_window_hours', 5)}h → {'BLOCKED' if not checks['quota'] else 'clear'}")
        details.append(f"Enabled: {checks['enabled']}")
        details.append(f"Would run: {'YES' if would_run else 'NO'}")

        output = "\n".join(details)

        # Log as a dry_run in the audit trail
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO cron_runs (job_id, started_at, finished_at, status, skip_reason, duration_seconds, output_summary, created_at)
                VALUES (?, ?, ?, 'dry_run', ?, 0, ?, ?)
            """, (job_id, now, now,
                  None if would_run else "would_be_blocked",
                  output, now))
            await db.commit()

        print(f"CronEngine: '{job['name']}' dry-run: would_run={would_run}")
        return {
            "dry_run": True,
            "job": job["name"],
            "would_run": would_run,
            "checks": checks,
            "details": output,
        }

    async def get_runs(self, job_id: str, limit: int = 20) -> list[dict]:
        """Get recent run history for a job."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT * FROM cron_runs
                WHERE job_id = ?
                ORDER BY started_at DESC
                LIMIT ?
            """, (job_id, limit))
            return [dict(row) for row in await cursor.fetchall()]

    async def get_status(self) -> dict:
        """Overall cron engine status."""
        jobs = await self.get_jobs()
        enabled = [j for j in jobs if j["enabled"]]
        running = [j for j in jobs if j.get("is_running")]

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM cron_runs WHERE started_at > ?",
                ((datetime.now() - timedelta(hours=24)).isoformat(),)
            )
            runs_24h = (await cursor.fetchone())[0]

        return {
            "total_jobs": len(jobs),
            "enabled": len(enabled),
            "running": len(running),
            "runs_last_24h": runs_24h,
            "jobs": jobs,
        }

    # ── Pause / Unpause ──────────────────────────────────────

    async def pause_fleet(self, commanders: list[str] | None = None) -> dict:
        """Pause cron jobs by disabling them and storing which were enabled.

        Args:
            commanders: List of commander factions to pause. If None, pauses all.

        Returns:
            Dict with paused job names and count.
        """
        now = _now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            if commanders:
                placeholders = ",".join("?" for _ in commanders)
                cursor = await db.execute(
                    f"SELECT id, name FROM cron_jobs WHERE enabled = 1 AND commander IN ({placeholders})",
                    commanders,
                )
            else:
                cursor = await db.execute(
                    "SELECT id, name FROM cron_jobs WHERE enabled = 1"
                )
            enabled_jobs = await cursor.fetchall()

            if not enabled_jobs:
                return {"paused": [], "count": 0, "message": "No enabled jobs to pause"}

            job_ids = [row[0] for row in enabled_jobs]
            job_names = [row[1] for row in enabled_jobs]

            # Store the paused set in a simple table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS fleet_pause_state (
                    job_id TEXT NOT NULL,
                    paused_at TEXT NOT NULL
                )
            """)
            # Clear any previous pause state
            await db.execute("DELETE FROM fleet_pause_state")
            for jid in job_ids:
                await db.execute(
                    "INSERT INTO fleet_pause_state (job_id, paused_at) VALUES (?, ?)",
                    (jid, now),
                )

            # Disable all paused jobs
            placeholders = ",".join("?" for _ in job_ids)
            await db.execute(
                f"UPDATE cron_jobs SET enabled = 0, updated_at = ? WHERE id IN ({placeholders})",
                [now] + job_ids,
            )
            await db.commit()

        # Remove from scheduler
        for jid in job_ids:
            try:
                self.scheduler.remove_job(f"cron_{jid}")
            except Exception:
                pass

        print(f"CronEngine: Fleet paused — {len(job_ids)} jobs disabled: {', '.join(job_names)}")
        return {"paused": job_names, "count": len(job_ids)}

    async def unpause_fleet(self) -> dict:
        """Unpause cron jobs by re-enabling those that were paused.

        Returns:
            Dict with unpaused job names and count.
        """
        now = _now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            # Check if pause state exists
            try:
                cursor = await db.execute("SELECT job_id FROM fleet_pause_state")
                paused_ids = [row[0] for row in await cursor.fetchall()]
            except Exception:
                return {"unpaused": [], "count": 0, "message": "No pause state found — fleet was not paused"}

            if not paused_ids:
                return {"unpaused": [], "count": 0, "message": "No pause state found — fleet was not paused"}

            # Re-enable paused jobs
            placeholders = ",".join("?" for _ in paused_ids)
            await db.execute(
                f"UPDATE cron_jobs SET enabled = 1, updated_at = ? WHERE id IN ({placeholders})",
                [now] + paused_ids,
            )

            # Get names for response
            cursor = await db.execute(
                f"SELECT name FROM cron_jobs WHERE id IN ({placeholders})",
                paused_ids,
            )
            job_names = [row[0] for row in await cursor.fetchall()]

            # Clear pause state
            await db.execute("DELETE FROM fleet_pause_state")
            await db.commit()

        # Re-register with scheduler
        for jid in paused_ids:
            job = await self.get_job(jid)
            if job and job["enabled"]:
                self._register_job(job)

        print(f"CronEngine: Fleet unpaused — {len(paused_ids)} jobs re-enabled: {', '.join(job_names)}")
        return {"unpaused": job_names, "count": len(paused_ids)}
