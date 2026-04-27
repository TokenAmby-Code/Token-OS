import importlib
import sys
from types import SimpleNamespace

import pytest

_MODULES_TO_RELOAD = [
    "shared",
    "routes.voice",
    "routes.tts",
    "routes.hooks",
    "stop_hook",
    "init_db",
    "main",
]


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    db_path = tmp_path / "agents.db"
    monkeypatch.setenv("TOKEN_API_DB", str(db_path))

    for name in _MODULES_TO_RELOAD:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)

    shared = sys.modules["shared"]
    init_db = sys.modules["init_db"]
    main = sys.modules["main"]

    init_db.init_database()
    return SimpleNamespace(db_path=db_path, shared=shared, init_db=init_db, main=main)
