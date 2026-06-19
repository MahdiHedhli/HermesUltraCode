"""The Hermes adapter wires the gate into the real dispatch seam: ``tool_request``
middleware (tighten) + ``pre_tool_call`` hook (block) on the ``delegate_task`` tool,
coordinated by ``tool_call_id``. It must fail closed and run the gate once."""

import unittest

from tests.helpers import make_gate, verdict_json

from adapters.hermes_hook import (
    DISPATCH_TOOL,
    GOAL_ARG,
    GateBlocked,
    HermesDispatchGate,
    _meta_from,
)
from core.providers import MockProvider, ProviderError
from core.tiering import (
    TIER_ELEVATED,
    TIER_MERGE_ADJACENT,
    TIER_STANDARD,
    TIER_TRIVIAL,
    classify,
)

DELEGATE_ARGS = {"goal": "Refactor the CSV exporter.", "context": "module export/csv.py",
                 "toolsets": ["files", "terminal"]}


def hdg(reviewer_responses=None, **kw):
    gate = make_gate(reviewer_responses=reviewer_responses, **kw)
    return HermesDispatchGate(gate=gate), gate


class AdapterSeamTest(unittest.TestCase):
    def test_tool_request_tightens_goal_on_release(self):
        h, _ = hdg([verdict_json("pass", ["Add unit tests for empty input."])])
        out = h.tool_request(DISPATCH_TOOL, dict(DELEGATE_ARGS), tool_call_id="tc1")
        self.assertIsInstance(out, dict)
        self.assertIn("args", out)
        self.assertTrue(out["args"][GOAL_ARG].startswith("Refactor the CSV exporter."))
        self.assertIn("Add unit tests for empty input.", out["args"][GOAL_ARG])

    def test_pre_tool_call_allows_released(self):
        h, _ = hdg([verdict_json("pass", [])])
        self.assertIsNone(h.pre_tool_call(DISPATCH_TOOL, dict(DELEGATE_ARGS), tool_call_id="tc1"))

    def test_pre_tool_call_blocks_on_block_verdict(self):
        h, _ = hdg([verdict_json("block", rationale="fundamentally unsafe")])
        out = h.pre_tool_call(DISPATCH_TOOL, dict(DELEGATE_ARGS), tool_call_id="tc1")
        self.assertEqual(out["action"], "block")
        self.assertIn("HermesUltraCode gate", out["message"])

    def test_gate_runs_once_across_both_seams(self):
        # tool_request then pre_tool_call with the SAME tool_call_id -> one model call.
        reviewer = MockProvider(lab="reviewer-lab", model="r",
                                responses=[verdict_json("pass", ["only export/"])])
        h = HermesDispatchGate(gate=make_gate(reviewer=reviewer))
        args = dict(DELEGATE_ARGS)
        rewritten = h.tool_request(DISPATCH_TOOL, args, tool_call_id="tcX")
        # pre_tool_call sees the rewritten (tightened) goal, like the real agent loop
        h.pre_tool_call(DISPATCH_TOOL, rewritten["args"], tool_call_id="tcX")
        self.assertEqual(reviewer.calls, 1)

    def test_idempotent_when_pre_sees_tightened_goal(self):
        # Even without a cache hit, a tightened goal must recover its base, not stack.
        reviewer = MockProvider(lab="reviewer-lab", model="r",
                                responses=[verdict_json("pass", ["dir-only"]),
                                           verdict_json("pass", ["dir-only"])])
        h = HermesDispatchGate(gate=make_gate(reviewer=reviewer))
        out = h.tool_request(DISPATCH_TOOL, dict(DELEGATE_ARGS), tool_call_id="")
        # different empty id path -> pre re-decides on rewritten goal; base recovered
        res = h.pre_tool_call(DISPATCH_TOOL, out["args"], tool_call_id="")
        self.assertIsNone(res)  # still released
        # the goal was not double-tightened (single directive block)
        self.assertEqual(out["args"][GOAL_ARG].count("dir-only"), 1)

    def test_non_delegate_tool_ignored(self):
        h, _ = hdg([verdict_json("pass", [])])
        self.assertIsNone(h.tool_request("write_file", {"path": "x", "content": "y"}, tool_call_id="t"))
        self.assertIsNone(h.pre_tool_call("write_file", {"path": "x"}, tool_call_id="t"))

    def test_assert_release_returns_or_raises(self):
        h_ok, _ = hdg([verdict_json("pass", ["c1"])])
        self.assertIn("c1", h_ok.assert_release(dict(DELEGATE_ARGS)))
        h_block, _ = hdg([verdict_json("block")])
        with self.assertRaises(GateBlocked):
            h_block.assert_release(dict(DELEGATE_ARGS))


class AdapterFailClosedTest(unittest.TestCase):
    def test_none_gate_blocks_all_delegate(self):
        h = HermesDispatchGate(gate=None, config_error="no reviewer key")
        out = h.pre_tool_call(DISPATCH_TOOL, dict(DELEGATE_ARGS), tool_call_id="t")
        self.assertEqual(out["action"], "block")
        self.assertIn("fail-closed", out["message"])
        # tool_request leaves args alone; the block is the enforcement point
        self.assertIsNone(h.tool_request(DISPATCH_TOOL, dict(DELEGATE_ARGS), tool_call_id="t"))

    def test_reviewer_error_fails_closed_to_block(self):
        gate = make_gate(reviewer=MockProvider(lab="reviewer-lab", model="r",
                                               raises=ProviderError("boom")))
        h = HermesDispatchGate(gate=gate)
        out = h.pre_tool_call(DISPATCH_TOOL, dict(DELEGATE_ARGS), tool_call_id="t")
        self.assertEqual(out["action"], "block")


class AdapterMetaTest(unittest.TestCase):
    # Assert the resulting TIER, not just the meta fields — a write-capable delegate
    # with unknown file paths must be reviewed (standard), never trivial-skipped.
    def test_readonly_toolsets_are_trivial(self):
        m = _meta_from(["search", "read"], "")
        self.assertTrue(m.read_only)
        self.assertEqual(classify(m), TIER_TRIVIAL)

    def test_merge_toolsets_carry_merge_authority(self):
        m = _meta_from(["files", "deploy"], "")
        self.assertTrue(m.carries_merge_authority)
        self.assertEqual(classify(m), TIER_MERGE_ADJACENT)

    def test_elevated_toolsets_get_protected_path(self):
        m = _meta_from(["git", "terminal"], "")
        self.assertEqual(classify(m), TIER_ELEVATED)

    def test_plain_write_toolsets_are_standard_not_trivial(self):
        # regression: ["files"] (no explicit paths) must be REVIEWED, not skipped
        self.assertEqual(classify(_meta_from(["files"], "")), TIER_STANDARD)

    def test_unknown_toolsets_default_to_standard(self):
        self.assertEqual(classify(_meta_from([], "")), TIER_STANDARD)
        self.assertEqual(classify(_meta_from(["whatever"], "")), TIER_STANDARD)


if __name__ == "__main__":
    unittest.main()
