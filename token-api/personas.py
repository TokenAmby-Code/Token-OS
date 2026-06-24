"""Canonical persona registry and assignment helpers.

The ``personas`` SQLite table is the runtime authority for display identity,
TTS/sound settings, and pane tinting. Legacy ``profile_name``/voice-pool call
sites are bridged here until the instances table grows ``persona_id``/``rank``.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

PERSONA_NAMESPACE = uuid.UUID("9b3dd7fa-27f3-5bc4-9498-36f6f8aa79b5")


@dataclass(frozen=True)
class PersonaSeed:
    slug: str
    display_name: str
    default_rank: str
    assignment_pool: str | None
    assignment_order: int | None
    pane_tint: str | None
    chip_color: str | None
    tts_voice: str | None
    tts_rate: str | None
    notification_sound: str | None
    # Symbolic session-doc binding policy resolved at stamp time (NOT a stored
    # path). ``'daily_note'`` → the persona co-binds today's shared
    # ``Terra/Journal/Daily/YYYY-MM-DD.md``; ``None`` → no persona default (the
    # dispatch/interactive resolver decides). Trailing + defaulted so the many
    # positional Astartes/Primarch seeds below need no change.
    default_session_doc: str | None = None

    @property
    def id(self) -> str:
        return str(uuid.uuid5(PERSONA_NAMESPACE, self.slug))


PRIMARY_ASTARTES: tuple[PersonaSeed, ...] = (
    PersonaSeed(
        "blood-angels",
        "Blood Angels",
        "astartes",
        "primary",
        10,
        "#300808",
        "#b1191e",
        "Microsoft Ravi",
        "1",
        "notify.wav",
    ),
    PersonaSeed(
        "ultramarines",
        "Ultramarines",
        "astartes",
        "primary",
        20,
        "#081c30",
        "#1f4e9b",
        "Microsoft Susan",
        "1",
        "notify.wav",
    ),
    PersonaSeed(
        "salamanders",
        "Salamanders",
        "astartes",
        "primary",
        30,
        "#082810",
        "#1b7a3d",
        "Microsoft Sean",
        "0",
        "chord.wav",
    ),
    PersonaSeed(
        "imperial-fists",
        "Imperial Fists",
        "astartes",
        "primary",
        40,
        "#302800",
        "#e6b800",
        "Microsoft Catherine",
        "1",
        "ding.wav",
    ),
    PersonaSeed(
        "raven-guard",
        "Raven Guard",
        "astartes",
        "primary",
        50,
        "#101010",
        "#2b2b2b",
        "Microsoft Heera",
        "1",
        "chimes.wav",
    ),
)

BACKUP_ASTARTES: tuple[PersonaSeed, ...] = (
    PersonaSeed(
        "space-wolves",
        "Space Wolves",
        "astartes",
        "backup",
        110,
        "default",
        "#7f8fa6",
        "Microsoft David",
        "1",
        "tada.wav",
    ),
    PersonaSeed(
        "dark-angels",
        "Dark Angels",
        "astartes",
        "backup",
        120,
        "default",
        "#0b3d2e",
        "Microsoft Zira",
        "1",
        "chord.wav",
    ),
    PersonaSeed(
        "white-scars",
        "White Scars",
        "astartes",
        "backup",
        130,
        "default",
        "#f0f0f0",
        "Microsoft Mark",
        "1",
        "recycle.wav",
    ),
)

ULTIMATE_ASTARTES = PersonaSeed(
    "deathwatch",
    "Deathwatch",
    "astartes",
    None,
    None,
    "default",
    "#1c1c1c",
    "Microsoft David",
    "1",
    "chimes.wav",
)

# Retirement/quarantine persona used when an Astartes chapter child is banished.
# It deliberately has no assignment pool, so moving a row here releases the
# original chapter/persona lock without putting Black Shields into rotation.
BLACK_SHIELDS = PersonaSeed(
    "black-shields",
    "Black Shields",
    "astartes",
    None,
    None,
    "default",
    "#111111",
    None,
    None,
    None,
)

# Civic worker persona for the koronus/pax-stack worker agents (the right-side
# stack), analogous to how mechanicus workers are astartes. ``astartes`` rank so
# it is resolvable like a chapter, but with NO assignment pool so it never enters
# the rotating Astartes auto-assignment — koronus workers resolve it by slug. It
# only applies while ON the koronus page; a civic worker started off-page falls
# through to the normal astartes assignment instead.
CIVIC_WORKER_PERSONAS: tuple[PersonaSeed, ...] = (
    PersonaSeed(
        "agentic-worker",
        "Agentic Worker",
        "astartes",
        None,
        None,  # assignment_pool, assignment_order — out of the rotation pool
        "#23323f",  # pane_tint  — civic slate (lighter), deliberately non-40k
        "#5a8fb5",  # chip_color — civic blue accent
        None,
        None,
        None,  # tts_voice, tts_rate, notification_sound (silent)
    ),
)

SINGLETON_PERSONAS: tuple[PersonaSeed, ...] = (
    PersonaSeed(
        "custodes",
        "Custodes",
        "overseer",
        None,
        None,
        "#302800",
        "#d4af37",
        "Microsoft George",
        "2",
        "chimes.wav",
        default_session_doc="daily_note",
    ),
    PersonaSeed(
        "fabricator-general",
        "Fabricator-General",
        "overseer",
        None,
        None,
        "#300808",
        "#8b1a1a",
        None,
        None,
        None,
        default_session_doc="daily_note",
    ),
    PersonaSeed(
        "administratum",
        "Administratum",
        "overseer",
        None,
        None,
        "#300808",
        "#6f1d1d",
        None,
        None,
        None,
        default_session_doc="daily_note",
    ),
    PersonaSeed(
        "inquisitor", "Inquisitor", "overseer", None, None, "#180830", "#7a4cc2", None, None, None
    ),
    PersonaSeed(
        "pax",
        "Pax",
        "overseer",
        None,
        None,  # assignment_pool, assignment_order (overseer → None)
        "#1c2b3a",  # pane_tint  — civic slate, deliberately non-40k
        "#3a6ea5",  # chip_color — civic blue
        None,
        None,
        None,  # tts_voice, tts_rate, notification_sound (silent seat)
    ),
    PersonaSeed(
        "orchestrator",
        "Orchestrator",
        "overseer",
        None,
        None,  # assignment_pool, assignment_order (overseer → None)
        "#14302a",  # pane_tint  — civic teal, deliberately non-40k
        "#2f9e8f",  # chip_color — civic teal accent
        None,
        None,
        None,  # tts_voice, tts_rate, notification_sound (silent seat)
    ),
)

PRIMARCH_PERSONAS: tuple[PersonaSeed, ...] = (
    PersonaSeed("vulkan", "Vulkan", "primarch", None, None, "#302000", "#d46a00", None, None, None),
    PersonaSeed(
        "guilliman", "Guilliman", "primarch", None, None, "#081c30", "#1f4e9b", None, None, None
    ),
    PersonaSeed(
        "sanguinius", "Sanguinius", "primarch", None, None, "#300808", "#b1191e", None, None, None
    ),
    PersonaSeed(
        "alpharius", "Alpharius", "primarch", None, None, "#082c30", "#2f9e9e", None, None, None
    ),
    PersonaSeed("dorn", "Dorn", "primarch", None, None, "#302800", "#e6b800", None, None, None),
    PersonaSeed("corax", "Corax", "primarch", None, None, "#101010", "#5f6368", None, None, None),
    PersonaSeed(
        "perturabo", "Perturabo", "primarch", None, None, "#202020", "#7f8c8d", None, None, None
    ),
    PersonaSeed(
        "mechanicus", "Mechanicus", "primarch", None, None, "#300808", "#8b1a1a", None, None, None
    ),
    PersonaSeed(
        "malcador", "Malcador", "primarch", None, None, "#302810", "#8a7a4a", None, None, None
    ),
)

# ── Mechanicus worker — the voiceless shared coat ───────────────────────────
# Declared 2026-06-10 (Terra/Ultramar/Mechanicus Worker Persona). NOT an Astartes
# chapter and NOT a singleton: a SHARED, non-exclusive role coat worn by any
# number of silent Fabricator-General subagents at once. ``default_rank='astartes'``
# is the deliberate encoding — that is the discriminator the singleton guard
# (``db_schema.trg_instances_singleton_guard``) reads as "non-singleton, no
# lock-and-retire", so many instances may hold this persona simultaneously. It is
# kept OUT of the Astartes voice mutex by carrying no ``assignment_pool`` and
# ``tts_voice=None`` (voiceless): never drawn by ``assign_astartes_persona`` /
# ``selectable_astartes_personas``, so it provably never squats a chapter voice
# slot. Uniform mechanicus tint so the silent FG cohort reads as one visual block;
# instances differ only by tab_name + pane. The biconditional that binds it to the
# FG commander lives in ``db_schema`` (write triggers) + ``validate_mechanicus_invariant``.
MECHANICUS_WORKER = PersonaSeed(
    "mechanicus-worker",
    "Mechanicus Worker",
    "astartes",
    None,
    None,
    "#300808",
    "#8b1a1a",
    None,
    None,
    None,
)

PERSONA_SEEDS: tuple[PersonaSeed, ...] = (
    *SINGLETON_PERSONAS,
    *PRIMARCH_PERSONAS,
    MECHANICUS_WORKER,
    *PRIMARY_ASTARTES,
    *BACKUP_ASTARTES,
    ULTIMATE_ASTARTES,
    BLACK_SHIELDS,
    *CIVIC_WORKER_PERSONAS,
)
SEED_BY_SLUG = {seed.slug: seed for seed in PERSONA_SEEDS}
SEED_BY_ID = {seed.id: seed for seed in PERSONA_SEEDS}

MAC_VOICE_BY_TTS = {
    "Microsoft Ravi": "Rishi",
    "Microsoft Susan": "Karen",
    "Microsoft Sean": "Moira",
    "Microsoft Catherine": "Karen",
    "Microsoft Heera": "Rishi",
    "Microsoft David": "Daniel",
    "Microsoft Zira": "Karen",
    "Microsoft Mark": "Daniel",
    "Microsoft George": "Daniel",
}


def persona_id_for_slug(slug: str) -> str:
    return str(uuid.uuid5(PERSONA_NAMESPACE, slug))


def persona_schema_sql() -> str:
    return """
        CREATE TABLE IF NOT EXISTS personas (
            id TEXT PRIMARY KEY,
            slug TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            default_rank TEXT NOT NULL CHECK (default_rank IN ('astartes','primarch','overseer')),
            assignment_pool TEXT CHECK (assignment_pool IN ('primary','backup') OR assignment_pool IS NULL),
            assignment_order INTEGER,
            pane_tint TEXT,
            chip_color TEXT,
            tts_voice TEXT,
            tts_rate TEXT,
            notification_sound TEXT,
            default_session_doc TEXT,
            CHECK (default_rank = 'astartes' OR assignment_pool IS NULL)
        )
    """


def seed_params(seed: PersonaSeed) -> tuple:
    return (
        seed.id,
        seed.slug,
        seed.display_name,
        seed.default_rank,
        seed.assignment_pool,
        seed.assignment_order,
        seed.pane_tint,
        seed.chip_color,
        seed.tts_voice,
        seed.tts_rate,
        seed.notification_sound,
        seed.default_session_doc,
    )


UPSERT_SQL = """
    INSERT INTO personas (
        id, slug, display_name, default_rank, assignment_pool, assignment_order,
        pane_tint, chip_color, tts_voice, tts_rate, notification_sound,
        default_session_doc
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        slug=excluded.slug,
        display_name=excluded.display_name,
        default_rank=excluded.default_rank,
        assignment_pool=excluded.assignment_pool,
        assignment_order=excluded.assignment_order,
        pane_tint=excluded.pane_tint,
        chip_color=excluded.chip_color,
        tts_voice=excluded.tts_voice,
        tts_rate=excluded.tts_rate,
        notification_sound=excluded.notification_sound,
        default_session_doc=excluded.default_session_doc
"""


# Additive column migrations for the personas table. ``persona_schema_sql`` uses
# ``CREATE TABLE IF NOT EXISTS``, so a column added after a DB's first boot would
# never land without an explicit ALTER. Each entry is ``(column_name, alter_sql)``
# and is applied iff the column is absent — idempotent, additive house style
# (mirrors the session_documents migration in db_schema). The UPSERT seed that
# follows then backfills values onto existing rows.
_PERSONA_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("default_session_doc", "ALTER TABLE personas ADD COLUMN default_session_doc TEXT"),
)


def _migrate_persona_columns_sync(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(personas)").fetchall()}
    for column_name, sql in _PERSONA_COLUMN_MIGRATIONS:
        if column_name not in existing:
            conn.execute(sql)


async def _migrate_persona_columns(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA table_info(personas)")
    existing = {row[1] for row in await cursor.fetchall()}
    for column_name, sql in _PERSONA_COLUMN_MIGRATIONS:
        if column_name not in existing:
            await db.execute(sql)


def ensure_personas_table_sync(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(persona_schema_sql())
        _migrate_persona_columns_sync(conn)
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_personas_assignment
            ON personas(default_rank, assignment_pool, assignment_order)
            """
        )
        seed_personas_sync_conn(conn)
        conn.commit()


