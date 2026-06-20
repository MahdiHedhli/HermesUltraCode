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
