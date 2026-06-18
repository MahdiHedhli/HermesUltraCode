"""The Hermes adapter is the only Hermes-coupled file. It must fail closed when it
cannot intercept dispatch, and must refuse to forward a prompt the gate did not
release (invariant 1 / criterion 1 at the boundary)."""

import unittest

from tests.helpers import make_gate, standard_meta, verdict_json

from adapters.hermes_hook import (
    AdapterUnavailable,
    GateBlocked,
    HermesGateAdapter,
    default_meta_builder,
)


class _FakeRuntimeNoHook:
    pass


class _FakeRuntimeWithHook:
    def __init__(self):
        self.registered = None

    def register_predispatch_hook(self, fn):
        self.registered = fn


class AdapterTest(unittest.TestCase):
    def test_register_fails_closed_without_runtime(self):
        gate = make_gate(reviewer_responses=[verdict_json("pass", [])])
        adapter = HermesGateAdapter(gate=gate)
        with self.assertRaises(AdapterUnavailable):
            adapter.register(hermes_runtime=None)

    def test_register_fails_closed_when_no_hook_surface(self):
        gate = make_gate(reviewer_responses=[verdict_json("pass", [])])
        adapter = HermesGateAdapter(gate=gate)
        with self.assertRaises(AdapterUnavailable):
            adapter.register(hermes_runtime=_FakeRuntimeNoHook())

    def test_register_attaches_when_hook_present(self):
        gate = make_gate(reviewer_responses=[verdict_json("pass", [])])
        adapter = HermesGateAdapter(gate=gate)
        rt = _FakeRuntimeWithHook()
        adapter.register(hermes_runtime=rt)
        # bound-method identity: compare the underlying function + instance.
        self.assertIsNotNone(rt.registered)
        self.assertIs(rt.registered.__func__, HermesGateAdapter.intercept)
        self.assertIs(rt.registered.__self__, adapter)

    def test_intercept_returns_dispatched_prompt_on_release(self):
        gate = make_gate(reviewer_responses=[verdict_json("pass", ["Only touch export/."])])
        adapter = HermesGateAdapter(gate=gate)
        out = adapter.intercept({"base_prompt": "Do the task.",
                                 "touched_paths": ["a.py", "b.py"]})
        self.assertTrue(out.startswith("Do the task."))
        self.assertIn("Only touch export/.", out)

    def test_intercept_raises_when_blocked(self):
        gate = make_gate(reviewer_responses=[verdict_json("block", rationale="nope")])
        adapter = HermesGateAdapter(gate=gate)
        with self.assertRaises(GateBlocked):
            adapter.intercept({"base_prompt": "Do the task.",
                               "touched_paths": ["a.py", "b.py"]})

    def test_intercept_fails_closed_on_reviewer_error(self):
        from core.providers import MockProvider, ProviderError
        gate = make_gate(reviewer=MockProvider(lab="reviewer-lab", model="r",
                                               raises=ProviderError("boom")))
        adapter = HermesGateAdapter(gate=gate)
        with self.assertRaises(GateBlocked) as ctx:
            adapter.intercept({"base_prompt": "Do.", "touched_paths": ["a.py", "b.py"]})
        self.assertTrue(ctx.exception.result.fail_closed)

    def test_meta_builder_defaults_are_conservative(self):
        meta = default_meta_builder({"base_prompt": "x"})
        self.assertFalse(meta.carries_merge_authority)
        self.assertFalse(meta.read_only)

    def test_unextractable_prompt_fails_closed(self):
        from adapters.hermes_hook import GateBlockedExtractionError
        gate = make_gate(reviewer_responses=[verdict_json("pass", [])])
        adapter = HermesGateAdapter(gate=gate)
        with self.assertRaises(GateBlockedExtractionError):
            adapter.intercept({"no_prompt_here": "oops"})


if __name__ == "__main__":
    unittest.main()
