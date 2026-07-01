"""The Hermes web-dashboard plugin: a native UltraCode tab (manifest + hand-authored JS +
thin FastAPI backend) that reuses the gate's data layer over the SHARED store."""

import json
import os
import unittest

import tests.helpers  # noqa: F401 - puts repo root on sys.path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = os.path.join(REPO, "dashboard")


class DashboardPluginTest(unittest.TestCase):
    def test_manifest_valid_and_conformant(self):
        with open(os.path.join(DASH, "manifest.json"), encoding="utf-8") as fh:
            m = json.load(fh)
        self.assertEqual(m["name"], "hermesultracode")
        self.assertEqual(m["tab"]["path"], "/ultracode")
        self.assertIn("position", m["tab"])           # native nav placement (near Kanban)
        self.assertEqual(m["entry"], "dist/index.js")
        self.assertEqual(m["api"], "plugin_api.py")

    def test_assets_present(self):
        for p in ("dist/index.js", "dist/style.css", "plugin_api.py", "manifest.json"):
            self.assertTrue(os.path.isfile(os.path.join(DASH, p)), p)

    def test_cost_tab_present(self):
        js = open(os.path.join(DASH, "dist/index.js"), encoding="utf-8").read()
        self.assertIn("costView", js)
        self.assertIn('["cost", "Cost"]', js)
        self.assertIn("/metrics", js)                  # cost view reads the routing block

    def test_roster_tab_and_reviewer_indicator(self):
        js = open(os.path.join(DASH, "dist/index.js"), encoding="utf-8").read()
        self.assertIn("reviewerBadge", js)             # prominent live reviewer-mode badge
        self.assertIn("rosterView", js)
        self.assertIn('["roster", "Roster"]', js)
        self.assertIn("/roster", js)
        api = open(os.path.join(DASH, "plugin_api.py"), encoding="utf-8").read()
        self.assertIn('@router.get("/roster")', api)
        self.assertIn("reviewer_mode", api)            # backend surfaces the live mode

    def test_record_summary_carries_routing_fields(self):
        from core.store import DispatchRecord
        from server.views import record_summary
        r = DispatchRecord(id="a", ts="t", base_prompt="b", added_directives=(),
                           dispatched_prompt="b", verdict="pass", tier="standard",
                           reviewer_model="m", decision="dispatched", round_count=1,
                           routed_model="local/gemma", routed_is_local=True, est_savings_usd=0.04)
        s = record_summary(r)
        for k in ("routed_model", "routed_is_local", "est_cost_usd", "est_savings_usd",
                  "route_reason", "route_required_tier"):
            self.assertIn(k, s)
        self.assertEqual(s["routed_model"], "local/gemma")

    def test_frontend_is_buildless_and_registers(self):
        js = open(os.path.join(DASH, "dist/index.js"), encoding="utf-8").read()
        self.assertIn("__HERMES_PLUGIN_SDK__", js)     # uses host React (no bundled React)
        self.assertIn('register("hermesultracode"', js)
        self.assertIn("/api/plugins/hermesultracode", js)
        self.assertNotIn("import ", js)                # no ES imports -> no build step

    def test_backend_reuses_shared_data_layer(self):
        # plugin_api.py reuses exactly these over the shared store; verify the contract.
        from core.store_sqlite import SqliteAuditStore
        from server import views
        st = SqliteAuditStore(":memory:")
        live = views.make_store_live_source(st)(st)
        for k in ("orchestrator", "active", "feed", "completed", "plan", "last_gate_decision"):
            self.assertIn(k, live)
        self.assertIn("total_dispatches", views.compute_metrics(st, None))
        self.assertIsInstance(views.default_queue_source(st), list)
        self.assertEqual(st.count_fail_closed(), 0)

    def test_plugin_api_syntax(self):
        import py_compile
        py_compile.compile(os.path.join(DASH, "plugin_api.py"), doraise=True)


if __name__ == "__main__":
    unittest.main()
