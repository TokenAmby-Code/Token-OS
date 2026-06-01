"""Root CLI shim for token-api/questions_gate.py."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_TOKEN_API = Path(__file__).resolve().parent / "token-api"
if str(_TOKEN_API) not in sys.path:
    sys.path.insert(0, str(_TOKEN_API))

# The repo-level Python may not have token-api dependencies (PyYAML).
# Preserve the requested `python -m questions_gate` surface by re-execing the
# token-api venv when needed.
try:
    import yaml  # noqa: F401
except ModuleNotFoundError:
    _venv_python = _TOKEN_API / ".venv" / "bin" / "python"
    if _venv_python.exists() and Path(sys.executable).resolve() != _venv_python.resolve():
        import os

        os.execv(str(_venv_python), [str(_venv_python), "-m", "questions_gate", *sys.argv[1:]])

_spec = importlib.util.spec_from_file_location("_token_api_questions_gate", _TOKEN_API / "questions_gate.py")
if _spec is None or _spec.loader is None:
    raise ImportError("cannot load token-api/questions_gate.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

trials_clear = _mod.trials_clear
trials_report = _mod.trials_report
main = _mod.main

if __name__ == "__main__":
    sys.exit(main())
