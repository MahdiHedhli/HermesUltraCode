"""Roster schema + the reviewer-lab invariant (#8): a cross-lab reviewer must differ in lab
from the orchestrator; a same-lab reviewer may never present as cross-lab. Offline."""

import unittest

from core.roster import (
    REVIEWER_CROSS_LAB,
    REVIEWER_OFF,
    REVIEWER_SAME_LAB_FLAGGED,
    Endpoint,
    Roster,
    RosterError,
    resolve_reviewer_mode,
)


def base(**over):
    d = {
        "orchestrator": {"profile": "orchestrator", "lab": "anthropic"},
        "reviewer": {"profile": "xai", "lab": "xai"},
        "providers": [
            {"profile": "claude", "provider": "anthropic-x", "lab": "anthropic", "flat_rate": True},
            {"profile": "xai", "provider": "xai-x", "lab": "xai"},
            {"profile": "gemma-local", "provider": "local", "lab": "local", "model": "gemma-4-31b"},
        ],
        "routing": {"trivial": ["gemma-local", "claude"], "standard": ["claude", "xai"]},
    }
    d.update(over)
    return d


class RosterTest(unittest.TestCase):
    def test_valid_cross_lab(self):
        r = Roster.from_dict(base())
        self.assertEqual(r.reviewer_mode, REVIEWER_CROSS_LAB)
        self.assertEqual(r.orchestrator.lab, "anthropic")
        self.assertIsNotNone(r.provider("gemma-local"))
        self.assertTrue(r.provider("claude").flat_rate)

    def test_same_lab_reviewer_cannot_present_cross_lab(self):
        d = base(reviewer={"profile": "claude", "lab": "anthropic"}, reviewer_mode="cross_lab")
        with self.assertRaises(RosterError):
            Roster.from_dict(d)

    def test_reviewer_mode_resolution(self):
        self.assertEqual(Roster.from_dict(base(reviewer_mode="")).reviewer_mode, REVIEWER_CROSS_LAB)
        d = base(reviewer={"profile": "claude", "lab": "anthropic"}, reviewer_mode="")
        self.assertEqual(Roster.from_dict(d).reviewer_mode, REVIEWER_SAME_LAB_FLAGGED)
        self.assertEqual(Roster.from_dict(base(reviewer=None, reviewer_mode="")).reviewer_mode, REVIEWER_OFF)

    def test_routing_references_must_be_declared(self):
        with self.assertRaises(RosterError):
            Roster.from_dict(base(routing={"trivial": ["ghost-profile"]}))

    def test_board_defaults_to_ultracode_and_is_configurable(self):
        self.assertEqual(Roster.from_dict(base()).board, "ultracode")
        self.assertEqual(Roster.from_dict(base(board="uc-prod")).board, "uc-prod")

    def test_resolve_reviewer_mode_helper(self):
        o = Endpoint("orch", "anthropic")
        self.assertEqual(resolve_reviewer_mode(o, Endpoint("r", "xai"), requested=None), REVIEWER_CROSS_LAB)
        self.assertEqual(resolve_reviewer_mode(o, Endpoint("r", "anthropic"), requested=None),
                         REVIEWER_SAME_LAB_FLAGGED)
        self.assertEqual(resolve_reviewer_mode(o, None, requested=None), REVIEWER_OFF)
        with self.assertRaises(RosterError):
            resolve_reviewer_mode(o, Endpoint("r", "anthropic"), requested="cross_lab")


class CouncilLabTest(unittest.TestCase):
    def test_labs_of_includes_aggregator(self):
        from core.roster import Council, labs_of
        ep = Endpoint("rev", council=Council(members=("openai", "deepseek"), aggregator="anthropic"))
        self.assertEqual(labs_of(ep), frozenset({"openai", "deepseek", "anthropic"}))
        self.assertEqual(labs_of(Endpoint("plain", lab="xai")), frozenset({"xai"}))
        self.assertEqual(labs_of(None), frozenset())

    def test_council_reviewer_disjoint_passes(self):
        # orch anthropic; council reviewer (openai, deepseek + aggregator xai) — disjoint => cross_lab
        r = Roster.from_dict(base(
            orchestrator={"profile": "orch", "lab": "anthropic"},
            reviewer={"profile": "council-rev",
                      "council": {"members": ["openai", "deepseek"], "aggregator": "xai"}}))
        self.assertEqual(r.reviewer_mode, REVIEWER_CROSS_LAB)

    def test_council_aggregator_collision_fails(self):
        # THE test: members differ (openai/deepseek) but the AGGREGATOR (which decides) matches
        # the orchestrator's lab (anthropic) -> must fail. This is why the check is a SET op.
        with self.assertRaises(RosterError):
            Roster.from_dict(base(
                orchestrator={"profile": "orch", "lab": "anthropic"},
                reviewer={"profile": "council-rev",
                          "council": {"members": ["openai", "deepseek"], "aggregator": "anthropic"}},
                reviewer_mode="cross_lab"))

    def test_council_orchestrator_collision_also_fails(self):
        # Symmetric: a COUNCIL orchestrator whose aggregator equals the plain reviewer's lab.
        with self.assertRaises(RosterError):
            Roster.from_dict(base(
                orchestrator={"profile": "orch", "council": {"members": ["openai"], "aggregator": "xai"}},
                reviewer={"profile": "xai", "lab": "xai"},
                reviewer_mode="cross_lab"))


if __name__ == "__main__":
    unittest.main()
