import importlib
import sqlite3
import sys
from types import SimpleNamespace

import pytest


# Test-only compatibility surface: older tests still exercise legacy-shaped SQL,
# but the live DB must be seeded through the instances table.  Each sqlite3 connection
# gets a TEMP view named legacy_instances plus INSTEAD OF triggers that project
# legacy column names onto the instances table.  TEMP objects do not appear in the main
# schema, preserving the exterminatus invariant.
def _install_legacy_instances_test_view(conn):
    try:
        exists = conn.execute(
            "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name='instances'"
        ).fetchone()
    except Exception:
        return
    if not exists:
        return
    try:
        conn.execute("INSERT OR IGNORE INTO golden_throne (id) VALUES (1)")
    except Exception:
        pass
    try:
        conn.executescript(
            """
            CREATE TEMP VIEW IF NOT EXISTS legacy_instances AS
            SELECT
              i.id,
              NULL AS session_id,
              i.name AS tab_name,
              i.working_dir,
              CASE i.status WHEN 'working' THEN 'processing' ELSE i.status END AS status,
              i.engine,
              i.device_id,
              i.origin_type,
              i.continuity_binding_source,
              i.wrapper_launch_id,
              i.automated,
              i.launcher AS spawner,
              i.launcher,
              i.is_subagent,
              i.created_at,
              i.created_at AS registered_at,
              i.last_activity,
              i.stopped_at,
              i.tmux_pane,
              i.pane_label,
              CASE
                WHEN p.slug = 'custodes' THEN 'custodes'
                WHEN p.slug = 'fabricator-general' THEN 'fabricator'
                WHEN p.slug = 'administratum' THEN 'mechanicus'
                WHEN p.slug = 'mechanicus' THEN 'mechanicus'
                WHEN COALESCE(p.default_rank, i.rank) = 'astartes' THEN 'astartes'
                ELSE COALESCE(p.slug, 'astartes')
              END AS legion,
              CASE
                WHEN COALESCE(p.default_rank, '') = 'astartes' THEN NULL
                ELSE p.slug
              END AS primarch,
              p.slug AS profile_name,
              CASE WHEN i.golden_throne = 'sync' THEN 1 ELSE 0 END AS synced,
              CASE
                WHEN i.golden_throne = 'sync' THEN 'sync'
                WHEN i.golden_throne IS NOT NULL THEN 'golden_throne'
                WHEN COALESCE(i.hook_driven, 0) = 1 THEN 'hook_driven'
                ELSE 'one_off'
              END AS instance_type,
              CASE WHEN i.commander_type = 'chapter' THEN i.commander_id END AS parent_instance_id,
              NULL AS pid,
              NULL AS source_ip,
              i.session_doc_id,
              i.pr_url,
              i.pr_state,
              i.workflow_state,
              i.workflow_updated_at,
              i.workflow_blocked_reason,
              i.next_required_action,
              i.next_action_owner,
              i.planning_state,
              i.planning_updated_at,
              i.planning_source,
              i.input_lock,
              i.tts_voice,
              i.notification_sound,
              CASE i.notification_mode
                WHEN 'muted' THEN 'muted'
                WHEN 'silent' THEN 'silent'
                ELSE CASE i.interaction_mode WHEN 'voice_chat' THEN 'voice-chat' ELSE 'verbose' END
              END AS tts_mode,
              i.discord_hosted,
              i.discord_channel,
              i.discord_bot,
              i.hook_driven,
              i.zealotry,
              i.gt_resume_count,
              i.gt_resume_window_started_at,
              i.gt_last_resume_at,
              i.follow_up_sop,
              i.stop_allowed,
              i.dispatch_target,
              i.dispatch_window,
              i.dispatch_mode,
              i.dispatch_slot,
              i.dispatch_session_doc_path,
              i.target_working_dir,
              i.launch_mode,
              i.transplant_target_session,
              i.transplant_expected,
              i.session_doc_policy,
              i.victory_at,
              i.victory_reason,
              i.closure_surface,
              i.closure_required
            FROM instances i
            LEFT JOIN personas p ON p.id = i.persona_id;

            CREATE TEMP TRIGGER IF NOT EXISTS legacy_instances_insert
            INSTEAD OF INSERT ON legacy_instances
            BEGIN
              INSERT OR REPLACE INTO instances (
                id, name, engine, working_dir, device_id, origin_type,
                continuity_binding_source,
                commander_type, commander_id, status, created_at, last_activity,
                stopped_at, persona_id, rank, session_doc_id, automated,
                notification_mode, interaction_mode, golden_throne,
                tmux_pane, pane_label, launcher, is_subagent, hook_driven,
                zealotry, gt_resume_count, gt_resume_window_started_at,
                gt_last_resume_at, follow_up_sop, stop_allowed, input_lock,
                tts_voice, notification_sound, discord_hosted, discord_channel,
                discord_bot, workflow_state, workflow_updated_at,
                workflow_blocked_reason, next_required_action, next_action_owner,
                planning_state, planning_updated_at, planning_source, pr_url,
                pr_state, dispatch_target, dispatch_window, dispatch_mode,
                dispatch_slot, dispatch_session_doc_path, target_working_dir,
                launch_mode, transplant_target_session, transplant_expected,
                session_doc_policy, victory_at, victory_reason, closure_surface,
                closure_required
              ) VALUES (
                COALESCE(NEW.id, NEW.session_id),
                COALESCE(NEW.tab_name, NEW.id, NEW.session_id, 'test-instance'),
                NEW.engine,
                NEW.working_dir,
                COALESCE(NEW.device_id, 'Mac-Mini'),
                CASE COALESCE(NEW.origin_type, 'local')
                  WHEN 'hook' THEN 'api'
                  ELSE COALESCE(NEW.origin_type, 'local')
                END,
                NEW.continuity_binding_source,
                CASE
                  WHEN NEW.parent_instance_id IS NOT NULL
                   AND EXISTS (SELECT 1 FROM instances WHERE id = NEW.parent_instance_id)
                  THEN 'chapter'
                  ELSE 'emperor'
                END,
                CASE
                  WHEN NEW.parent_instance_id IS NOT NULL
                   AND EXISTS (SELECT 1 FROM instances WHERE id = NEW.parent_instance_id)
                  THEN NEW.parent_instance_id
                  ELSE NULL
                END,
                CASE COALESCE(NEW.status, 'idle') WHEN 'processing' THEN 'working' ELSE COALESCE(NEW.status, 'idle') END,
                COALESCE(NEW.registered_at, NEW.created_at, CURRENT_TIMESTAMP),
                COALESCE(NEW.last_activity, NEW.created_at, CURRENT_TIMESTAMP),
                NEW.stopped_at,
                (SELECT id FROM personas WHERE slug = CASE CASE
                    WHEN NEW.legion IS NOT NULL AND NEW.legion != 'astartes' THEN NEW.legion
                    ELSE COALESCE(NEW.profile_name, NEW.primarch, NEW.legion)
                  END
                    WHEN 'fabricator' THEN 'fabricator-general'
                    WHEN 'mechanicus:admin' THEN 'administratum'
                    WHEN 'mechanicus:administratum' THEN 'administratum'
                    WHEN 'legion:custodes' THEN 'custodes'
                    ELSE CASE
                      WHEN NEW.legion IS NOT NULL AND NEW.legion != 'astartes' THEN NEW.legion
                      ELSE COALESCE(NEW.profile_name, NEW.primarch, NEW.legion)
                    END
                  END LIMIT 1),
                'astartes',
                NEW.session_doc_id,
                COALESCE(NEW.is_subagent, 0),
                CASE COALESCE(NEW.tts_mode, 'verbose') WHEN 'muted' THEN 'muted' WHEN 'silent' THEN 'silent' ELSE 'verbose' END,
                CASE COALESCE(NEW.tts_mode, '') WHEN 'voice-chat' THEN 'voice_chat' WHEN 'voice_chat' THEN 'voice_chat' ELSE 'text' END,
                CASE WHEN COALESCE(NEW.synced, 0) = 1 THEN 'sync' WHEN NEW.instance_type = 'sync' THEN 'sync' WHEN NEW.instance_type = 'golden_throne' THEN '1' ELSE NULL END,
                NEW.tmux_pane, NEW.pane_label, COALESCE(NEW.launcher, NEW.spawner), COALESCE(NEW.is_subagent, 0), COALESCE(NEW.hook_driven, CASE WHEN NEW.instance_type = 'hook_driven' THEN 1 ELSE 0 END),
                COALESCE(NEW.zealotry, 4), COALESCE(NEW.gt_resume_count, 0), NEW.gt_resume_window_started_at,
                NEW.gt_last_resume_at, NEW.follow_up_sop, COALESCE(NEW.stop_allowed, 1), NEW.input_lock,
                NEW.tts_voice, NEW.notification_sound, COALESCE(NEW.discord_hosted, 0), NEW.discord_channel,
                NEW.discord_bot, NEW.workflow_state, NEW.workflow_updated_at,
                NEW.workflow_blocked_reason, NEW.next_required_action, NEW.next_action_owner,
                COALESCE(NEW.planning_state, 'none'), NEW.planning_updated_at, NEW.planning_source, NEW.pr_url,
                NEW.pr_state, NEW.dispatch_target, NEW.dispatch_window, NEW.dispatch_mode,
                NEW.dispatch_slot, NEW.dispatch_session_doc_path, NEW.target_working_dir,
                NEW.launch_mode, NEW.transplant_target_session, COALESCE(NEW.transplant_expected, 0),
                NEW.session_doc_policy, NEW.victory_at, NEW.victory_reason, NEW.closure_surface,
                COALESCE(NEW.closure_required, 0)
              );
            END;

            CREATE TEMP TRIGGER IF NOT EXISTS legacy_instances_update
            INSTEAD OF UPDATE ON legacy_instances
            BEGIN
              UPDATE instances SET
                name = COALESCE(NEW.tab_name, name),
                working_dir = NEW.working_dir,
                origin_type = CASE COALESCE(NEW.origin_type, origin_type)
                  WHEN 'hook' THEN 'api'
                  ELSE COALESCE(NEW.origin_type, origin_type)
                END,
                continuity_binding_source = NEW.continuity_binding_source,
                automated = COALESCE(NEW.automated, NEW.is_subagent, automated),
                status = CASE COALESCE(NEW.status, status) WHEN 'processing' THEN 'working' ELSE COALESCE(NEW.status, status) END,
                last_activity = COALESCE(NEW.last_activity, last_activity),
                stopped_at = NEW.stopped_at,
                tmux_pane = NEW.tmux_pane,
                pane_label = NEW.pane_label,
                persona_id = (SELECT id FROM personas WHERE slug = CASE
                    CASE
                      WHEN NEW.legion IS NOT OLD.legion THEN NEW.legion
                      ELSE COALESCE(NEW.profile_name, NEW.primarch, NEW.legion)
                    END
                    WHEN 'fabricator' THEN 'fabricator-general'
                    WHEN 'mechanicus:admin' THEN 'administratum'
                    WHEN 'mechanicus:administratum' THEN 'administratum'
                    WHEN 'legion:custodes' THEN 'custodes'
                    ELSE CASE
                      WHEN NEW.legion IS NOT OLD.legion THEN NEW.legion
                      ELSE COALESCE(NEW.profile_name, NEW.primarch, NEW.legion)
                    END
                  END LIMIT 1),
                golden_throne = CASE
                  WHEN COALESCE(NEW.synced, 0) = 1 THEN 'sync'
                  WHEN NEW.instance_type = 'sync' THEN 'sync'
                  WHEN NEW.instance_type = 'golden_throne' THEN COALESCE(golden_throne, '1')
                  WHEN COALESCE(NEW.synced, 0) = 0 THEN NULL
                  ELSE golden_throne
                END,
                pr_url = NEW.pr_url,
                pr_state = NEW.pr_state,
                workflow_state = NEW.workflow_state,
                workflow_updated_at = NEW.workflow_updated_at,
                workflow_blocked_reason = NEW.workflow_blocked_reason,
                next_required_action = NEW.next_required_action,
                next_action_owner = NEW.next_action_owner,
                planning_state = NEW.planning_state,
                planning_updated_at = NEW.planning_updated_at,
                planning_source = NEW.planning_source,
                input_lock = NEW.input_lock,
                hook_driven = COALESCE(NEW.hook_driven, hook_driven),
                gt_resume_count = COALESCE(NEW.gt_resume_count, gt_resume_count),
                gt_last_resume_at = NEW.gt_last_resume_at,
                follow_up_sop = NEW.follow_up_sop,
                stop_allowed = COALESCE(NEW.stop_allowed, stop_allowed)
              WHERE id = OLD.id;
            END;

            CREATE TEMP TRIGGER IF NOT EXISTS legacy_instances_delete
            INSTEAD OF DELETE ON legacy_instances
            BEGIN
              DELETE FROM instances WHERE id = OLD.id;
            END;
            """
        )
    except Exception:
        return


