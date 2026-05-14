"""Daily-note callout placeholder for dirty worktree imports."""
import re

ALLOWED_CALLOUT_TYPES = {"note", "info", "warning", "danger", "tip"}
CALLOUT_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
MAX_CONTENT_BYTES = 65536

class CalloutError(Exception):
    pass

class CalloutConflictError(CalloutError):
    pass


def apply_callout(*args, **kwargs):
    raise CalloutError("dailynote_callout service is not implemented in this worktree")
