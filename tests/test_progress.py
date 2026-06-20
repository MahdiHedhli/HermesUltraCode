"""Live progress store (orchestrator + subagents + plan) and the dashboard live source."""

import json
import unittest

import tests.helpers  # noqa: F401 - puts repo root on sys.path

from core.store_sqlite import SqliteAuditStore
from server.progress import LiveProgress, plan_progress
from server.views import make_progress_live_source


class ProgressStoreTest(unittest.TestCase):
    def test_lifecycle_feed_and_orchestrator(self):
        p = LiveProgress()
        p.subagent_started("sa-1", "sess-1", "build a small thing", "leaf")
        p.tool_event("sess-1", "write_file", "ok", 12)
        p.tool_event("sess-1", "run_tests", "ok", 340)
        p.tool_event("orch-session", "delegate_task", "ok", 5)  # untracked -> orchestrator

        snap = p.snapshot()
        self.assertEqual(len(snap["active"]), 1)
        a = snap["active"][0]
        self.assertEqual(a["subagent_id"], "sa-1")
        self.assertEqual(a["tool_count"], 2)
        self.assertEqual(a["last_tool"], "run_tests")
        self.assertIsNotNone(a["elapsed_s"])
        self.assertEqual(len(a["log"]), 2)                       # per-agent tool log
        # orchestrator captured the untracked tool call
        self.assertEqual(snap["orchestrator"]["tool_count"], 1)
        self.assertEqual(snap["orchestrator"]["last_tool"], "delegate_task")
        self.assertTrue(snap["orchestrator"]["active"])
        # feed has all three, newest first
        self.assertEqual(len(snap["feed"]), 3)
        self.assertEqual(snap["feed"][0]["tool"], "delegate_task")
        self.assertEqual(snap["feed"][0]["subagent_id"], "orchestrator")

        p.subagent_stopped("sess-1", "done: built the thing", "completed", 500)
        snap2 = p.snapshot()
        self.assertEqual(snap2["active"], [])
        self.assertEqual(len(snap2["completed"]), 1)
        self.assertIn("built the thing", snap2["completed"][0]["summary"])

    def test_plan_capture_merge_and_progress(self):
        p = LiveProgress()
        p.set_plan([
            {"id": "1", "content": "backend API", "status": "completed"},
            {"id": "2", "content": "frontend", "status": "in_progress"},
            {"id": "3", "content": "tests", "status": "pending"},
        ])
        items = p.snapshot()["plan"]["items"]
        self.assertEqual([i["content"] for i in items], ["backend API", "frontend", "tests"])
        prog = plan_progress(items)
        self.assertEqual((prog["total"], prog["done"], prog["active"], prog["todo"], prog["pct"]),
                         (3, 1, 1, 1, 33))
        # a partial (merge-mode) write updates status without dropping items or content
        p.set_plan([{"id": "2", "status": "completed"}])
        merged = p.snapshot()["plan"]["items"]
        self.assertEqual(len(merged), 3)
        two = next(i for i in merged if i["id"] == "2")
        self.assertEqual(two["status"], "completed")
        self.assertEqual(two["content"], "frontend")            # content preserved across merge

    def test_feed_is_capped(self):
        p = LiveProgress()
        p.subagent_started("sa", "s", "g")
        for _ in range(500):
            p.tool_event("s", "t", "ok", 1)
        self.assertLessEqual(len(p.snapshot()["feed"]), 60)

    def test_unknown_session_goes_to_orchestrator(self):
        p = LiveProgress()
        p.tool_event("nope", "search", "ok", 1)                 # orchestrator activity
        p.subagent_stopped("nope", "s", "completed")            # no such subagent -> no-op
        snap = p.snapshot()
        self.assertEqual(snap["active"], [])
        self.assertEqual(snap["completed"], [])
        self.assertEqual(snap["orchestrator"]["tool_count"], 1)
        self.assertEqual(len(snap["feed"]), 1)


class ProgressLiveSourceTest(unittest.TestCase):
    def test_shape_and_secret_redaction(self):
        p = LiveProgress()
        p.subagent_started("sa-1", "sess-1", "use key sk-abcdef0123456789ABCDEF in the build", "leaf")
        p.tool_event("sess-1", "write_file", "ok", 5)
        p.set_plan([{"id": "1", "content": "scaffold", "status": "in_progress"}])
        out = make_progress_live_source(p)(SqliteAuditStore(":memory:"))
        for k in ("orchestrator", "active", "feed", "completed", "plan"):
            self.assertIn(k, out)
        self.assertIn("progress", out["plan"])
        self.assertEqual(out["plan"]["items"][0]["state"], "active")   # in_progress -> active
        blob = json.dumps(out)
        self.assertNotIn("sk-abcdef0123456789ABCDEF", blob)            # secret in goal redacted
        self.assertIn("REDACTED", blob)


if __name__ == "__main__":
    unittest.main()