class _TokenOSTestConnection(sqlite3.Connection):
    def execute(self, sql, parameters=(), /):
        if isinstance(sql, str) and "legacy_instances" in sql:
            _install_legacy_instances_test_view(self)
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        if isinstance(sql, str) and "legacy_instances" in sql:
            _install_legacy_instances_test_view(self)
        return super().executemany(sql, parameters)

    def executescript(self, sql, /):
        if (
            isinstance(sql, str)
            and "legacy_instances" in sql
            and "CREATE TABLE legacy_instances" not in sql
        ):
            _install_legacy_instances_test_view(self)
        return super().executescript(sql)


_ORIG_SQLITE_CONNECT = sqlite3.connect


def _token_os_sqlite_connect(*args, **kwargs):
    kwargs.setdefault("factory", _TokenOSTestConnection)
    conn = _ORIG_SQLITE_CONNECT(*args, **kwargs)
    _install_legacy_instances_test_view(conn)
    return conn


_MODULES_TO_RELOAD = [
    "personas",
    "shared",
    "db_schema",
    "phone_service",
    "enforce",
    "enforcement_service",
    "routes.voice",
    "routes.tts",
    "routes.day_start",
    "routes.hooks",
    "stop_hook",
    "init_db",
    "temp_message",
    "timer_telemetry",
    "main",
]


