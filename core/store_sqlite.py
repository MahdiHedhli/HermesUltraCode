"""Default audit store: stdlib sqlite3 (neckbeard rung 2 — stdlib does it).

Immutability is enforced structurally: UPDATE and DELETE on the audit table raise
via SQLite triggers, on top of the interface exposing no mutator but ``append``.
Secrets are redacted on write. ``append`` is idempotent on ``id`` (retries/backoff
safe — extended protected set).

Leave room for a Cloudflare D1 adapter as a later swap: it implements the same
``AuditStore`` interface; nothing above this file knows the backend.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any

from .redact import redact, redact_obj
from .store import (
    COLUMNS,
    AuditFilter,
    AuditStore,
    DispatchRecord,
    ImmutabilityError,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dispatch_audit (
    id                TEXT PRIMARY KEY,
    ts                TEXT NOT NULL,
    base_prompt       TEXT NOT NULL,
    added_directives  TEXT NOT NULL,   -- JSON array
    dispatched_prompt TEXT NOT NULL,
    verdict           TEXT NOT NULL,
    tier              TEXT NOT NULL,
    reviewer_model    TEXT NOT NULL,
    decision          TEXT NOT NULL,
    round_count       INTEGER NOT NULL,
    rationale         TEXT NOT NULL DEFAULT '',
    scope_assessment  TEXT NOT NULL DEFAULT '',
    fail_closed       INTEGER NOT NULL DEFAULT 0,
    fail_closed_reason TEXT NOT NULL DEFAULT '',
    dissent_logged    INTEGER NOT NULL DEFAULT 0,
    escalated         INTEGER NOT NULL DEFAULT 0,
    neckbeard_block    INTEGER NOT NULL DEFAULT 0,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    added_tokens      INTEGER NOT NULL DEFAULT 0
);

-- Structural immutability: the audit trail is append-only evidence.
CREATE TRIGGER IF NOT EXISTS dispatch_audit_no_update
BEFORE UPDATE ON dispatch_audit
BEGIN
    SELECT RAISE(ABORT, 'dispatch_audit is append-only: UPDATE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS dispatch_audit_no_delete
BEFORE DELETE ON dispatch_audit
BEGIN
    SELECT RAISE(ABORT, 'dispatch_audit is append-only: DELETE forbidden');
END;

CREATE INDEX IF NOT EXISTS idx_audit_ts ON dispatch_audit(ts);
CREATE INDEX IF NOT EXISTS idx_audit_tier ON dispatch_audit(tier);
CREATE INDEX IF NOT EXISTS idx_audit_verdict ON dispatch_audit(verdict);
"""


class SqliteAuditStore(AuditStore):
    """SQLite-backed immutable audit trail. ``path=':memory:'`` for tests."""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        # check_same_thread=False + a lock: the reviewer wrapper uses a worker thread,
        # and the read API serves concurrently. One connection, serialised by a lock.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def append(self, record: DispatchRecord) -> str:
        import json

        # Redact on write — the audit trail must not become a credential store.
        directives = redact_obj(list(record.added_directives))
        values = {
            "id": record.id,
            "ts": record.ts,
            "base_prompt": redact(record.base_prompt),
            "added_directives": json.dumps(directives, ensure_ascii=False),
            "dispatched_prompt": redact(record.dispatched_prompt),
            "verdict": record.verdict,
            "tier": record.tier,
            "reviewer_model": record.reviewer_model,
            "decision": record.decision,
            "round_count": int(record.round_count),
            "rationale": redact(record.rationale),
            "scope_assessment": record.scope_assessment,
            "fail_closed": int(bool(record.fail_closed)),
            "fail_closed_reason": redact(record.fail_closed_reason),
            "dissent_logged": int(bool(record.dissent_logged)),
            "escalated": int(bool(record.escalated)),
            "neckbeard_block": int(bool(record.neckbeard_block)),
            "latency_ms": int(record.latency_ms),
            "added_tokens": int(record.added_tokens),
        }
        cols = ", ".join(values.keys())
        placeholders = ", ".join(f":{k}" for k in values.keys())
        with self._lock:
            # Idempotent on id: a retried write of the same dispatch is a no-op, not a
            # duplicate and not an immutability violation (retries/backoff safe).
            existing = self._conn.execute(
                "SELECT 1 FROM dispatch_audit WHERE id = ?", (record.id,)
            ).fetchone()
            if existing is not None:
                return record.id
            try:
                self._conn.execute(
                    f"INSERT INTO dispatch_audit ({cols}) VALUES ({placeholders})", values
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:  # pragma: no cover - race fallback
                raise ImmutabilityError(str(exc)) from exc
        return record.id

    def get(self, record_id: str) -> DispatchRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM dispatch_audit WHERE id = ?", (record_id,)
            ).fetchone()
        return _row_to_record(row) if row else None

    def query(self, flt: AuditFilter | None = None) -> list[DispatchRecord]:
        flt = flt or AuditFilter()
        clauses: list[str] = []
        params: list[Any] = []
        if flt.tier:
            clauses.append("tier = ?")
            params.append(flt.tier)
        if flt.verdict:
            clauses.append("verdict = ?")
            params.append(flt.verdict)
        if flt.decision:
            clauses.append("decision = ?")
            params.append(flt.decision)
        if flt.fail_closed is not None:
            clauses.append("fail_closed = ?")
            params.append(int(flt.fail_closed))
        if flt.ts_from:
            clauses.append("ts >= ?")
            params.append(flt.ts_from)
        if flt.ts_to:
            clauses.append("ts <= ?")
            params.append(flt.ts_to)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM dispatch_audit{where} ORDER BY ts ASC, id ASC"
        if flt.limit:
            sql += " LIMIT ?"
            params.append(int(flt.limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_record(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_record(row: sqlite3.Row) -> DispatchRecord:
    import json

    return DispatchRecord(
        id=row["id"],
        ts=row["ts"],
        base_prompt=row["base_prompt"],
        added_directives=tuple(json.loads(row["added_directives"])),
        dispatched_prompt=row["dispatched_prompt"],
        verdict=row["verdict"],
        tier=row["tier"],
        reviewer_model=row["reviewer_model"],
        decision=row["decision"],
        round_count=row["round_count"],
        rationale=row["rationale"],
        scope_assessment=row["scope_assessment"],
        fail_closed=bool(row["fail_closed"]),
        fail_closed_reason=row["fail_closed_reason"],
        dissent_logged=bool(row["dissent_logged"]),
        escalated=bool(row["escalated"]),
        neckbeard_block=bool(row["neckbeard_block"]),
        latency_ms=row["latency_ms"],
        added_tokens=row["added_tokens"],
    )
