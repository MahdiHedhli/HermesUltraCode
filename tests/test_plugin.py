"""The Hermes plugin packaging: register(ctx) wires the real seams and fails closed;
the plugin.yaml manifest and neckbeard SKILL.md conform to the Hermes formats."""

import importlib.util
import json
import os
import tempfile
import unittest

import tests.helpers  # noqa: F401 - puts repo root on sys.path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REVIEWER_ENV = ("HERMESULTRACODE_REVIEWER_API_KEY", "HERMESULTRACODE_REVIEWER_LAB",
                 "HERMESULTRACODE_REVIEWER_MODEL", "HERMESULTRACODE_REVIEWER_BASE_URL",
                 "HERMESULTRACODE_ORCH_LAB", "HERMESULTRACODE_STORE",
                 "HERMESULTRACODE_AUTO_DASHBOARD")


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "hermesultracode_plugin", os.path.join(REPO_ROOT, "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    def __init__(self):
        self.hooks, self.middleware, self.tools, self.skills, self.cli = {}, {}, {}, {}, {}
        self.commands = {}
        self.dispatch_calls = []
        self.dispatch_result = '{"result": "subagent-done"}'

    def register_hook(self, name, cb):
        self.hooks.setdefault(name, []).append(cb)

    def register_middleware(self, kind, cb):
        self.middleware.setdefault(kind, []).append(cb)

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}

    def register_skill(self, name, path, description=""):
        self.skills[name] = {"path": path, "description": description}

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli[name] = {"help": help, "handler_fn": handler_fn}

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = {"handler": handler, "args_hint": args_hint}

    def dispatch_tool(self, tool_name, args, **kw):
        self.dispatch_calls.append((tool_name, dict(args)))
        return self.dispatch_result


class PluginRegisterTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _REVIEWER_ENV}
        self._tmp = tempfile.mkdtemp()
        for k in _REVIEWER_ENV:
            os.environ.pop(k, None)
        os.environ["HERMESULTRACODE_STORE"] = os.path.join(self._tmp, "audit.sqlite3")
        os.environ["HERMESULTRACODE_AUTO_DASHBOARD"] = "0"  # don't bind a port in tests
        self.plugin = load_plugin()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _set_reviewer(self):
        os.environ["HERMESULTRACODE_REVIEWER_API_KEY"] = "sk-or-fake0123456789ABCDEF"
        os.environ["HERMESULTRACODE_REVIEWER_LAB"] = "anthropic"
        os.environ["HERMESULTRACODE_ORCH_LAB"] = "nous"

    def test_always_installs_block_hook_and_tighten_middleware(self):
        ctx = FakeCtx()
        self.plugin.register(ctx)
        self.assertIn("pre_tool_call", ctx.hooks)
        self.assertIn("tool_request", ctx.middleware)

    def test_failclosed_when_no_reviewer_configured(self):
        ctx = FakeCtx()
        self.plugin.register(ctx)  # no reviewer env -> fail-closed mode
        block = ctx.hooks["pre_tool_call"][0]
        out = block("delegate_task", {"goal": "do x", "toolsets": ["files"]}, tool_call_id="t")
        self.assertEqual(out["action"], "block")
        self.assertIn("fail-closed", out["message"])

    def test_active_when_reviewer_configured(self):
        self._set_reviewer()
        ctx = FakeCtx()
        self.plugin.register(ctx)
        # gate active: distinct labs validated at build, no network during register
        block = ctx.hooks["pre_tool_call"][0]
        # a non-delegate tool is ignored (no gate call, no network)
        self.assertIsNone(block("write_file", {"path": "x"}, tool_call_id="t"))

    def test_registers_query_tools(self):
        self._set_reviewer()
        ctx = FakeCtx()
        self.plugin.register(ctx)
        for t in ("gate_metrics", "gate_audit_query", "gate_recent_verdicts"):
            self.assertIn(t, ctx.tools)
            self.assertEqual(ctx.tools[t]["toolset"], "hermesultracode")

    def test_query_tool_returns_json(self):
        self._set_reviewer()
        ctx = FakeCtx()
        self.plugin.register(ctx)
        out = ctx.tools["gate_metrics"]["handler"]({})
        data = json.loads(out)
        self.assertIn("total_dispatches", data)

    def test_registers_skill_and_dashboard_cli(self):
        self._set_reviewer()
        ctx = FakeCtx()
        self.plugin.register(ctx)
        self.assertIn("neckbeard", ctx.skills)
        self.assertIn("ultracode-dashboard", ctx.cli)

    def test_registers_ultracode_command_and_session_hook(self):
        self._set_reviewer()
        ctx = FakeCtx()
        self.plugin.register(ctx)
        self.assertIn("ultracode", ctx.commands)
        self.assertEqual(ctx.commands["ultracode"]["args_hint"], "<task>")
        self.assertIn("on_session_start", ctx.hooks)

    def test_ultracode_status_shows_gate_and_usage(self):
        self._set_reviewer()
        ctx = FakeCtx()
        self.plugin.register(ctx)
        out = ctx.commands["ultracode"]["handler"]("")          # no args -> status
        self.assertIn("HermesUltraCode gate", out)
        self.assertIn("Usage", out)
        self.assertEqual(ctx.dispatch_calls, [])                # status never delegates

    def test_ultracode_failclosed_blocks_delegation(self):
        ctx = FakeCtx()
        self.plugin.register(ctx)                               # no reviewer -> fail-closed
        out = ctx.commands["ultracode"]["handler"]("do the risky thing")
        self.assertIn("did NOT release", out)
        self.assertEqual(ctx.dispatch_calls, [])                # blocked -> never dispatched

    def test_hyperlink_osc8_and_toggle(self):
        h = self.plugin._hyperlink
        on = h("http://127.0.0.1:9120/?token=abc")
        self.assertIn("\033]8;;http://127.0.0.1:9120/?token=abc", on)  # OSC 8 link emitted
        self.assertIn("http://127.0.0.1:9120/?token=abc", on)          # visible/copyable label
        os.environ["HERMESULTRACODE_HYPERLINKS"] = "0"
        try:
            self.assertEqual(h("http://x/y"), "http://x/y")            # plain when disabled
        finally:
            os.environ.pop("HERMESULTRACODE_HYPERLINKS", None)

    def test_ultracode_active_delegates_via_dispatch_tool(self):
        # Active path with a MOCK gate (no network): release -> dispatch_tool(delegate_task).
        from adapters.hermes_hook import HermesDispatchGate
        from tests.helpers import make_gate, verdict_json
        hdg = HermesDispatchGate(gate=make_gate(reviewer_responses=[verdict_json("pass", ["Add tests."])]))
        ctx = FakeCtx()
        handler = self.plugin._make_ultracode_command(ctx, hdg, os.environ["HERMESULTRACODE_STORE"])
        out = handler("implement a CSV exporter")
        self.assertEqual(len(ctx.dispatch_calls), 1)
        tool, args = ctx.dispatch_calls[0]
        self.assertEqual(tool, "delegate_task")
        self.assertTrue(args["goal"].startswith("implement a CSV exporter"))
        self.assertIn("Add tests.", args["goal"])               # gate tightening reached delegate_task
        self.assertIn("subagent-done", out)

    def test_same_lab_reviewer_falls_back_to_failclosed(self):
        # reviewer lab == orchestrator lab must NOT silently activate the gate
        os.environ["HERMESULTRACODE_REVIEWER_API_KEY"] = "sk-or-fake0123456789ABCDEF"
        os.environ["HERMESULTRACODE_REVIEWER_LAB"] = "nous"
        os.environ["HERMESULTRACODE_ORCH_LAB"] = "nous"
        ctx = FakeCtx()
        self.plugin.register(ctx)
        out = ctx.hooks["pre_tool_call"][0]("delegate_task", {"goal": "x"}, tool_call_id="t")
        self.assertEqual(out["action"], "block")


class ManifestAndSkillTest(unittest.TestCase):
    def test_plugin_yaml_present_and_conformant(self):
        with open(os.path.join(REPO_ROOT, "plugin.yaml"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("name: hermesultracode", text)
        self.assertIn("version:", text)
        self.assertIn("description:", text)
        self.assertIn("provides_hooks:", text)
        self.assertIn("pre_tool_call", text)
        for t in ("gate_audit_query", "gate_metrics", "gate_recent_verdicts"):
            self.assertIn(t, text)
        self.assertIn("requires_env:", text)
        self.assertIn("HERMESULTRACODE_REVIEWER_API_KEY", text)

    def test_init_py_present_for_discovery(self):
        # Hermes discovery requires plugin.yaml AND __init__.py co-located.
        self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, "__init__.py")))
        self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, "plugin.yaml")))

    def test_neckbeard_skill_frontmatter(self):
        with open(os.path.join(REPO_ROOT, "skills", "neckbeard", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---"))
        self.assertIn("name: neckbeard", text)
        self.assertIn("description:", text)
        self.assertIn("version:", text)
        self.assertIn("platforms:", text)
        self.assertIn("metadata:", text)
        self.assertIn("hermes:", text)
        self.assertIn("tags:", text)
        # the extended protected set must be present
        for item in ("observability", "audit logging", "idempotency", "retries"):
            self.assertIn(item, text.lower())


if __name__ == "__main__":
    unittest.main()