async def ensure_personas_table(db: aiosqlite.Connection) -> None:
    await db.execute(persona_schema_sql())
    await _migrate_persona_columns(db)
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_personas_assignment
        ON personas(default_rank, assignment_pool, assignment_order)
        """
    )
    await seed_personas(db)


def seed_personas_sync_conn(conn: sqlite3.Connection) -> None:
    conn.executemany(UPSERT_SQL, [seed_params(seed) for seed in PERSONA_SEEDS])


async def seed_personas(db: aiosqlite.Connection) -> None:
    await db.executemany(UPSERT_SQL, [seed_params(seed) for seed in PERSONA_SEEDS])


def _row_to_dict(row) -> dict | None:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        data = dict(row)
    elif hasattr(row, "keys"):
        data = {key: row[key] for key in row.keys()}
    else:
        keys = (
            "id",
            "slug",
            "display_name",
            "default_rank",
            "assignment_pool",
            "assignment_order",
            "pane_tint",
            "chip_color",
            "tts_voice",
            "tts_rate",
            "notification_sound",
            "default_session_doc",
        )
        data = dict(zip(keys, row, strict=False))
    data["silent"] = data.get("tts_voice") is None
    return data


def resolve_persona_sync(db_path: Path, persona_id_or_slug: str) -> dict | None:
    ensure_personas_table_sync(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM personas WHERE id = ? OR slug = ?",
            (persona_id_or_slug, persona_id_or_slug),
        ).fetchone()
    return _row_to_dict(row)


async def resolve_persona(db: aiosqlite.Connection, persona_id_or_slug: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM personas WHERE id = ? OR slug = ?", (persona_id_or_slug, persona_id_or_slug)
    )
    return _row_to_dict(await cursor.fetchone())


async def resolve_live_persona_instance(db: aiosqlite.Connection, slug: str) -> dict | None:
    """Live singleton instance row for a persona, resolved by identity + rank.

    Custodes (and later FG/Administratum) are THE persona by virtue of
    ``personas.slug`` + a non-``retired`` ``instances.rank`` — never by sync mode.
    ``synced``/``instance_type``/``golden_throne='sync'`` are runtime *modes*, not
    identity (see ``instance_registry.golden_throne_binding``). Returns the
    highest-rank, then most-recently-active non-retired, non-stopped/archived row
    for the persona (rank is the primary sort so a correctly-stamped overseer wins
    over a stale ``astartes`` row even if the latter is more recently active), or
    ``None`` when no such instance is alive.

    Note: the ``instances`` table normalizes ``processing``→``working`` (see
    ``instance_registry.normalize_status``), so the active set is expressed as
    "not stopped/archived" rather than an ``IN ('idle','processing')`` allowlist
    that would never match a live row.

    ``commander_type != 'chapter'`` isolates the singleton orchestrator from its
    chapter children (subagents that share the persona_id) — the same definition
    the singleton-guard and rank-stamp triggers use. Without it, a more-recently
    active subagent would shadow the overseer (live archive.db had four custodes
    chapter children under the overseer).

    Does not depend on the caller's ``row_factory``: the fixed column list is
    rebuilt by position so a plain tuple and an :class:`aiosqlite.Row` both work.
    """
    cursor = await db.execute(
        """
        SELECT i.id, i.device_id, i.status, i.rank
        FROM instances i
        JOIN personas p ON p.id = i.persona_id
        WHERE p.slug = ?
          AND i.rank != 'retired'
          AND i.commander_type != 'chapter'
          AND i.status NOT IN ('stopped', 'archived')
        ORDER BY
          CASE i.rank
            WHEN 'primarch' THEN 3
            WHEN 'overseer' THEN 2
            WHEN 'astartes' THEN 1
            ELSE 0
          END DESC,
          i.last_activity DESC,
          i.created_at DESC,
          i.id DESC
        LIMIT 1
        """,
        (slug,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return {"id": row[0], "device_id": row[1], "status": row[2], "rank": row[3]}


async def persona_tint_for_instance(
    db: aiosqlite.Connection, instance_id: str | None, *, default: str = "default"
) -> str:
    """Resolve tmux pane tint from canonical ``instances.persona_id``.

    Legacy ``legion`` and ``profile_name`` are deliberately not consulted here.
    Unassigned/civic rows with no canonical persona resolve to tmux default.
    """
    if not instance_id:
        return default
    cursor = await db.execute(
        """
        SELECT p.pane_tint
        FROM instances i
        LEFT JOIN personas p ON p.id = i.persona_id
        WHERE i.id = ?
        """,
        (instance_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return default
    tint = row[0]
    return tint if tint else default


def persona_tint_for_instance_sync(
    conn: sqlite3.Connection, instance_id: str | None, *, default: str = "default"
) -> str:
    """Synchronous variant of :func:`persona_tint_for_instance`."""
    if not instance_id:
        return default
    row = conn.execute(
        """
        SELECT p.pane_tint
        FROM instances i
        LEFT JOIN personas p ON p.id = i.persona_id
        WHERE i.id = ?
        """,
        (instance_id,),
    ).fetchone()
    if not row:
        return default
    tint = row[0]
    return tint if tint else default


async def repair_legacy_instance_personas(db: aiosqlite.Connection) -> int:
    """Compatibility repair for legacy-shaped test/extraction fixtures.

    The live legacy instance table is gone; callers that still exercise this
    repair path operate through the temporary ``legacy_instances`` projection.
    Update instance persona/voice fields in place.
    """
    cursor = await db.execute(
        """SELECT i.id, p.slug, i.persona_id, i.tts_voice, i.notification_sound
           FROM instances i
           LEFT JOIN personas p ON p.id = i.persona_id
           WHERE i.status NOT IN ('stopped', 'archived')
             AND (
               i.persona_id IS NULL
               OR p.slug IN ('profile_1','p','emperors-children')
               OR i.tts_voice IS NOT p.tts_voice
               OR i.notification_sound IS NOT p.notification_sound
             )
           ORDER BY i.id"""
    )
    repaired = 0
    for instance_id, slug, persona_id, _tts_voice, _notification_sound in await cursor.fetchall():
        target_slug = slug
        if target_slug in {"profile_1", "p", "emperors-children"}:
            target_slug = "blood-angels"
        if not target_slug and persona_id is None:
            target_slug = "blood-angels"
        if not target_slug:
            continue
        persona = await resolve_persona(db, target_slug)
        if not persona:
            continue
        profile = persona_to_profile(persona)
        await db.execute(
            """UPDATE instances
               SET persona_id = ?, tts_voice = ?, notification_sound = ?
               WHERE id = ?""",
            (
                persona["id"],
                profile["wsl_voice"],
                profile["notification_sound"],
                instance_id,
            ),
        )
        repaired += 1
    return repaired


def assignment_exhausted(persona: dict) -> bool:
    return persona.get("assignment_pool") != "primary"


def assign_astartes_persona_from_rows(
    rows: Sequence[dict], active_non_retired_persona_ids: Iterable[str]
) -> tuple[dict, bool]:
    locked = set(active_non_retired_persona_ids)
    primary = [r for r in rows if r.get("assignment_pool") == "primary"]
    backup = [r for r in rows if r.get("assignment_pool") == "backup"]
    for row in (*primary, *backup):
        if row["id"] not in locked:
            return row, assignment_exhausted(row)
    deathwatch = next((r for r in rows if r.get("slug") == "deathwatch"), None)
    if deathwatch is None:
        raise RuntimeError("personas registry missing deathwatch overflow row")
    return deathwatch, True


async def _table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        return set()
    try:
        # PRAGMA table_info does not accept a bind parameter for the table name;
        # the identifier is validated above before interpolation.
        cursor = await db.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in await cursor.fetchall()}
    except (aiosqlite.Error, sqlite3.Error):
        return set()


async def active_non_retired_persona_ids(db: aiosqlite.Connection) -> set[str]:
    """Return DB-locked personas for active, non-retired instance rows."""
    locked: set[str] = set()
    instance_cols = await _table_columns(db, "instances")
    if {"persona_id", "rank"}.issubset(instance_cols):
        cursor = await db.execute(
            """
            SELECT DISTINCT persona_id
            FROM instances
            WHERE persona_id IS NOT NULL
              AND COALESCE(rank, '') != 'retired'
              AND COALESCE(status, 'active') NOT IN ('stopped', 'closed', 'archived')
            """
        )
        locked.update(row[0] for row in await cursor.fetchall() if row[0])
    return locked


async def assign_astartes_persona(
    db: aiosqlite.Connection, active_ids: Iterable[str] | None = None
) -> tuple[dict, bool]:
    if active_ids is None:
        active_ids = await active_non_retired_persona_ids(db)
    cursor = await db.execute(
        """
        SELECT * FROM personas
        WHERE default_rank = 'astartes'
        ORDER BY CASE assignment_pool WHEN 'primary' THEN 0 WHEN 'backup' THEN 1 ELSE 2 END,
                 assignment_order IS NULL,
                 assignment_order,
                 slug
        """
    )
    rows = [_row_to_dict(row) for row in await cursor.fetchall()]
    return assign_astartes_persona_from_rows(rows, active_ids)


def persona_to_profile(persona: dict) -> dict:
    """Compatibility projection for legacy ``profile`` call sites."""
    tts_rate = persona.get("tts_rate")
    try:
        wsl_rate = int(tts_rate) if tts_rate is not None else None
    except (TypeError, ValueError):
        wsl_rate = None
    wsl_voice = persona.get("tts_voice")
    return {
        "id": persona.get("id"),
        "name": persona.get("slug"),
        "slug": persona.get("slug"),
        "chapter": persona.get("display_name"),
        "display_name": persona.get("display_name"),
        "default_rank": persona.get("default_rank"),
        "assignment_pool": persona.get("assignment_pool"),
        "assignment_order": persona.get("assignment_order"),
        "wsl_voice": wsl_voice,
        "wsl_rate": wsl_rate,
        "mac_voice": MAC_VOICE_BY_TTS.get(wsl_voice),
        "notification_sound": persona.get("notification_sound"),
        "color": persona.get("chip_color"),
        "chip_color": persona.get("chip_color"),
        "pane_tint": persona.get("pane_tint"),
        "tts_voice": wsl_voice,
        "tts_rate": persona.get("tts_rate"),
        "silent": wsl_voice is None,
    }


def seed_profile(seed: PersonaSeed) -> dict:
    return persona_to_profile(
        {
            "id": seed.id,
            "slug": seed.slug,
            "display_name": seed.display_name,
            "default_rank": seed.default_rank,
            "assignment_pool": seed.assignment_pool,
            "assignment_order": seed.assignment_order,
            "pane_tint": seed.pane_tint,
            "chip_color": seed.chip_color,
            "tts_voice": seed.tts_voice,
            "tts_rate": seed.tts_rate,
            "notification_sound": seed.notification_sound,
        }
    )


PRIMARY_PROFILES = [seed_profile(seed) for seed in PRIMARY_ASTARTES]
BACKUP_PROFILES = [seed_profile(seed) for seed in BACKUP_ASTARTES]
ULTIMATE_FALLBACK_PROFILE = seed_profile(ULTIMATE_ASTARTES)
PERSONA_COMPAT_PROFILES = [seed_profile(seed) for seed in (*SINGLETON_PERSONAS, *PRIMARCH_PERSONAS)]
ALL_COMPAT_PROFILES = [
    *PRIMARY_PROFILES,
    *BACKUP_PROFILES,
    ULTIMATE_FALLBACK_PROFILE,
    seed_profile(BLACK_SHIELDS),
    *PERSONA_COMPAT_PROFILES,
]
PROFILE_BY_SLUG = {p["name"]: p for p in ALL_COMPAT_PROFILES}


def profile_by_tts_voice(tts_voice: str | None, *, default_rank: str | None = None) -> dict | None:
    """Resolve a seeded persona compatibility profile by its Windows TTS voice.

    ``tts_voice`` is not globally unique forever (the Deathwatch overflow uses
    the same emergency voice as a backup chapter), so results are ordered the
    same way assignment is ordered: primary Astartes, backup Astartes, overflow,
    then singleton/Primarch compatibility profiles.
    """
    if not tts_voice:
        return None
    for profile in ALL_COMPAT_PROFILES:
        if profile.get("wsl_voice") != tts_voice:
            continue
        if default_rank and profile.get("default_rank") != default_rank:
            continue
        return profile
    return None


def voice_settings_for_tts_voice(tts_voice: str | None) -> dict:
    """Return TTS playback settings for a seeded persona voice.

    Unknown voices keep the historical safe defaults so direct notification
    overrides still work, but normal queued/runtime instance voices resolve
    through the persona registry projection instead of hand-iterating pools.
    """
    profile = profile_by_tts_voice(tts_voice)
    return {
        "wsl_voice": tts_voice,
        "mac_voice": (profile or {}).get("mac_voice") or "Daniel",
        "wsl_rate": (profile or {}).get("wsl_rate") or 0,
        "profile": profile,
    }


async def astartes_persona_by_tts_voice(db: aiosqlite.Connection, tts_voice: str) -> dict | None:
    """Resolve a selectable Astartes persona row for a requested TTS voice."""
    cursor = await db.execute(
        """
        SELECT *
        FROM personas
        WHERE default_rank = 'astartes'
          AND assignment_pool IN ('primary', 'backup')
          AND tts_voice = ?
        ORDER BY CASE assignment_pool WHEN 'primary' THEN 0 WHEN 'backup' THEN 1 ELSE 2 END,
                 assignment_order IS NULL,
                 assignment_order,
                 slug
        LIMIT 1
        """,
        (tts_voice,),
    )
    return _row_to_dict(await cursor.fetchone())


async def selectable_astartes_personas(db: aiosqlite.Connection) -> list[dict]:
    """Return seeded Astartes rows available for manual voice selection."""
    cursor = await db.execute(
        """
        SELECT *
        FROM personas
        WHERE default_rank = 'astartes'
          AND assignment_pool IN ('primary', 'backup')
          AND tts_voice IS NOT NULL
        ORDER BY CASE assignment_pool WHEN 'primary' THEN 0 WHEN 'backup' THEN 1 ELSE 2 END,
                 assignment_order IS NULL,
                 assignment_order,
                 slug
        """
    )
    return [_row_to_dict(row) for row in await cursor.fetchall()]


def _normalize_slug(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().lower().replace("_", "-").replace(" ", "-")


def singleton_persona_slug_for_runtime(
    *, legion: str | None = None, primarch: str | None = None
) -> str | None:
    """Map runtime legion/primarch markers to non-Astartes seeded persona slugs."""
    primarch_slug = _normalize_slug(primarch)
    if primarch_slug in SEED_BY_SLUG and SEED_BY_SLUG[primarch_slug].default_rank != "astartes":
        return primarch_slug

    legion_slug = _normalize_slug(legion)
    return {
        "custodes": "custodes",
        "fabricator": "fabricator-general",
        "fabricator-general": "fabricator-general",
        "administratum": "administratum",
        "inquisitor": "inquisitor",
    }.get(legion_slug)


def _row_to_dict_with_names(row, names: Sequence[str]) -> dict:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(zip(names, row, strict=False))


# ── Mechanicus-worker biconditional — query-time guard (belt + suspenders) ──
# The keystone invariant (Terra/Ultramar/Mechanicus Worker Persona) is enforced
# at write time by the ``db_schema`` triggers; this is the read-time half. For
# every live (rank != 'retired') *worker-tier* instance it asserts:
#
#   * commander resolves to the Fabricator-General persona singleton
#     IFF persona == mechanicus-worker            (the keystone biconditional)
#   * persona == mechanicus-worker  =>  automated = 1   (secondary invariant)
#
# "Commander resolves to FG" is keyed on the FG *persona* id (resolved by slug,
# restart-stable) carried as ``commander_type='persona' AND commander_id=<fg>`` —
# never a volatile commander instance_id. Singletons (personas whose
# ``default_rank != 'astartes'``: FG, Administratum, Custodes, Inquisitor,
# Primarchs) are EXEMPT — the biconditional is worker-tier only, and the FG is
# not its own commander. NULL-persona rows are treated as worker-tier (default
# rank 'astartes') so a stray FG-commanded row with no persona is still flagged.
#
# Column order is fixed and read by position so the guard does not depend on the
# caller's ``row_factory`` (a plain tuple and an aiosqlite.Row both work).
_MECH_INVARIANT_SQL = """
    SELECT
        i.id,
        (i.commander_type = 'persona'
            AND i.commander_id = (SELECT id FROM personas WHERE slug = 'fabricator-general'))
            AS commander_is_fg,
        (i.persona_id IS (SELECT id FROM personas WHERE slug = 'mechanicus-worker'))
            AS persona_is_mech,
        i.automated
    FROM instances i
    LEFT JOIN personas p ON p.id = i.persona_id
    WHERE i.rank != 'retired'
      AND COALESCE(p.default_rank, 'astartes') = 'astartes'
