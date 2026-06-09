"""Canonical persona registry and assignment helpers.

The ``personas`` SQLite table is the runtime authority for display identity,
TTS/sound settings, and pane tinting. Legacy ``profile_name``/voice-pool call
sites are bridged here until the instances table grows ``persona_id``/``rank``.
"""

from __future__ import annotations

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
        "default",
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
        "default",
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
        "default",
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
        "default",
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
        "default",
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
    ),
    PersonaSeed(
        "inquisitor", "Inquisitor", "overseer", None, None, "#180830", "#7a4cc2", None, None, None
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
)

PERSONA_SEEDS: tuple[PersonaSeed, ...] = (
    *SINGLETON_PERSONAS,
    *PRIMARCH_PERSONAS,
    *PRIMARY_ASTARTES,
    *BACKUP_ASTARTES,
    ULTIMATE_ASTARTES,
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
    )


UPSERT_SQL = """
    INSERT INTO personas (
        id, slug, display_name, default_rank, assignment_pool, assignment_order,
        pane_tint, chip_color, tts_voice, tts_rate, notification_sound
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        notification_sound=excluded.notification_sound
"""


def ensure_personas_table_sync(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(persona_schema_sql())
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
    try:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in await cursor.fetchall()}
    except Exception:
        return set()


async def active_non_retired_persona_ids(db: aiosqlite.Connection) -> set[str]:
    """Return DB-locked personas for active, non-retired instances.

    Future schema path: ``instances.persona_id`` + ``instances.rank``.
    Current compatibility path: ``claude_instances.profile_name`` is treated as
    the persona slug while ``rank`` is absent; stopped rows do not lock.
    """
    instance_cols = await _table_columns(db, "instances")
    if {"persona_id", "rank"}.issubset(instance_cols):
        cursor = await db.execute(
            """
            SELECT DISTINCT persona_id
            FROM instances
            WHERE persona_id IS NOT NULL
              AND COALESCE(rank, '') != 'retired'
              AND COALESCE(status, 'active') NOT IN ('stopped', 'closed')
            """
        )
        return {row[0] for row in await cursor.fetchall() if row[0]}

    legacy_cols = await _table_columns(db, "claude_instances")
    if "profile_name" not in legacy_cols:
        return set()
    cursor = await db.execute(
        """
        SELECT DISTINCT p.id
        FROM claude_instances ci
        JOIN personas p ON p.slug = ci.profile_name
        WHERE ci.status IN ('processing', 'idle')
          AND p.default_rank = 'astartes'
        """
    )
    return {row[0] for row in await cursor.fetchall() if row[0]}


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


async def repair_legacy_instance_personas(db: aiosqlite.Connection) -> int:
    """Repair active instance rows left from the pre-persona voice profile era.

    This is a cutover migration for the compatibility column
    ``claude_instances.profile_name``. It only touches active non-subagent rows:
    singleton/Primarch panes are rewritten to their seeded persona identity, and
    active Astartes rows with missing/unknown legacy profile names receive the
    first currently-unlocked seeded Astartes persona.
    """
    cols = await _table_columns(db, "claude_instances")
    required = {"id", "profile_name", "tts_voice", "notification_sound", "status"}
    if not required.issubset(cols):
        return 0

    select_cols = [
        "id",
        "profile_name",
        "tts_voice",
        "notification_sound",
        "status",
    ]
    for optional in ("legion", "primarch", "is_subagent", "registered_at", "last_activity"):
        if optional in cols:
            select_cols.append(optional)

    subagent_clause = "AND COALESCE(is_subagent, 0) = 0" if "is_subagent" in cols else ""
    order_terms = [col for col in ("registered_at", "last_activity", "id") if col in cols]
    order_sql = ", ".join(order_terms or ["id"])
    cursor = await db.execute(
        f"""
        SELECT {", ".join(select_cols)}
        FROM claude_instances
        WHERE status IN ('processing', 'idle')
          {subagent_clause}
        ORDER BY {order_sql}
        """
    )
    rows = [_row_to_dict_with_names(row, select_cols) for row in await cursor.fetchall()]

    changed = 0
    locked_ids = await active_non_retired_persona_ids(db)
    for row in rows:
        singleton_slug = singleton_persona_slug_for_runtime(
            legion=row.get("legion"), primarch=row.get("primarch")
        )
        if singleton_slug:
            persona = await resolve_persona(db, singleton_slug)
            if not persona:
                continue
            profile = persona_to_profile(persona)
            updates = {
                "profile_name": profile["name"],
                "tts_voice": profile["wsl_voice"],
                "notification_sound": profile["notification_sound"],
            }
            if any(row.get(key) != value for key, value in updates.items()):
                await db.execute(
                    """
                    UPDATE claude_instances
                    SET profile_name = ?, tts_voice = ?, notification_sound = ?
                    WHERE id = ?
                    """,
                    (
                        updates["profile_name"],
                        updates["tts_voice"],
                        updates["notification_sound"],
                        row["id"],
                    ),
                )
                changed += 1
            # Singleton repairs may release a formerly valid Astartes slug.
            locked_ids = await active_non_retired_persona_ids(db)
            continue

        current = await resolve_persona(db, row.get("profile_name") or "")
        if current and current.get("default_rank") == "astartes":
            continue

        assigned, _ = await assign_astartes_persona(db, active_ids=locked_ids)
        profile = persona_to_profile(assigned)
        await db.execute(
            """
            UPDATE claude_instances
            SET profile_name = ?, tts_voice = ?, notification_sound = ?
            WHERE id = ?
            """,
            (
                profile["name"],
                profile["wsl_voice"],
                profile["notification_sound"],
                row["id"],
            ),
        )
        locked_ids.add(assigned["id"])
        changed += 1

    return changed


def _row_to_dict_with_names(row, names: Sequence[str]) -> dict:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(zip(names, row, strict=False))
