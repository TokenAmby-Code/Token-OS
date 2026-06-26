"""Promoted/“top-decked” pause-queue items must play OLDEST-first.

Emperor (2026-06-26): "now that I'm draining the queue it's playing in reverse —
when I top deck the queue it should play oldest first." A cascade queues messages
A(old) → B → C(new); on promote/play-pane they must drain in that order, not
reversed. The bug was `appendleft` in forward order during a batch promote, which
flips the cascade so the newest message plays first.
"""

import asyncio
import importlib
import sys
from pathlib import Path


def _load_tts():
    token_api_dir = Path(__file__).resolve().parents[1]
    if str(token_api_dir) not in sys.path:
        sys.path.insert(0, str(token_api_dir))
    return sys.modules.get("routes.tts") or importlib.import_module("routes.tts")


def _make_item(tts, instance_id, message):
    return tts.TTSQueueItem(
        instance_id=instance_id,
        message=message,
        voice="Microsoft George",
        sound="",
        tab_name="cascade-pane",
        queue_target="pause",
    )


def _drain_order(tts):
    """Pop the hot queue the way the worker does (popleft / FIFO)."""
    return [item.message for item in list(tts.hot_queue)]


def test_play_pane_promotes_oldest_first():
    tts = _load_tts()
    tts.pause_queue.clear()
    tts.hot_queue.clear()

    iid = "cascade-instance"
    for msg in ("A-oldest", "B", "C-newest"):
        tts.pause_queue.append(_make_item(tts, iid, msg))

    asyncio.run(tts.play_pane(tts.PlayPaneRequest(instance_id=iid)))

    # Hot queue drains left→right (popleft). Oldest must come out first.
    assert _drain_order(tts) == ["A-oldest", "B", "C-newest"]
    assert len(tts.pause_queue) == 0


def test_promote_by_instance_promotes_oldest_first():
    tts = _load_tts()
    tts.pause_queue.clear()
    tts.hot_queue.clear()

    iid = "cascade-instance"
    for msg in ("A-oldest", "B", "C-newest"):
        tts.pause_queue.append(_make_item(tts, iid, msg))

    asyncio.run(tts.promote_from_pause(tts.PromoteRequest(instance_id=iid)))

    assert _drain_order(tts) == ["A-oldest", "B", "C-newest"]
    assert len(tts.pause_queue) == 0


def test_promoted_batch_lands_ahead_of_existing_hot_items():
    """A promoted cascade jumps the queue (front), still oldest-first within itself."""
    tts = _load_tts()
    tts.pause_queue.clear()
    tts.hot_queue.clear()

    # An unrelated item already sitting in the hot queue.
    existing = _make_item(tts, "other", "existing-hot")
    existing.queue_target = "hot"
    tts.hot_queue.append(existing)

    iid = "cascade-instance"
    for msg in ("A-oldest", "B", "C-newest"):
        tts.pause_queue.append(_make_item(tts, iid, msg))

    asyncio.run(tts.play_pane(tts.PlayPaneRequest(instance_id=iid)))

    assert _drain_order(tts) == ["A-oldest", "B", "C-newest", "existing-hot"]
