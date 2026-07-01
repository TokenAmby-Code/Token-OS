"""Wrapper-first tmuxctld runtime ledger.

The daemon owns pane/wrapper occupancy.  This ledger is intentionally
pseudo-volatile: an in-process dict is authoritative while tmuxctld is alive and
a single JSON file is rewritten after each mutation so a daemon bounce has a warm
hint.  The recovery authority is a one-shot tmux scan via ``reconcile_from_tmux``;
this is not sqlite-grade durability and must not become a rival token-api DB.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .tmux_adapter import TmuxAdapter

LEDGER_STATES = frozenset({"SHIPPED", "OPEN", "CLOSED"})
_ACTIVE_STATES = frozenset({"SHIPPED", "OPEN"})
_LEDGER_ENV = "TMUXCTLD_WRAPPER_LEDGER_PATH"
_SCAN_SEP = "__TMUXCTLD_LEDGER_FIELD__"


def _default_ledger_path() -> Path:
    return Path(os.environ.get(_LEDGER_ENV, "~/.claude/tmuxctld-wrapper-ledger.json")).expanduser()


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _state(value: Any, default: str = "OPEN") -> str:
    candidate = _clean(value).upper()
    return candidate if candidate in LEDGER_STATES else default


@dataclass(frozen=True)
class WrapperLedgerRow:
    """One wrapper-owned pane occupancy/runtime row.

    ``wrapper_id`` is the primary key.  ``instance_id`` and ``pane_label`` (the
    stable pane-positional id such as ``somnium:W``) are secondary oracle keys.
    ``pane_id`` is the current physical tmux backing, included for local daemon
    operations and client handoff; tmux remains the basement physical authority.
    """

    wrapper_id: str
    instance_id: str = ""
    persona: str = ""
    pane_id: str = ""
    pane_label: str = ""
    engine: str = ""
    working_dir: str = ""
    born_epoch: str = ""
    state: str = "OPEN"

    @property
    def pane_positional_id(self) -> str:
        return self.pane_label

    @property
    def occupied(self) -> bool:
        return self.state in _ACTIVE_STATES

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WrapperLedgerRow":
        pane_label = _clean(data.get("pane_label") or data.get("pane_positional_id"))
        return cls(
            wrapper_id=_clean(data.get("wrapper_id") or data.get("wrapper_launch_id")),
            instance_id=_clean(data.get("instance_id")),
            persona=_clean(data.get("persona")),
            pane_id=_clean(data.get("pane_id") or data.get("pane")),
            pane_label=pane_label,
            engine=_clean(data.get("engine")),
            working_dir=_clean(data.get("working_dir") or data.get("cwd")),
            born_epoch=_clean(data.get("born_epoch")),
            state=_state(data.get("state")),
        )

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "wrapper_id": self.wrapper_id,
            "instance_id": self.instance_id,
            "persona": self.persona,
            "pane_id": self.pane_id,
            "pane_label": self.pane_label,
            "pane_positional_id": self.pane_label,
            "engine": self.engine,
            "working_dir": self.working_dir,
            "born_epoch": self.born_epoch,
            "state": self.state,
            "occupied": self.occupied,
        }

    def merged(self, **updates: Any) -> "WrapperLedgerRow":
        data = self.to_dict()
        data.update({k: v for k, v in updates.items() if v is not None})
        return WrapperLedgerRow.from_dict(data)


class WrapperLedger:
    """Thread-safe wrapper-keyed in-memory ledger with JSON write-behind."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_ledger_path()
        self._lock = threading.RLock()
        self._rows: dict[str, WrapperLedgerRow] = {}
        self._by_instance: dict[str, str] = {}
        self._by_pane_label: dict[str, str] = {}
        self._by_pane_id: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        with self._lock:
            self._rows = {}
            if self.path.exists():
                try:
                    payload = json.loads(self.path.read_text(encoding="utf-8"))
                    raw_rows = payload.get("rows", []) if isinstance(payload, dict) else []
                    for raw in raw_rows:
                        if not isinstance(raw, dict):
                            continue
                        row = WrapperLedgerRow.from_dict(raw)
                        if row.wrapper_id:
                            self._rows[row.wrapper_id] = row
                except (OSError, ValueError, TypeError):
                    self._rows = {}
            self._rebuild_indexes_locked()

    def _write_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_epoch": time.time(),
            "rows": [row.to_dict() for row in sorted(self._rows.values(), key=lambda r: r.wrapper_id)],
        }
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp_name, self.path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)

    def _rebuild_indexes_locked(self) -> None:
        self._by_instance = {}
        self._by_pane_label = {}
        self._by_pane_id = {}
        for wrapper_id, row in self._rows.items():
            if row.instance_id:
                self._by_instance[row.instance_id] = wrapper_id
            if row.pane_label:
                self._by_pane_label[row.pane_label] = wrapper_id
            if row.pane_id:
                self._by_pane_id[row.pane_id] = wrapper_id

    def _put_locked(self, row: WrapperLedgerRow) -> WrapperLedgerRow:
        if not row.wrapper_id:
            raise ValueError("wrapper_id required")
        self._rows[row.wrapper_id] = row
        self._rebuild_indexes_locked()
        self._write_locked()
        return row

    def rows(self) -> list[WrapperLedgerRow]:
        with self._lock:
            return list(self._rows.values())

    def resolve(
        self,
        *,
        wrapper_id: str = "",
        instance_id: str = "",
        pane: str = "",
        include_closed: bool = True,
    ) -> WrapperLedgerRow | None:
        """Resolve wrapper_id, instance_id, physical pane id, or pane label."""
        wrapper_id = _clean(wrapper_id)
        instance_id = _clean(instance_id)
        pane = _clean(pane)
        with self._lock:
            key = wrapper_id
            if not key and instance_id:
                key = self._by_instance.get(instance_id, "")
            if not key and pane:
                key = self._by_pane_label.get(pane, "") or self._by_pane_id.get(pane, "")
            row = self._rows.get(key) if key else None
            if row and (include_closed or row.state != "CLOSED"):
                return row
            return None

    def wrapper_start(
        self,
        *,
        wrapper_id: str,
        pane_id: str = "",
        pane_label: str = "",
        persona: str = "",
        engine: str = "",
        working_dir: str = "",
        born_epoch: str = "",
        instance_id: str = "",
        state: str = "OPEN",
    ) -> WrapperLedgerRow:
        wrapper_id = _clean(wrapper_id)
        if not wrapper_id:
            raise ValueError("wrapper_id required")
        with self._lock:
            existing = self._rows.get(wrapper_id)
            row = existing or WrapperLedgerRow(wrapper_id=wrapper_id)
            row = row.merged(
                instance_id=_clean(instance_id) or row.instance_id,
                persona=_clean(persona) or row.persona,
                pane_id=_clean(pane_id) or row.pane_id,
                pane_label=_clean(pane_label) or row.pane_label,
                engine=_clean(engine) or row.engine,
                working_dir=_clean(working_dir) or row.working_dir,
                born_epoch=_clean(born_epoch) or row.born_epoch or str(int(time.time())),
                state=_state(state, "OPEN"),
            )
            return self._put_locked(row)

    def session_start(
        self,
        *,
        wrapper_id: str = "",
        instance_id: str,
        pane: str = "",
        pane_label: str = "",
        persona: str = "",
        engine: str = "",
        working_dir: str = "",
        born_epoch: str = "",
    ) -> WrapperLedgerRow:
        instance_id = _clean(instance_id)
        if not instance_id:
            raise ValueError("instance_id required")
        wrapper_id = _clean(wrapper_id)
        pane = _clean(pane)
        pane_label = _clean(pane_label)
        with self._lock:
            if not wrapper_id and pane:
                wrapper_id = self._by_pane_id.get(pane, "") or self._by_pane_label.get(pane, "")
            if not wrapper_id and pane_label:
                wrapper_id = self._by_pane_label.get(pane_label, "")
            if not wrapper_id:
                wrapper_id = self._by_instance.get(instance_id, "")
            if not wrapper_id:
                raise ValueError("wrapper_id required")
            existing = self._rows.get(wrapper_id) or WrapperLedgerRow(wrapper_id=wrapper_id)
            row = existing.merged(
                instance_id=instance_id,
                persona=_clean(persona) or existing.persona,
                pane_id=pane if pane.startswith("%") else existing.pane_id,
                pane_label=pane_label or (pane if pane and not pane.startswith("%") else existing.pane_label),
                engine=_clean(engine) or existing.engine,
                working_dir=_clean(working_dir) or existing.working_dir,
                born_epoch=_clean(born_epoch) or existing.born_epoch or str(int(time.time())),
                state="OPEN",
            )
            return self._put_locked(row)

    def wrapper_end(self, *, wrapper_id: str = "", pane: str = "") -> WrapperLedgerRow | None:
        wrapper_id = _clean(wrapper_id)
        pane = _clean(pane)
        with self._lock:
            if not wrapper_id and pane:
                wrapper_id = self._by_pane_id.get(pane, "") or self._by_pane_label.get(pane, "")
            row = self._rows.get(wrapper_id) if wrapper_id else None
            if not row:
                return None
            return self._put_locked(row.merged(state="CLOSED"))

    def reconcile_from_tmux(self, adapter: TmuxAdapter) -> dict[str, Any]:
        """Rebuild active rows from a one-shot tmux scan.

        This intentionally trusts tmux only for the scan-time physical facts and
        previously stamped bootstrap facts.  Rows without a wrapper id are not
        ledger rows yet and are skipped.
        """
        raw = adapter.run(
            "list-panes",
            "-a",
            "-F",
            _SCAN_SEP.join(
                [
                    "#{pane_id}",
                    "#{@TOKEN_API_WRAPPER_LAUNCH_ID}",
                    "#{@INSTANCE_ID}",
                    "#{@PANE_ID}",
                    "#{@PERSONA}",
                    "#{@TOKEN_API_ENGINE}",
                    "#{@TOKEN_API_CWD}",
                    "#{@PANE_BORN}",
                    "#{pane_current_path}",
                    "#{pane_dead}",
                ]
            ),
            allow_failure=True,
        )
        rebuilt: dict[str, WrapperLedgerRow] = {}
        skipped = 0
        for line in raw.splitlines():
            if not line:
                continue
            parts = line.split(_SCAN_SEP)
            if len(parts) != 10:
                skipped += 1
                continue
            (
                pane_id,
                wrapper_id,
                instance_id,
                pane_label,
                persona,
                engine,
                working_dir,
                born_epoch,
                pane_current_path,
                pane_dead,
            ) = (_clean(part) for part in parts)
            if not wrapper_id:
                skipped += 1
                continue
            # A remain-on-exit dead pane still exists but no longer hosts an open
            # wrapper.  Reconcile is a live-runtime rebuild, so prune it.
            if pane_dead == "1":
                skipped += 1
                continue
            rebuilt[wrapper_id] = WrapperLedgerRow(
                wrapper_id=wrapper_id,
                instance_id=instance_id,
                persona=persona or pane_label,
                pane_id=pane_id,
                pane_label=pane_label,
                engine=engine,
                working_dir=working_dir or pane_current_path,
                born_epoch=born_epoch or str(int(time.time())),
                state="OPEN",
            )
        with self._lock:
            self._rows = rebuilt
            self._rebuild_indexes_locked()
            self._write_locked()
        return {"rows": len(rebuilt), "skipped": skipped, "path": str(self.path)}

_GLOBAL_LOCK = threading.RLock()
_GLOBAL_LEDGER: WrapperLedger | None = None
_GLOBAL_PATH: Path | None = None


def get_wrapper_ledger() -> WrapperLedger:
    global _GLOBAL_LEDGER, _GLOBAL_PATH
    path = _default_ledger_path()
    with _GLOBAL_LOCK:
        if _GLOBAL_LEDGER is None or _GLOBAL_PATH != path:
            _GLOBAL_LEDGER = WrapperLedger(path)
            _GLOBAL_PATH = path
        return _GLOBAL_LEDGER


def reset_wrapper_ledger_for_tests(path: Path | None = None) -> WrapperLedger:
    global _GLOBAL_LEDGER, _GLOBAL_PATH
    with _GLOBAL_LOCK:
        _GLOBAL_PATH = path or _default_ledger_path()
        _GLOBAL_LEDGER = WrapperLedger(_GLOBAL_PATH)
        return _GLOBAL_LEDGER
