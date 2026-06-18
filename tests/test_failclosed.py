"""Acceptance criteria 1 & 4: the dispatcher never releases without a present,
parseable, passing verdict; reviewer error/timeout/quota/empty/unparseable are
not-a-pass and fail closed per tier (block+escalate for elevated/merge_adjacent)."""

import unittest

from tests.helpers import make_gate, standard_meta, verdict_json

from core.providers import (
    MockProvider,
    ProviderEmptyResponse,
    ProviderError,
    ProviderQuotaError,
)
from core.store import DECISION_BLOCKED, DECISION_ESCALATED
from core.tiering import DispatchMeta

BASE = "Refactor the pagination helper. Keep the public signature stable."


def elevated_meta():
    # protected path -> elevated
    return DispatchMeta(touched_paths=("src/auth/login.py",), read_only=False)


def merge_meta():
    return DispatchMeta(carries_merge_authority=True, touched_paths=("src/x.py",))


class FailClosedTest(unittest.TestCase):
    def _reviewer_raising(self, exc):
        return MockProvider(lab="reviewer-lab", model="reviewer-x", raises=exc)

    def test_provider_error_blocks(self):
        gate = make_gate(reviewer=self._reviewer_raising(ProviderError("boom")))
        res = gate.review_and_dispatch(BASE, standard_meta())
        self.assertFalse(res.released)
        self.assertTrue(res.fail_closed)
        self.assertEqual(res.decision, DECISION_BLOCKED)

    def test_quota_blocks(self):
        gate = make_gate(reviewer=self._reviewer_raising(ProviderQuotaError("429")))
        res = gate.review_and_dispatch(BASE, standard_meta())
        self.assertFalse(res.released)
        self.assertTrue(res.fail_closed)

    def test_empty_response_blocks(self):
        gate = make_gate(reviewer=MockProvider(lab="reviewer-lab", model="r", response=""))
        res = gate.review_and_dispatch(BASE, standard_meta())
        self.assertFalse(res.released)
        self.assertTrue(res.fail_closed)
        self.assertIn("empty", res.fail_closed_reason.lower())

    def test_provider_empty_exception_blocks(self):
        gate = make_gate(reviewer=self._reviewer_raising(ProviderEmptyResponse("nada")))
        res = gate.review_and_dispatch(BASE, standard_meta())
        self.assertFalse(res.released)
        self.assertTrue(res.fail_closed)

    def test_unparseable_verdict_blocks(self):
        gate = make_gate(reviewer=MockProvider(lab="reviewer-lab", model="r",
                                               response="not json at all"))
        res = gate.review_and_dispatch(BASE, standard_meta())
        self.assertFalse(res.released)
        self.assertTrue(res.fail_closed)
        self.assertIn("unparseable", res.fail_closed_reason.lower())

    def test_missing_verdict_field_blocks(self):
        gate = make_gate(reviewer=MockProvider(lab="reviewer-lab", model="r",
                                               response='{"rationale": "hi"}'))
        res = gate.review_and_dispatch(BASE, standard_meta())
        self.assertFalse(res.released)
        self.assertTrue(res.fail_closed)

    def test_timeout_blocks(self):
        slow = MockProvider(lab="reviewer-lab", model="r", response=verdict_json(), delay=0.5)
        gate = make_gate(reviewer=slow, reviewer_timeout_s=0.05)
        res = gate.review_and_dispatch(BASE, standard_meta())
        self.assertFalse(res.released)
        self.assertTrue(res.fail_closed)
        self.assertIn("timed out", res.fail_closed_reason.lower())

    def test_elevated_escalates_on_failclosed(self):
        gate = make_gate(reviewer=self._reviewer_raising(ProviderError("boom")))
        res = gate.review_and_dispatch(BASE, elevated_meta())
        self.assertEqual(res.tier, "elevated")
        self.assertTrue(res.escalated)
        self.assertEqual(res.decision, DECISION_ESCALATED)

    def test_merge_escalates_on_failclosed(self):
        gate = make_gate(reviewer=self._reviewer_raising(ProviderError("boom")))
        res = gate.review_and_dispatch(BASE, merge_meta())
        self.assertEqual(res.tier, "merge_adjacent")
        self.assertTrue(res.escalated)
        self.assertEqual(res.decision, DECISION_ESCALATED)

    def test_standard_blocks_without_escalation_on_failclosed(self):
        gate = make_gate(reviewer=self._reviewer_raising(ProviderError("boom")))
        res = gate.review_and_dispatch(BASE, standard_meta())
        self.assertEqual(res.tier, "standard")
        self.assertFalse(res.escalated)
        self.assertEqual(res.decision, DECISION_BLOCKED)

    def test_block_verdict_never_dispatches(self):
        gate = make_gate(reviewer_responses=[verdict_json("block", rationale="fundamentally wrong")])
        res = gate.review_and_dispatch(BASE, standard_meta())
        self.assertFalse(res.released)
        self.assertEqual(res.verdict, "block")
        self.assertIsNone(res.dispatched_prompt)

    def test_failclosed_writes_audit_row(self):
        gate = make_gate(reviewer=self._reviewer_raising(ProviderError("boom")))
        res = gate.review_and_dispatch(BASE, standard_meta())
        row = gate.store.get(res.record_id)
        self.assertIsNotNone(row)
        self.assertTrue(row.fail_closed)
        self.assertEqual(gate.store.count_fail_closed(), 1)


if __name__ == "__main__":
    unittest.main()
