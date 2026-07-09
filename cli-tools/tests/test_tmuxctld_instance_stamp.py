"""tmuxctld is the SOLE writer of the durable @INSTANCE_ID pane stamp.

`TmuxControlPlane.instance_stamp` (the engine behind `POST /instance/stamp`) is the
single-writer replacement for token-api authoring a raw `set-option @INSTANCE_ID`.
It stamps the canonical instance id onto an explicit (fail-closed) pane, binds the
wrapper-ledger row's instance_id, and guarded-vacates a prior pane on a genuine move.
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import service  # noqa: E402
from tmuxctl.service import TmuxControlPlane  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_wrapper_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUXCTLD_WRAPPER_LEDGER_PATH", str(tmp_path / "wrapper-ledger.json"))
    from tmuxctl import wrapper_ledger

    wrapper_ledger.LEDGER._rows = {}
    wrapper_ledger.LEDGER._loaded = False
    wrapper_ledger.LEDGER.load(force=True)
    yield
    wrapper_ledger.LEDGER._rows = {}
    wrapper_ledger.LEDGER._loaded = False


class StampAdapter:
    """Records tmux `run` calls and reflects set-option/unset mutations back."""

    def __init__(self, pane_options: dict | None = None) -> None:
        self.pane_options: dict[tuple[str, str], str] = dict(pane_options or {})
        self.runs: list[tuple[str, ...]] = []

    def list_sessions(self) -> list:
        return []

    def run(self, *args, allow_failure: bool = False) -> str:
        self.runs.append(tuple(args))
        a = list(args)
        if a and a[0] == "set-option" and "-t" in a:
            i = a.index("-t")
            target = a[i + 1]
            option = a[i + 2]
            if "-pu" in a or "-u" in a:
                self.pane_options.pop((target, option), None)
            elif i + 3 < len(a):
                self.pane_options[(target, option)] = a[i + 3]
        return ""

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return self.pane_options.get((pane_id, option), "")


def _identity_resolve(monkeypatch, roles: dict | None = None):
    roles = roles or {}

    def fake_resolve(adapter, target, session_name=None):
        return SimpleNamespace(pane_id=target, pane_role=roles.get(target, "mechanicus:2"))

    monkeypatch.setattr(service, "resolve_pane", fake_resolve)


def _set_options(runs, option="@INSTANCE_ID"):
    return [r for r in runs if r and r[0] == "set-option" and option in r]


def test_instance_stamp_writes_pane_option_and_binds_ledger(monkeypatch):
    from tmuxctl import wrapper_ledger

    adapter = StampAdapter({("%2", "@PANE_ID"): "mechanicus:2"})
    control = TmuxControlPlane(adapter=adapter)
    _identity_resolve(monkeypatch)

    out = control.instance_stamp(
        instance_id="inst-abc",
        pane="%2",
        wrapper_id="wrap-2",
        engine="claude",
        working_dir="/tmp/wt",
        persona="mechanicus",
    )

    assert out["found"] is True
    assert out["stamped"] is True
    assert out["pane"] == "%2"
    assert out["instance_id"] == "inst-abc"
    # tmuxctld wrote the stamp.
    assert ("set-option", "-p", "-t", "%2", "@INSTANCE_ID", "inst-abc") in adapter.runs
    assert adapter.pane_options[("%2", "@INSTANCE_ID")] == "inst-abc"
    assert adapter.pane_options[("%2", "@PERSONA")] == "mechanicus"
    assert adapter.pane_options[("%2", "@TOKEN_API_ENGINE")] == "claude"
    assert adapter.pane_options[("%2", "@TOKEN_API_CWD")] == "/tmp/wt"
    assert adapter.pane_options[("%2", "@PANE_BORN")]
    # Ledger bind: the reverse oracle now prefers the ledger over a stamp scan.
    row = wrapper_ledger.LEDGER.resolve(wrapper_id="wrap-2")
    assert row is not None
    assert row.instance_id == "inst-abc"
    assert row.pane_positional_id == "mechanicus:2"


def test_instance_stamp_does_not_tint_before_registry_commit(monkeypatch):
    adapter = StampAdapter({("%fg", "@PANE_ID"): "mechanicus:fabricator-general"})
    control = TmuxControlPlane(adapter=adapter)
    _identity_resolve(monkeypatch, {"%fg": "mechanicus:fabricator-general"})

    out = control.instance_stamp(
        instance_id="inst-fg",
        pane="%fg",
        wrapper_id="wrap-fg",
        persona="fabricator-general",
    )

    assert out["tint"] == ""
    assert adapter.pane_options[("%fg", "@PERSONA")] == "fabricator-general"
    assert not [r for r in adapter.runs if "window-style" in r or "window-active-style" in r]


def test_instance_stamp_never_tints_or_clears_tint_before_registry_commit(monkeypatch):
    adapter = StampAdapter({("%fg", "@PANE_ID"): "mechanicus:fabricator-general"})
    control = TmuxControlPlane(adapter=adapter)
    _identity_resolve(monkeypatch, {"%fg": "mechanicus:fabricator-general"})

    out = control.instance_stamp(
        instance_id="inst-worker",
        pane="%fg",
        wrapper_id="wrap-worker",
        persona="mechanicus",
    )

    assert out["tint"] == ""
    assert not [r for r in adapter.runs if "window-style" in r or "window-active-style" in r]


def test_instance_stamp_fails_closed_on_unresolved_pane(monkeypatch):
    adapter = StampAdapter()
    control = TmuxControlPlane(adapter=adapter)

    def boom(adapter_, target, session_name=None):
        raise ValueError("no such pane")

    monkeypatch.setattr(service, "resolve_pane", boom)

    out = control.instance_stamp(instance_id="inst-ghost", pane="%dead", wrapper_id="wrap-x")

    assert out["found"] is False
    assert out["stamped"] is False
    # NEVER stamp a wrong/unresolved pane.
    assert _set_options(adapter.runs) == []


def test_instance_stamp_no_instance_id_is_noop(monkeypatch):
    adapter = StampAdapter()
    control = TmuxControlPlane(adapter=adapter)
    _identity_resolve(monkeypatch)

    out = control.instance_stamp(instance_id="", pane="%2")

    assert out["found"] is False
    assert out["stamped"] is False
    assert adapter.runs == []


def test_instance_stamp_guarded_vacate_clears_matching_old_pane(monkeypatch):
    # Old pane still carries THIS instance's id → vacate it on the move.
    adapter = StampAdapter(
        {
            ("%old", "@INSTANCE_ID"): "inst-move",
            ("%new", "@PANE_ID"): "mechanicus:2",
        }
    )
    control = TmuxControlPlane(adapter=adapter)
    _identity_resolve(monkeypatch)

    out = control.instance_stamp(
        instance_id="inst-move", pane="%new", wrapper_id="wrap-m", vacate_pane="%old"
    )

    assert out["stamped"] is True
    assert out["vacated"] == "%old"
    assert ("set-option", "-pu", "-t", "%old", "@INSTANCE_ID") in adapter.runs
    assert ("%old", "@INSTANCE_ID") not in adapter.pane_options


def test_instance_stamp_guarded_vacate_skips_reused_old_pane(monkeypatch):
    # Old pane was re-stamped by a DIFFERENT instance → never clobber it.
    adapter = StampAdapter(
        {
            ("%old", "@INSTANCE_ID"): "someone-else",
            ("%new", "@PANE_ID"): "mechanicus:2",
        }
    )
    control = TmuxControlPlane(adapter=adapter)
    _identity_resolve(monkeypatch)

    out = control.instance_stamp(
        instance_id="inst-move", pane="%new", wrapper_id="wrap-m", vacate_pane="%old"
    )

    assert out["vacated"] == ""
    assert ("set-option", "-pu", "-t", "%old", "@INSTANCE_ID") not in adapter.runs
    assert adapter.pane_options[("%old", "@INSTANCE_ID")] == "someone-else"


def test_instance_stamp_same_pane_refire_does_not_vacate(monkeypatch):
    # Paneless re-fire: effective pane == vacate pane. Re-stamp is idempotent and the
    # guarded vacate must not clear the live pane (the churn-that-zeroed-the-stamp bug).
    adapter = StampAdapter(
        {
            ("%live", "@INSTANCE_ID"): "inst-live",
            ("%live", "@PANE_ID"): "mechanicus:2",
        }
    )
    control = TmuxControlPlane(adapter=adapter)
    _identity_resolve(monkeypatch)

    out = control.instance_stamp(
        instance_id="inst-live", pane="%live", wrapper_id="wrap-l", vacate_pane="%live"
    )

    assert out["stamped"] is True
    assert out["vacated"] == ""
    assert _set_options(adapter.runs) == [
        ("set-option", "-p", "-t", "%live", "@INSTANCE_ID", "inst-live")
    ]
    assert adapter.pane_options[("%live", "@INSTANCE_ID")] == "inst-live"


def test_instance_stamp_resolves_pane_by_wrapper_id_fallback(monkeypatch):
    from tmuxctl import wrapper_ledger

    # No explicit pane: resolve through the ledger row's positional id.
    wrapper_ledger.LEDGER.upsert(
        wrapper_id="wrap-f", pane_positional_id="mechanicus:2", persona="mechanicus", state="OPEN"
    )
    adapter = StampAdapter({("%2", "@PANE_ID"): "mechanicus:2"})
    control = TmuxControlPlane(adapter=adapter)

    def fake_resolve(adapter_, target, session_name=None):
        assert target == "mechanicus:2"
        return SimpleNamespace(pane_id="%2", pane_role="mechanicus:2")

    monkeypatch.setattr(service, "resolve_pane", fake_resolve)

    out = control.instance_stamp(instance_id="inst-f", wrapper_id="wrap-f")

    assert out["stamped"] is True
    assert out["pane"] == "%2"
    assert ("set-option", "-p", "-t", "%2", "@INSTANCE_ID", "inst-f") in adapter.runs
