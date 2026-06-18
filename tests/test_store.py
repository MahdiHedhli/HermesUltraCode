"""Acceptance criterion 7: every dispatch writes an immutable audit row; secrets
redacted; exportable to JSON and CSV; queryable by tier/verdict/date."""

import csv
import io
import json
import sqlite3
import unittest

from core.store import (
    DECISION_DISPATCHED,
    AuditFilter,
    DispatchRecord,
)
from core.store_sqlite import SqliteAuditStore


def rec(id="r1", ts="2026-06-18T10:00:00+00:00", tier="standard", verdict="pass",
        base="do the thing", directives=("only touch x/",), decision=DECISION_DISPATCHED,
        fail_closed=False):
    return DispatchRecord(
        id=id, ts=ts, base_prompt=base, added_directives=tuple(directives),
        dispatched_prompt=base + "\n- " + "; ".join(directives), verdict=verdict,
        tier=tier, reviewer_model="reviewer-x", decision=decision, round_count=1,
        rationale="ok", scope_assessment="in_scope", fail_closed=fail_closed,
    )


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = SqliteAuditStore(":memory:")

    def test_append_and_get(self):
        rid = self.store.append(rec())
        got = self.store.get(rid)
        self.assertIsNotNone(got)
        self.assertEqual(got.verdict, "pass")
        self.assertEqual(got.added_directives, ("only touch x/",))

    def test_append_idempotent_on_id(self):
        self.store.append(rec(id="dup"))
        self.store.append(rec(id="dup"))  # second write is a no-op
        self.assertEqual(len(self.store.all()), 1)

    def test_update_forbidden(self):
        self.store.append(rec(id="r1"))
        # The append-only trigger raises ABORT, surfaced as sqlite3.IntegrityError.
        with self.assertRaises(sqlite3.DatabaseError):
            self.store._conn.execute("UPDATE dispatch_audit SET verdict='block' WHERE id='r1'")
        # the row is unchanged
        self.assertEqual(self.store.get("r1").verdict, "pass")

    def test_delete_forbidden(self):
        self.store.append(rec(id="r1"))
        with self.assertRaises(sqlite3.DatabaseError):
            self.store._conn.execute("DELETE FROM dispatch_audit WHERE id='r1'")
        self.assertIsNotNone(self.store.get("r1"))

    def test_secrets_redacted_on_write(self):
        secret_base = "use key sk-ABCDEF0123456789ABCDEF and AKIAIOSFODNN7EXAMPLE"
        self.store.append(rec(id="s1", base=secret_base, directives=("password=hunter2longenough",)))
        got = self.store.get("s1")
        self.assertNotIn("sk-ABCDEF0123456789ABCDEF", got.base_prompt)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", got.base_prompt)
        self.assertIn("REDACTED", got.base_prompt)
        self.assertNotIn("hunter2longenough", json.dumps(list(got.added_directives)))

    def test_query_filters(self):
        self.store.append(rec(id="a", tier="standard", verdict="pass"))
        self.store.append(rec(id="b", tier="elevated", verdict="block"))
        self.store.append(rec(id="c", tier="elevated", verdict="pass"))
        self.assertEqual({r.id for r in self.store.query(AuditFilter(tier="elevated"))}, {"b", "c"})
        self.assertEqual({r.id for r in self.store.query(AuditFilter(verdict="block"))}, {"b"})

    def test_query_date_range(self):
        self.store.append(rec(id="old", ts="2026-06-01T00:00:00+00:00"))
        self.store.append(rec(id="new", ts="2026-06-18T00:00:00+00:00"))
        got = self.store.query(AuditFilter(ts_from="2026-06-10T00:00:00+00:00"))
        self.assertEqual({r.id for r in got}, {"new"})

    def test_fail_closed_counter(self):
        self.store.append(rec(id="ok", fail_closed=False))
        self.store.append(rec(id="fc1", fail_closed=True))
        self.store.append(rec(id="fc2", fail_closed=True))
        self.assertEqual(self.store.count_fail_closed(), 2)

    def test_export_json(self):
        self.store.append(rec(id="r1"))
        data = json.loads(self.store.export_json())
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], "r1")
        self.assertIsInstance(data[0]["added_directives"], list)

    def test_export_csv(self):
        self.store.append(rec(id="r1"))
        self.store.append(rec(id="r2"))
        reader = csv.DictReader(io.StringIO(self.store.export_csv()))
        rows = list(reader)
        self.assertEqual(len(rows), 2)
        self.assertIn("dispatched_prompt", rows[0])
        # added_directives serialised as JSON string in CSV
        self.assertTrue(rows[0]["added_directives"].startswith("["))


if __name__ == "__main__":
    unittest.main()
