"""Audit store interface (invariant 7: one immutable row per dispatch).

Storage sits behind this interface so the core stays testable and portable. The
default is ``store_sqlite.SqliteAuditStore`` (stdlib, no new dependency). A
Cloudflare D1 adapter is a later swap against this same seam — not built now.

Immutability is part of the contract: ``append`` is the only mutator. There is no
update or delete. Implementations SHOULD enforce this structurally (e.g. SQLite
triggers) as well as by omission.
"""

from __future__ import annotations

import abc
import csv
import io
import json
from dataclasses import asdict, dataclass, field
from typing import Any

# Decisions the dispatcher records. Distinct from the verdict: the *decision* is
# code's, the *verdict* is the reviewer's.
DECISION_DISPATCHED = "dispatched"
DECISION_DISPATCHED_FALLBACK = "dispatched_fallback"  # standard-tier auto-accept on exhaustion
DECISION_BLOCKED = "blocked"
DECISION_ESCALATED = "escalated"

# Canonical column order for the immutable row + exports.
COLUMNS: tuple[str, ...] = (
    "id",
    "ts",
    "base_prompt",
    "added_directives",
    "dispatched_prompt",
    "verdict",
    "tier",
    "reviewer_model",
    "decision",
    "round_count",
    "rationale",
    "scope_assessment",
    "fail_closed",
    "fail_closed_reason",
    "dissent_logged",
    "escalated",
    "ponytail_block",
    "latency_ms",
    "added_tokens",
)


@dataclass(frozen=True)
class DispatchRecord:
    """One immutable audit row. ``added_directives`` is stored as a JSON list."""

    id: str
    ts: str
    base_prompt: str
    added_directives: tuple[str, ...]
    dispatched_prompt: str
    verdict: str
    tier: str
    reviewer_model: str
    decision: str
    round_count: int
    rationale: str = ""
    scope_assessment: str = ""
    fail_closed: bool = False
    fail_closed_reason: str = ""
    dissent_logged: bool = False
    escalated: bool = False
    # True when the block was specifically a protected-set / compliance violation,
    # surfaced on the dashboard ponytail view.
    ponytail_block: bool = False
    # Observability (extended protected set): wall-clock of the whole review +
    # a cheap proxy for the tokens the gate added on top of the base.
    latency_ms: int = 0
    added_tokens: int = 0

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["added_directives"] = list(self.added_directives)
        return d

    def to_export_dict(self) -> dict[str, Any]:
        """Flat, CSV-friendly view (lists -> JSON strings, bools -> 0/1)."""
        d = self.to_row()
        d["added_directives"] = json.dumps(d["added_directives"], ensure_ascii=False)
        for b in ("fail_closed", "dissent_logged", "escalated", "ponytail_block"):
            d[b] = int(bool(d[b]))
        return d


@dataclass(frozen=True)
class AuditFilter:
    """Filters for querying the audit trail."""

    tier: str | None = None
    verdict: str | None = None
    decision: str | None = None
    fail_closed: bool | None = None
    ts_from: str | None = None  # ISO inclusive
    ts_to: str | None = None  # ISO inclusive
    limit: int | None = None


class AuditStore(abc.ABC):
    """Append-only, queryable, exportable audit trail."""

    @abc.abstractmethod
    def append(self, record: DispatchRecord) -> str:
        """Persist one immutable row. Returns the row id. Idempotent on id."""

    @abc.abstractmethod
    def get(self, record_id: str) -> DispatchRecord | None:
        ...

    @abc.abstractmethod
    def query(self, flt: AuditFilter | None = None) -> list[DispatchRecord]:
        ...

    def all(self) -> list[DispatchRecord]:
        return self.query(None)

    def count_fail_closed(self) -> int:
        return len(self.query(AuditFilter(fail_closed=True)))

    # ---- exports (ISO 27001 evidence) ----

    def export_json(self, flt: AuditFilter | None = None) -> str:
        rows = [r.to_row() for r in self.query(flt)]
        return json.dumps(rows, ensure_ascii=False, indent=2)

    def export_csv(self, flt: AuditFilter | None = None) -> str:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(COLUMNS))
        writer.writeheader()
        for r in self.query(flt):
            writer.writerow(r.to_export_dict())
        return buf.getvalue()


class ImmutabilityError(RuntimeError):
    """Raised on any attempt to mutate an existing audit row."""
