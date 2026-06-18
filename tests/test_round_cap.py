"""Acceptance criterion 5: round cap honored (default 2); on exhaustion the
tier-specific fallback fires (auto-accept + log dissent for standard; escalate for
elevated/merge_adjacent)."""

import unittest

from tests.helpers import make_gate, standard_meta, verdict_json

from core.providers import MockProvider
from core.store import (
    DECISION_DISPATCHED,
    DECISION_DISPATCHED_FALLBACK,
    DECISION_ESCALATED,
)
from core.tiering import DispatchMeta

BASE = "Add input validation to the upload handler."


def elevated_meta():
    return DispatchMeta(touched_paths=("src/auth/upload.py",), read_only=False)


class RoundCapTest(unittest.TestCase):
    def test_revise_then_pass_within_cap(self):
        gate = make_gate(
            reviewer_responses=[
                verdict_json("revise", ["Reject files over 10MB."]),
                verdict_json("pass", []),
            ],
            round_cap=2,
        )
        res = gate.review_and_dispatch(BASE, standard_meta())
        self.assertTrue(res.released)
        self.assertEqual(res.decision, DECISION_DISPATCHED)
        self.assertEqual(res.round_count, 2)
        self.assertIn("Reject files over 10MB.", res.dispatched_prompt)

    def test_cap_honored_reviewer_called_at_most_cap_times(self):
        reviewer = MockProvider(
            lab="reviewer-lab",
            model="r",
            responses=[
                verdict_json("revise", ["d1"]),
                verdict_json("revise", ["d2"]),
                verdict_json("revise", ["d3"]),  # should never be consumed
            ],
        )
        gate = make_gate(reviewer=reviewer, round_cap=2)
        gate.review_and_dispatch(BASE, standard_meta())
        self.assertEqual(reviewer.calls, 2)

    def test_standard_exhaustion_autoaccepts_with_dissent(self):
        gate = make_gate(
            reviewer_responses=[
                verdict_json("revise", ["d1"]),
                verdict_json("revise", ["d2"]),
            ],
            round_cap=2,
        )
        res = gate.review_and_dispatch(BASE, standard_meta())
        self.assertTrue(res.released)
        self.assertEqual(res.decision, DECISION_DISPATCHED_FALLBACK)
        self.assertTrue(res.dissent_logged)
        self.assertFalse(res.fail_closed)
        # the base survives verbatim and both directives are appended
        self.assertTrue(res.dispatched_prompt.startswith(BASE))
        self.assertIn("d1", res.dispatched_prompt)
        self.assertIn("d2", res.dispatched_prompt)

    def test_elevated_exhaustion_escalates(self):
        gate = make_gate(
            reviewer_responses=[
                verdict_json("revise", ["d1"]),
                verdict_json("revise", ["d2"]),
            ],
            round_cap=2,
        )
        res = gate.review_and_dispatch(BASE, elevated_meta())
        self.assertEqual(res.tier, "elevated")
        self.assertFalse(res.released)
        self.assertEqual(res.decision, DECISION_ESCALATED)
        self.assertTrue(res.escalated)
        self.assertTrue(res.dissent_logged)

    def test_merge_exhaustion_escalates(self):
        gate = make_gate(
            reviewer_responses=[
                verdict_json("revise", ["d1"]),
                verdict_json("revise", ["d2"]),
            ],
            round_cap=2,
        )
        meta = DispatchMeta(carries_merge_authority=True, touched_paths=("src/x.py",))
        res = gate.review_and_dispatch(BASE, meta)
        self.assertEqual(res.tier, "merge_adjacent")
        self.assertFalse(res.released)
        self.assertEqual(res.decision, DECISION_ESCALATED)

    def test_custom_cap_one(self):
        reviewer = MockProvider(
            lab="reviewer-lab", model="r",
            responses=[verdict_json("revise", ["d1"]), verdict_json("pass", [])],
        )
        gate = make_gate(reviewer=reviewer, round_cap=1)
        res = gate.review_and_dispatch(BASE, standard_meta())
        # one round only -> revise exhausts immediately -> standard auto-accept
        self.assertEqual(reviewer.calls, 1)
        self.assertEqual(res.decision, DECISION_DISPATCHED_FALLBACK)


if __name__ == "__main__":
    unittest.main()
