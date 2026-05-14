"""Temporary message broadcast service placeholder.

This module is present so Token-API can import while the orchestrator temp-message
feature is under development in the dirty worktree.
"""

class SelectorError(ValueError):
    pass


async def broadcast_temp_message(selector, payload, *, idempotency_key, db_path, queue_sender, queue_drainer):
    raise SelectorError("temp_message service is not implemented in this worktree")