@pytest.fixture(autouse=True)
def _legacy_instances_sqlite_view(monkeypatch):
    monkeypatch.setattr(sqlite3, "connect", _token_os_sqlite_connect)


@pytest.fixture(autouse=True)
def isolate_vault(tmp_path, monkeypatch):
    """Point the Obsidian vault at a per-test temp dir for EVERY test.

    Without this, vault-root resolution falls back to the live vault at
    /Volumes/Imperium/Imperium-ENV whenever IMPERIUM_ENV is unset and the NAS is
    mounted — which is how thousands of placeholder `needs-session-name-*.md` and
    `test-job-*.md` docs leaked into the live vault from test runs.  Vault-root
    resolution is now lazy (shared._vault_root / session_doc_helpers.vault_root),
    so setting the env here redirects all session-doc writes into the temp dir.
    The chokepoint guard in session_doc_helpers is the backstop if anything slips.
    """
    vault = tmp_path / "Imperium-ENV"
    monkeypatch.setenv("IMPERIUM_ENV", str(vault))
    monkeypatch.setenv("IMPERIUM", str(tmp_path / "imperium-root"))
    return vault


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    db_path = tmp_path / "agents.db"
    monkeypatch.setenv("TOKEN_API_DB", str(db_path))
    monkeypatch.setenv("IMPERIUM_ENV", str(tmp_path / "Imperium-ENV"))
    # Isolate morning-session state from the real /tmp so the keepalive gate and
    # morning/end endpoint operate on a per-test directory.
    monkeypatch.setenv("CUSTODES_MORNING_DIR", str(tmp_path / "custodes_morning"))
    monkeypatch.setattr(sqlite3, "connect", _token_os_sqlite_connect)

    for name in _MODULES_TO_RELOAD:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)

    shared = sys.modules["shared"]
    init_db = sys.modules["init_db"]
    main = sys.modules["main"]

    init_db.init_database()

    async def _no_pane_rows():
        return []

    async def _no_observed_agents():
        return []

    # Golden Throne fixtures insert Mac-Mini-local instances; pin the
    # reloaded module so Linux CI does not route them through satellite dispatch.
    monkeypatch.setattr(main, "LOCAL_DEVICE_NAME", "Mac-Mini")
    monkeypatch.setattr(main, "_tmux_pane_rows", _no_pane_rows)
    monkeypatch.setattr(main, "_detect_tmux_agent_panes", _no_observed_agents)

    return SimpleNamespace(db_path=db_path, shared=shared, init_db=init_db, main=main)
