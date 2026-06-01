#!/usr/bin/env python3
"""civic-invariant server launcher.

Single-process, no-reload uvicorn launcher for the always-on askCivic instance.
Run from inside the askCivic worktree (cwd = worktree root) via the venv python.

Why this exists instead of `make local` / `python -m app`:
  - `make local` runs uvicorn with reload=True (a reloader parent + worker child).
    That split makes supervision/PID tracking fragile and is wrong for a stable
    always-on instance. This launcher is a single clean process.
  - We load .env explicitly and absolutise GOOGLE_APPLICATION_CREDENTIALS so the
    Cloud SQL Connector + checkpointer find the SA key regardless of cwd timing.

DB connectivity (see ~/.civic-invariant/README or the session doc):
  - Main app pool   -> Cloud SQL Python Connector (INSTANCE_CONNECTION_NAME, port 443).
  - LangGraph saver -> direct psycopg DSN to DB_HOST:5432. This Mac's IP is NOT
    allowlisted on the dev SQL public IP, so .env sets DB_HOST=127.0.0.1 and we
    run a local cloud-sql-proxy on 127.0.0.1:5432 (managed by the harness).
"""
import os
import sys

# This launcher lives outside the worktree, so `python /abs/server.py` puts the
# script's own dir on sys.path[0] (not the cwd). The harness cd's into the
# worktree before launching, so add cwd so `import app.api.app` resolves.
sys.path.insert(0, os.getcwd())

from dotenv import load_dotenv

# Load the worktree .env (DB creds, PORT, DB_HOST=127.0.0.1, GAC, etc.)
load_dotenv(override=True)

# The Cloud SQL Connector resolves credentials via GOOGLE_APPLICATION_CREDENTIALS.
# .env stores it as a relative path; make it absolute so it resolves no matter
# what cwd the connector/google-auth happens to use.
gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
if gac and not os.path.isabs(gac):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath(gac)

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    # Single worker, no reload: one clean PID, deterministic supervision.
    uvicorn.run("app.api.app:app", host="0.0.0.0", port=port, log_level="info")
