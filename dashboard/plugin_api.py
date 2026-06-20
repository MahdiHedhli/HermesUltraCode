"""HermesUltraCode dashboard-plugin backend.

A thin FastAPI router the Hermes web dashboard mounts at ``/api/plugins/hermesultracode/``
(see Hermes ``_mount_plugin_api_routes``). It reuses the gate's existing ``server.views`` +
the SHARED SQLite store — the immutable audit trail PLUS the live-progress snapshot the
agent process mirrors into SQLite (the web dashboard runs in a different process and can't
see the agent's in-memory PROGRESS). Read-only; secrets are redacted in the store/views
layer. Auth is the dashboard's own session token (the host middleware gates these routes).
"""

from __future__ import annotations

import os
import sys

# The plugin root holds core/ + server/; put it on sys.path so this file (under dashboard/)
# can reuse the gate's data layer without duplicating it.
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from fastapi import APIRouter, Query  # provided by the Hermes dashboard runtime

from core.store import AuditFilter
from core.store_sqlite import SqliteAuditStore
from server import views

router = APIRouter()

_STORE: SqliteAuditStore | None = None


def _store_path() -> str:
    return os.environ.get(
        "HERMESULTRACODE_STORE",
        os.path.join(os.path.expanduser("~"), ".hermes", "hermesultracode", "audit.sqlite3"),
    )


def _store() -> SqliteAuditStore:
    global _STORE
    if _STORE is None:
        _STORE = SqliteAuditStore(_store_path())
    return _STORE


@router.get("/live")
def live() -> dict:
    st = _store()
    return views.make_store_live_source(st)(st)


@router.get("/plan")
def plan() -> dict:
    st = _store()
    return views.make_store_live_source(st)(st).get("plan", {})


@router.get("/metrics")
def metrics() -> dict:
    return views.compute_metrics(_store(), None)


@router.get("/neckbeard")
def neckbeard() -> dict:
    return views.neckbeard_view(_store(), _PLUGIN_ROOT)


@router.get("/queue")
def queue() -> dict:
    return {"queue": views.default_queue_source(_store())}


@router.get("/failclosed")
def failclosed() -> dict:
    return {"fail_closed_count": _store().count_fail_closed()}


@router.get("/audit")
def audit(tier: str = Query(None), verdict: str = Query(None), limit: int = Query(50, le=500)) -> dict:
    flt = AuditFilter(tier=tier or None, verdict=verdict or None, limit=limit)
    return {"rows": [views.record_summary(r) for r in _store().query(flt)]}


@router.get("/dispatch/{rid}")
def dispatch(rid: str) -> dict:
    rec = _store().get(rid)
    if rec is None:
        return {"error": "not found"}
    return views.gate_panel(rec)
