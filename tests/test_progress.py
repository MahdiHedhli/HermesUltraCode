"""Live subagent-progress store + live source (the dashboard's real-time Live view)."""

import json
import unittest

import tests.helpers  # noqa: F401 - puts repo root on sys.path

from core.store_sqlite import SqliteAuditStore
from server.progress import LiveProgress
from server.views import make_progress_live_source


class ProgressStoreTest(unittest.TestCase):
    def test_lifecycle_feed_and_completion(self):
        p = LiveProgress()
        p.subagent_started("sa-1", "sess-1", "build a small thing", "leaf")
        p.tool_event("sess-1", "write_file", "ok", 12)
        p.tool_event("sess-1", "run_tests", "ok", 340)
        p.tool_event("untracked-session", "noise", "ok", 1)  # not a tracked subagent -> ignored

        snap = p.snapshot()
        self.assertEqual(len(snap["active"]), 1)
        a = snap["active"][0]
        self.assertEqual(a["subagent_id"], "sa-1")
        self.assertEqual(a["tool_count"], 2)
        self.assertEqual(a["last_tool"], "run_tests")
        self.assertIsNotNone(a["elapsed_s"])
        self.assertEqual(len(snap["feed"]), 2)              # untracked event excluded
        self.assertEqual(snap["feed"][0]["tool"], "run_tests")  # newest first

        p.subagent_stopped("sess-1", "done: built the thing", "completed", 500)
        snap2 = p.snapshot()
        self.assertEqual(snap2["active"], [])
        self.assertEqual(len(snap2["completed"]), 1)
        self.assertEqual(snap2["completed"][0]["status"], "completed")
        self.assertIn("built the thing", snap2["completed"][0]["summary"])

    def test_feed_is_capped(self):
        p = LiveProgress()
        p.subagent_started("sa", "s", "g")
        for _ in range(500):
            p.tool_event("s", "t", "ok", 1)
        self.assertLessEqual(len(p.snapshot()["feed"]), 50)

    def test_unknown_session_events_are_safe(self):
        p = LiveProgress()
        p.tool_event("nope", "x", "ok", 1)          # no subagent -> no-op
        p.subagent_stopped("nope", "s", "completed")  # no subagent -> no-op
        self.assertEqual(p.snapshot(), {"active": [], "feed": [], "completed": []})


class ProgressLiveSourceTest(unittest.TestCase):
    def test_shape_and_secret_redaction(self):
        p = LiveProgress()
        p.subagent_started("sa-1", "sess-1", "use key sk-abcdef0123456789ABCDEF in the build", "leaf")
        p.tool_event("sess-1", "write_file", "ok", 5)
        out = make_progress_live_source(p)(SqliteAuditStore(":memory:"))
        for k in ("orchestrator", "active", "feed", "completed"):
            self.assertIn(k, out)
        blob = json.dumps(out)
        self.assertNotIn("sk-abcdef0123456789ABCDEF", blob)   # secret in goal redacted
        self.assertIn("REDACTED", blob)


if __name__ == "__main__":
    unittest.main()
