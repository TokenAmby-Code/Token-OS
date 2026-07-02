"""Wrapper-first tmuxctld runtime ledger.

The daemon keeps this intentionally cheap: an in-process dictionary is the live
source of truth, with a single JSON write-behind file rewritten on every change.
It is not a registry database. If it is lost, ``/reconcile`` rebuilds the open
rows from the live tmux wrapper stamps.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

LEDGER_VERSION = 1
ACTIVE_STATES = frozenset({"SHIPPED", "OPEN"})
DEFAULT_LEDGER_PATH = Path.home() / ".claude" / "tmuxctld-wrapper-ledger.json"
_SCAN_SEP = "__TMUXCTLD_WRAPPER_LEDGER_FIELD__"


def ledger_path() -> Path:
    raw = os.environ.get("TMUXCTLD_WRAPPER_LEDGER_PATH", "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_LEDGER_PATH


def _s(value: object) -> str:
    return "" if value is None else str(value).strip()


def _epoch(value: object | None = None) -> float:
    if value in (None, ""):
        return time.time()
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return time.time()
    return parsed if parsed > 0 else time.time()


@dataclass(frozen=True)
class WrapperLedgerRow:
    wrapper_id: str
    instance_id: str
    persona: str
    pane_positional_id: str
    engine: str
    working_dir: str
    born_epoch: float
    state: str

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> WrapperLedgerRow:
        return cls(
            wrapper_id=_s(data.get("wrapper_id") or data.get("wrapper_launch_id")),
            instance_id=_s(data.get("instance_id")),
            persona=_s(data.get("persona")),
            pane_positional_id=_s(
                data.get("pane_positional_id") or data.get("pane_label") or data.get("pane_id")
            ),
            engine=_s(data.get("engine")),
            working_dir=_s(data.get("working_dir") or data.get("cwd")),
            born_epoch=_epoch(data.get("born_epoch")),
            state=(_s(data.get("state")) or "OPEN").upper(),
        )

    def merge(self, **updates: object) -> WrapperLedgerRow:
        data = asdict(self)
        for key, value in updates.items():
            if key == "born_epoch":
                if value not in (None, ""):
                    data[key] = _epoch(value)
            elif key == "state":
                if _s(value):
                    data[key] = _s(value).upper()
            elif key in data and _s(value):
                data[key] = _s(value)
        return WrapperLedgerRow.from_mapping(data)

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["born_epoch"] = float(self.born_epoch)
        return data


class WrapperLedger:
    """Thread-safe wrapper_id keyed runtime oracle."""

    def __init__(self, path: Path | None = None) -> None:
        self._explicit_path = path is not None
        self._path = path or ledger_path()
        self._lock = threading.RLock()
        self._loaded = False
        self._rows: dict[str, WrapperLedgerRow] = {}
        self._by_instance: dict[str, str] = {}
        self._by_pane_positional: dict[str, str] = {}

    @property
    def path(self) -> Path:
        return self._path

    def load(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._loaded and not force:
                return {"loaded": True, "path": str(self._path), "rows": len(self._rows)}
            if not self._explicit_path:
                self._path = ledger_path()
            rows: dict[str, WrapperLedgerRow] = {}
            try:
                payload = json.loads(self._path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                payload = {}
            except Exception:
                # Corrupt write-behind is not a disaster; the next reconcile scan
                # reconstructs open rows. Start empty and overwrite on change.
                payload = {}
            raw_rows = payload.get("rows") if isinstance(payload, dict) else None
            if isinstance(raw_rows, list):
                for item in raw_rows:
                    if not isinstance(item, dict):
                        continue
                    row = WrapperLedgerRow.from_mapping(item)
                    if row.wrapper_id:
                        rows[row.wrapper_id] = row
            self._rows = rows
            self._reindex_locked()
            self._loaded = True
            return {"loaded": True, "path": str(self._path), "rows": len(self._rows)}

    def _ensure_loaded_locked(self) -> None:
        if not self._loaded:
            self.load()

    def _reindex_locked(self) -> None:
        self._by_instance = {}
        self._by_pane_positional = {}
        for wrapper_id, row in self._rows.items():
            if not row.active:
                continue
            if row.instance_id:
                self._by_instance[row.instance_id] = wrapper_id
            if row.pane_positional_id:
                self._by_pane_positional[row.pane_positional_id] = wrapper_id

    def _write_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            row.as_dict() for row in sorted(self._rows.values(), key=lambda item: item.wrapper_id)
        ]
        payload = {
            "version": LEDGER_VERSION,
            "updated_epoch": time.time(),
            "rows": rows,
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.", suffix=".tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, sort_keys=True, separators=(",", ":"))
                fh.write("\n")
            os.replace(tmp_name, self._path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    def upsert(self, **fields: object) -> WrapperLedgerRow:
        wrapper_id = _s(fields.get("wrapper_id") or fields.get("wrapper_launch_id"))
        if not wrapper_id:
            raise ValueError("wrapper_id required")
        with self._lock:
            self._ensure_loaded_locked()
            existing = self._rows.get(wrapper_id)
            if existing is None:
                row = WrapperLedgerRow.from_mapping(
                    {
                        **fields,
                        "wrapper_id": wrapper_id,
                        "born_epoch": fields.get("born_epoch") or time.time(),
                        "state": fields.get("state") or "OPEN",
                    }
                )
            else:
                row = existing.merge(**{**fields, "wrapper_id": wrapper_id})
            self._rows[wrapper_id] = row
            self._reindex_locked()
            self._write_locked()
            return row

    def close(self, wrapper_id: str, *, state: str = "CLOSED") -> WrapperLedgerRow | None:
        wrapper_id = _s(wrapper_id)
        if not wrapper_id:
            return None
        with self._lock:
            self._ensure_loaded_locked()
            existing = self._rows.get(wrapper_id)
            if existing is None:
                return None
            row = existing.merge(state=state)
            self._rows[wrapper_id] = row
            self._reindex_locked()
            self._write_locked()
            return row

    def resolve(
        self,
        value: str = "",
        *,
        wrapper_id: str = "",
        instance_id: str = "",
        pane_positional_id: str = "",
        include_closed: bool = False,
    ) -> WrapperLedgerRow | None:
        needle = _s(value)
        with self._lock:
            self._ensure_loaded_locked()
            if wrapper_id:
                candidates = [_s(wrapper_id)]
            elif instance_id:
                instance_key = _s(instance_id)
                candidates = [self._by_instance.get(instance_key, "")]
                if include_closed:
                    candidates.extend(
                        row.wrapper_id
                        for row in self._rows.values()
                        if row.instance_id == instance_key
                    )
            elif pane_positional_id:
                pane_key = _s(pane_positional_id)
                candidates = [self._by_pane_positional.get(pane_key, "")]
                if include_closed:
                    candidates.extend(
                        row.wrapper_id
                        for row in self._rows.values()
                        if row.pane_positional_id == pane_key
                    )
            elif needle:
                candidates = [
                    needle,
                    self._by_instance.get(needle, ""),
                    self._by_pane_positional.get(needle, ""),
                ]
                if include_closed:
                    candidates.extend(
                        row.wrapper_id
                        for row in self._rows.values()
                        if row.instance_id == needle or row.pane_positional_id == needle
                    )
            else:
                candidates = []
            for key in candidates:
                row = self._rows.get(key) if key else None
                if row and (include_closed or row.active):
                    return row
            return None

    def rows(self, *, include_closed: bool = True) -> list[WrapperLedgerRow]:
        with self._lock:
            self._ensure_loaded_locked()
            return [
                row
                for row in sorted(self._rows.values(), key=lambda r: r.wrapper_id)
                if include_closed or row.active
            ]

    def reconcile_from_tmux(self, adapter: Any) -> dict[str, Any]:
        """Replace active rows from a single live tmux scan.

        Existing CLOSED rows are retained for cheap post-mortem/debug continuity;
        SHIPPED/OPEN rows not present in tmux are pruned because the physical pane
        truth says the wrapper is no longer open.
        """
        fields = _SCAN_SEP.join(
            [
                "#{@TOKEN_API_WRAPPER_ID}",
                "#{@TOKEN_API_WRAPPER_LAUNCH_ID}",
                "#{@INSTANCE_ID}",
                "#{@PERSONA}",
                "#{@PANE_ID}",
                "#{@TOKEN_API_ENGINE}",
                "#{@TOKEN_API_CWD}",
                "#{@PANE_BORN}",
            ]
        )
        raw = adapter.run("list-panes", "-a", "-F", fields, allow_failure=True)
        live_rows: dict[str, WrapperLedgerRow] = {}
        for line in raw.splitlines():
            parts = line.split(_SCAN_SEP)
            if len(parts) == 7:
                # Backward compatibility for older scan formats.
                parts = ["", *parts]
            if len(parts) != 8:
                continue
            (
                wrapper_id,
                legacy_wrapper_id,
                instance_id,
                persona,
                pane_positional_id,
                engine,
                working_dir,
                born_epoch,
            ) = parts
            wrapper_id = _s(wrapper_id) or _s(legacy_wrapper_id)
            if not wrapper_id:
                continue
            row = WrapperLedgerRow.from_mapping(
                {
                    "wrapper_id": wrapper_id,
                    "instance_id": instance_id,
                    "persona": persona,
                    "pane_positional_id": pane_positional_id,
                    "engine": engine,
                    "working_dir": working_dir,
                    "born_epoch": born_epoch or time.time(),
                    "state": "OPEN",
                }
            )
            live_rows[row.wrapper_id] = row

        with self._lock:
            self._ensure_loaded_locked()
            active_before = {key for key, row in self._rows.items() if row.active}
            closed_rows = {
                key: row
                for key, row in self._rows.items()
                if not row.active and key not in live_rows
            }
            self._rows = {**closed_rows, **live_rows}
            self._reindex_locked()
            self._write_locked()
            return {
                "path": str(self._path),
                "loaded_rows": len(self._rows),
                "open_rows": len(live_rows),
                "pruned_open_rows": len(active_before - set(live_rows)),
            }


LEDGER = WrapperLedger()
