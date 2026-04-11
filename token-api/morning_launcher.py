#!/usr/bin/env python3
"""Morning Session Launcher — thin shim.

Called by cron at wakeup time. Delegates entirely to morning_session.run_morning_session().
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from morning_session import run_morning_session

if __name__ == "__main__":
    result = run_morning_session()
    print(json.dumps(result, indent=2))