"""


def _mech_invariant_violations(rows) -> list[dict]:
    violations: list[dict] = []
    for row in rows:
        instance_id = row[0]
        commander_is_fg = bool(row[1])
        persona_is_mech = bool(row[2])
        automated = bool(row[3])
        reasons: list[str] = []
        if commander_is_fg != persona_is_mech:
            # commander→FG without the mechanicus coat, or the coat without a
            # FG commander — either half of the biconditional broken.
            reasons.append("biconditional")
        if persona_is_mech and not automated:
            reasons.append("not_automated")
        if reasons:
            violations.append(
                {
                    "id": instance_id,
                    "reasons": reasons,
                    "commander_is_fg": commander_is_fg,
                    "persona_is_mech": persona_is_mech,
                    "automated": automated,
                }
            )
    return violations


async def validate_mechanicus_invariant(db: aiosqlite.Connection) -> list[dict]:
    """Return mechanicus-worker biconditional violations among live instances.

    Empty list == healthy. Callers raise/log/heal per policy (the write triggers
    keep this empty in steady state; a non-empty result is a real anomaly worth
    surfacing). See :data:`_MECH_INVARIANT_SQL` for the exact contract.
    """
    cursor = await db.execute(_MECH_INVARIANT_SQL)
    return _mech_invariant_violations(await cursor.fetchall())


def validate_mechanicus_invariant_sync(conn: sqlite3.Connection) -> list[dict]:
    """Synchronous variant of :func:`validate_mechanicus_invariant`."""
    return _mech_invariant_violations(conn.execute(_MECH_INVARIANT_SQL).fetchall())
