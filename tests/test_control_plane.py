"""Invariant #10 / acceptance criterion 10: no LLM path can create, edit, or select a
profile or credential. Verified by driving register() with a recording context and asserting
the control plane is registered ONLY as CLI commands, never as an agent tool."""

import os
import tempfile
import unittest

import tests.helpers  # noqa: F401 - puts repo root on sys.path


class RecordingCtx:
    """Captures every register_* call. Tool registrations are recorded verbatim so we can
    prove no control-plane operation leaks into the agent toolset."""

    def __init__(self):
        self.tools = []
        self.cli = []

    def register_tool(self, *a, **kw):
        self.tools.append(repr((a, kw)))

    def register_cli_command(self, name, *a, **kw):
        self.cli.append(name)

    def __getattr__(self, _name):
        return lambda *a, **kw: None      # any other register_* is a recording no-op


# Control-plane tokens that must NEVER appear inside an agent-tool registration.
_FORBIDDEN = ("reconcile", "create_profile", "reap", "validate_ready", "plan_roster",
              "roster", "credential")


class ControlPlaneSafetyTest(unittest.TestCase):
    def setUp(self):
        os.environ["HERMESULTRACODE_AUTO_DASHBOARD"] = "0"          # no background dashboard thread
        os.environ["HERMESULTRACODE_STORE"] = tempfile.mktemp(suffix=".sqlite3")

    def test_control_plane_is_cli_only_never_a_tool(self):
        import __init__ as plugin
        ctx = RecordingCtx()
        plugin.register(ctx)
        # the profile/roster control plane is present, as CLI commands
        self.assertLessEqual(
            {"ultracode-roster", "ultracode-reconcile", "ultracode-setup"}, set(ctx.cli))
        # ...and NONE of it is exposed to the model as a tool
        joined = " ".join(ctx.tools).lower()
        for token in _FORBIDDEN:
            self.assertNotIn(token, joined, f"control-plane token {token!r} leaked into an agent tool")


if __name__ == "__main__":
    unittest.main()
