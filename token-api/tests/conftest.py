import importlib
import sys
from types import SimpleNamespace

import pytest

_MODULES_TO_RELOAD = [
    "shared",
    "db_schema",
    "phone_service",
    "enforcement_service",
    "routes.voice",
    "routes.tts",
    "routes.day_start",
    "routes.hooks",
    "stop_hook",
    "init_db",
    "temp_message",
    "main",
]


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    db_path = tmp_path / "agents.db"
    monkeypatch.setenv("TOKEN_API_DB", str(db_path))
    monkeypatch.setenv("IMPERIUM_ENV", str(tmp_path / "Imperium-ENV"))

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

    monkeypatch.setattr(main, "_tmux_pane_rows", _no_pane_rows)
    monkeypatch.setattr(main, "_detect_tmux_agent_panes", _no_observed_agents)

    return SimpleNamespace(db_path=db_path, shared=shared, init_db=init_db, main=main)
